import asyncio
import json
import logging
import time

import zenoh

from teleop_server.config.schema import TelemetryConfig
from teleop_server.telemetry.parser import extract_timestamp_delay
from teleop_server.telemetry.metrics import M
from teleop_server.state import SharedState
from teleop_contracts import (
    IncompatibleSchemaError,
    make_envelope,
    parse_envelope,
    TOPIC_BOT_HEARTBEAT,
    TOPIC_CMD,
    TOPIC_CMD_MODE_BOT,
    TOPIC_CPU,
    TOPIC_DISK,
    TOPIC_ESTOP_CMD,
    TOPIC_ESTOP_STATUS,
    TOPIC_GPU,
    TOPIC_MEM,
    TOPIC_MUX,
    TOPIC_NET,
    TOPIC_TELEMETRY,
    TOPIC_TWIST,
    key_for,
)

logger = logging.getLogger(__name__)


# Bot-originated suffixes this protocol handles. Subscribers narrow to these
# so we never re-process server-published topics (cmd, cmd_mode, estop_cmd,
# telemetry) — those would feed back into bandwidth counters and trigger
# auto-creation of empty VehicleState entries.
_BOT_SUFFIXES = (
    TOPIC_MUX,
    TOPIC_TWIST,
    TOPIC_ESTOP_STATUS,
    TOPIC_CPU,
    TOPIC_MEM,
    TOPIC_GPU,
    TOPIC_DISK,
    TOPIC_NET,
    TOPIC_BOT_HEARTBEAT,
)
_BOT_HANDLED_TOPICS = frozenset(_BOT_SUFFIXES)


_QOS_BE_LOW = dict(
    reliability=zenoh.Reliability.BEST_EFFORT,
    congestion_control=zenoh.CongestionControl.DROP,
    priority=zenoh.Priority.DATA_LOW,
)
_QOS_BE_HIGH = dict(
    reliability=zenoh.Reliability.BEST_EFFORT,
    congestion_control=zenoh.CongestionControl.DROP,
    priority=zenoh.Priority.INTERACTIVE_HIGH,
)
_QOS_REL_RT = dict(
    reliability=zenoh.Reliability.RELIABLE,
    congestion_control=zenoh.CongestionControl.BLOCK,
    priority=zenoh.Priority.REAL_TIME,
)
_QOS_REL_HIGH = dict(
    reliability=zenoh.Reliability.RELIABLE,
    congestion_control=zenoh.CongestionControl.BLOCK,
    priority=zenoh.Priority.INTERACTIVE_HIGH,
)

# suffix → QoS for teleop-side publishers (server → bot/client).
# Video-related QoS (camera/video_ctl/rtx_request) has moved to stream_server.
TELEOP_PUB_QOS = {
    TOPIC_TELEMETRY: _QOS_BE_LOW,
    TOPIC_CMD: _QOS_BE_HIGH,
    TOPIC_ESTOP_CMD: _QOS_REL_RT,
    TOPIC_CMD_MODE_BOT: _QOS_REL_HIGH,
}


class RobotProtocol:
    """Bot <-> teleop_server telemetry bridge.

    Telemetry-only. Does not handle any camera/video topics — those are
    handled by stream_server on separate ports (7457/7458).

    Topic mapping:
      bot -> server: nev/teleop/{vid}/mux, twist, estop, cpu, mem, gpu, disk, net
                     nev/teleop/{vid}/bot_heartbeat (per-vehicle liveness)
      server -> bot: nev/teleop/{vid}/cmd, estop_cmd, cmd_mode (control command relay)
      server -> client: nev/teleop/{vid}/telemetry (aggregated state)

    The RTT measurement channel (telemetry_ping/telemetry_pong) runs
    end-to-end between client and bot; the server only passes it through
    the zenoh router.
    """

    def __init__(
        self,
        state: SharedState,
        loop: asyncio.AbstractEventLoop,
        telemetry_cfg: TelemetryConfig = None,
    ):
        self.state = state
        self._loop = loop
        self._session: zenoh.Session | None = None
        self._cfg = telemetry_cfg or TelemetryConfig()
        self._subs: list = []
        self._seq = 0

        self._veh_pubs: dict[str, dict[str, zenoh.Publisher]] = {}
        self._veh_tele_bytes: dict[str, int] = {}
        # monotonic clock for measuring elapsed time between bandwidth
        # samples — immune to wall-clock jumps (NTP slew/step).
        self._bw_ts = time.monotonic()

    def start(self, session: zenoh.Session) -> None:
        self._session = session
        # Narrow each subscriber to a specific bot-originated suffix so we
        # never receive server-published topics (cmd/cmd_mode/estop_cmd/
        # telemetry). Using one subscriber per suffix avoids any self-loop.
        # Wildcard vid in the key expression — key_for() rejects "*", so
        # we build the wildcard key manually here.
        self._subs = [
            session.declare_subscriber(
                f"nev/teleop/*/{suffix}", self._on_teleop
            )
            for suffix in _BOT_SUFFIXES
        ]
        logger.info(
            f"RobotProtocol started (subs={_BOT_SUFFIXES})"
        )

    def stop(self) -> None:
        for sub in self._subs:
            sub.undeclare()
        for pubs in self._veh_pubs.values():
            for pub in pubs.values():
                pub.undeclare()
        self._veh_pubs.clear()

    def _call(self, fn, *args):
        self._loop.call_soon_threadsafe(fn, *args)

    def _get_pub(self, vehicle_id: str, suffix: str) -> zenoh.Publisher:
        if vehicle_id not in self._veh_pubs:
            self._veh_pubs[vehicle_id] = {}
        pubs = self._veh_pubs[vehicle_id]
        if suffix not in pubs:
            key = key_for(vehicle_id, suffix)
            qos = TELEOP_PUB_QOS.get(suffix, _QOS_BE_LOW)
            pubs[suffix] = self._session.declare_publisher(key, **qos)
            logger.info(f"Declared teleop publisher: {key}")
        return pubs[suffix]

    def _add_tele_bytes(self, vehicle_id: str, n: int):
        self._veh_tele_bytes[vehicle_id] = self._veh_tele_bytes.get(vehicle_id, 0) + n

    def reset_dedupe(self, vehicle_id: str) -> None:
        # teleop_server has no dedupe cache (it's a video-only feature),
        # but keep this as a no-op for compatibility with the disconnect ->
        # reconnect logic in main.py.
        return

    def release_vehicle_pubs(self, vehicle_id: str) -> None:
        """Tear down all publishers for a vehicle (e.g. on disconnect).

        Publishers will be lazily re-declared on the next outbound send
        (cmd / cmd_mode / estop_cmd / telemetry) via _get_pub.
        """
        pubs = self._veh_pubs.pop(vehicle_id, None)
        if not pubs:
            return
        for suffix, pub in pubs.items():
            try:
                pub.undeclare()
            except Exception as e:
                logger.debug(
                    f"undeclare pub [{key_for(vehicle_id, suffix)}]: {e}"
                )

    def _on_teleop(self, sample):
        # nev/teleop/{vehicle_id}/{topic}
        # Subscribers are narrowed to bot-originated suffixes in start(),
        # so this callback should only ever see _BOT_HANDLED_TOPICS. The
        # explicit filter below is defense-in-depth: it stops a stray PUT
        # from auto-creating an empty VehicleState via _handle_telemetry.
        parts = str(sample.key_expr).split("/")
        if len(parts) != 4:
            return
        vehicle_id = parts[2]
        topic = parts[3]
        if topic not in _BOT_HANDLED_TOPICS:
            return
        raw = bytes(sample.payload)

        if topic == TOPIC_BOT_HEARTBEAT:
            self._handle_bot_heartbeat(vehicle_id)
        else:
            self._handle_telemetry(vehicle_id, topic, raw)

    def _handle_bot_heartbeat(self, vehicle_id: str):
        def _update():
            veh = self.state.get_vehicle(vehicle_id)
            veh.last_robot_recv = time.monotonic()

        self._call(_update)

    def _handle_telemetry(self, vehicle_id: str, topic: str, raw: bytes):
        self._add_tele_bytes(vehicle_id, len(raw))
        # Bytes-in is counted per topic suffix, BEFORE parsing — we want
        # the metric to reflect actual wire traffic even when a payload
        # is malformed.
        try:
            M.inc_bytes_in(topic, len(raw))
        except Exception:
            pass
        try:
            _v, data = parse_envelope(raw)
        except IncompatibleSchemaError as e:
            logger.warning(f"drop incompatible schema [{topic}]: {e}")
            try:
                M.inc_parse_error(topic)
            except Exception:
                pass
            return
        except Exception as e:
            logger.warning(f"JSON parse error [{topic}]: {e}")
            try:
                M.inc_parse_error(topic)
            except Exception:
                pass
            return

        # tele_delay_ms is wall-clock(server) - ts(bot); requires NTP/PTP
        # synced clocks between bot and server to be meaningful.
        ts, tele_delay_ms = extract_timestamp_delay(data)

        if topic == TOPIC_MUX:

            def _update():
                veh = self.state.get_vehicle(vehicle_id)
                veh.update_packet("mux", data)
                veh.update_remote_enabled(data.get("remote_enabled", False))
                if tele_delay_ms is not None:
                    veh.network.tele_delay_ms = tele_delay_ms

            self._call(_update)

        elif topic == TOPIC_TWIST:

            def _update():
                veh = self.state.get_vehicle(vehicle_id)
                veh.update_packet("twist", data)
                if tele_delay_ms is not None:
                    veh.network.tele_delay_ms = tele_delay_ms

            self._call(_update)

        elif topic == TOPIC_ESTOP_STATUS:

            def _update():
                veh = self.state.get_vehicle(vehicle_id)
                veh.update_packet("estop", data)
                if tele_delay_ms is not None:
                    veh.network.tele_delay_ms = tele_delay_ms

            self._call(_update)

        elif topic == TOPIC_CPU or topic == TOPIC_MEM:
            self._call(
                lambda: self.state.get_vehicle(vehicle_id).update_packet("resources", data)
            )

        elif topic == TOPIC_GPU:

            def _update():
                veh = self.state.get_vehicle(vehicle_id)
                # Envelope wraps the bare list under "gpus" (required so
                # the {v, ts} fields can ride on the JSON object — bare
                # arrays can't carry an envelope). The bot was changed to
                # publish {"gpus": [...]} when this contract package was
                # introduced.
                gpus = data.get("gpus", [])
                for g in gpus if isinstance(gpus, list) else []:
                    try:
                        veh.update_gpu(
                            g["idx"],
                            {
                                "gpu_usage": g["gpu_usage"],
                                "gpu_mem_used": g["gpu_mem_used"],
                                "gpu_mem_total": g["gpu_mem_total"],
                                "gpu_temp": g["gpu_temp"],
                                "gpu_power": g["gpu_power"],
                            },
                        )
                    except (KeyError, TypeError):
                        pass

            self._call(_update)

        elif topic == TOPIC_DISK:

            def _update():
                veh = self.state.get_vehicle(vehicle_id)
                for p in data.get("partitions", []):
                    try:
                        veh.update_disk_partition(
                            p["idx"],
                            {
                                "mountpoint": p["mountpoint"],
                                "total_bytes": p["total_bytes"],
                                "used_bytes": p["used_bytes"],
                                "percent": p["percent"],
                                "accessible": p["accessible"],
                            },
                        )
                    except (KeyError, TypeError):
                        pass

            self._call(_update)

        elif topic == TOPIC_NET:

            def _update():
                veh = self.state.get_vehicle(vehicle_id)
                try:
                    veh.update_packet(
                        "resources",
                        {
                            "net_total_ifaces": data["net_total_ifaces"],
                            "net_active_ifaces": data["net_active_ifaces"],
                            "net_down_ifaces": data["net_down_ifaces"],
                        },
                    )
                except (KeyError, TypeError):
                    pass
                for iface in data.get("interfaces", []):
                    try:
                        veh.update_net_interface(
                            iface["idx"],
                            {
                                "name": iface["name"],
                                "is_up": iface["is_up"],
                                "speed_mbps": iface["speed_mbps"],
                                "in_bps": iface["in_bps"],
                                "out_bps": iface["out_bps"],
                            },
                        )
                    except (KeyError, TypeError):
                        pass

            self._call(_update)

        else:
            logger.debug(f"Unknown teleop topic: nev/teleop/{vehicle_id}/{topic}")

    def _next_seq(self) -> int:
        s = self._seq
        self._seq = (self._seq + 1) % self._cfg.seq_max
        return s

    def _zput(self, vehicle_id: str, suffix: str, data: dict) -> None:
        try:
            pub = self._get_pub(vehicle_id, suffix)
            payload = json.dumps(make_envelope(data))
            pub.put(payload)
            try:
                M.inc_bytes_out(suffix, len(payload))
            except Exception:
                pass
        except Exception as e:
            logger.warning(f"zenoh put [{key_for(vehicle_id, suffix)}]: {e}")
            try:
                M.inc_publish_error(suffix)
            except Exception:
                pass

    def send_teleop(self, vehicle_id: str, linear_x: float, steer_angle: float):
        self._zput(
            vehicle_id,
            TOPIC_CMD,
            {
                "linear_x": round(linear_x, 3),
                "steer_angle": round(steer_angle, 4),
                "seq": self._next_seq(),
            },
        )

    def send_estop(self, vehicle_id: str, activate: bool):
        self._zput(vehicle_id, TOPIC_ESTOP_CMD, {"active": activate, "seq": self._next_seq()})

    def send_cmd_mode(self, vehicle_id: str, mode: int):
        self._zput(vehicle_id, TOPIC_CMD_MODE_BOT, {"mode": mode, "seq": self._next_seq()})

    def send_telemetry(self, state_json: str):
        """Broadcast aggregated telemetry to all connected vehicles' client topics.

        ``state_json`` is the pre-serialized aggregate from ``state.to_json``.
        The envelope ``v`` / ``ts`` are added inside ``to_json`` already, so
        we pass the string through unchanged — re-parsing just to envelope
        would waste a JSON round-trip on the hot telemetry path.
        """
        # Pre-compute the payload size once; the same bytes are broadcast
        # to every vehicle, so accounting per-vehicle is what we want for
        # the bytes_out_total counter (one increment per pub.put()).
        try:
            payload_len = len(state_json.encode("utf-8")) if isinstance(state_json, str) \
                else len(state_json)
        except Exception:
            payload_len = 0
        for vehicle_id in list(self.state.vehicles.keys()):
            try:
                pub = self._get_pub(vehicle_id, TOPIC_TELEMETRY)
                pub.put(state_json)
                try:
                    M.inc_bytes_out(TOPIC_TELEMETRY, payload_len)
                except Exception:
                    pass
            except Exception as e:
                logger.warning(
                    f"zenoh put [{key_for(vehicle_id, TOPIC_TELEMETRY)}]: {e}"
                )
                try:
                    M.inc_publish_error(TOPIC_TELEMETRY)
                except Exception:
                    pass

    def calc_bandwidth(self):
        now = time.monotonic()
        dt = now - self._bw_ts
        if dt < self._cfg.bw_calc_interval:
            return
        self._bw_ts = now

        all_vids = set(self.state.vehicles.keys()) | set(self._veh_tele_bytes.keys())
        for vehicle_id in all_vids:
            tele_bytes = self._veh_tele_bytes.pop(vehicle_id, 0)
            tele_mbps = round(tele_bytes * 8 / (dt * 1e6), 3)

            def _update(vid=vehicle_id, tm=tele_mbps):
                veh = self.state.get_vehicle(vid)
                veh.network.bw_telemetry = tm

            self._call(_update)

