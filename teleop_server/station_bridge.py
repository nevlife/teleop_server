import json
import logging
import math
import time

import zenoh

from teleop_server.telemetry.metrics import M
from teleop_contracts import (
    IncompatibleSchemaError,
    parse_envelope,
    TOPIC_CLIENT_HEARTBEAT,
    TOPIC_CMD_MODE,
    TOPIC_CONTROLLER_HEARTBEAT,
    TOPIC_ESTOP,
    TOPIC_TELEOP,
)

logger = logging.getLogger(__name__)


class StationBridge:
    """Client -> teleop_server -> bot control signal relay.

    Telemetry-only. Video-related topics (video_ctl/video_feedback/
    rtx_request/stream_heartbeat) are handled by stream_server on a
    separate router.

    Handled topics (all under nev/teleop/{vid}/...):
      teleop                — vehicle control command. Relayed to bot as cmd.
      estop                 — emergency stop. Relayed to bot as estop_cmd, RELIABLE.
      cmd_mode              — mode switch. Relayed to bot, RELIABLE.
      client_heartbeat      — GCS process alive signal -> station_connected.
      controller_heartbeat  — joystick alive signal -> joystick_connected.

    RTT (telemetry_ping/telemetry_pong) only passes through the zenoh
    router and is not handled here.
    """

    _HANDLED_TOPICS = frozenset({
        TOPIC_TELEOP, TOPIC_ESTOP, TOPIC_CMD_MODE,
        TOPIC_CLIENT_HEARTBEAT, TOPIC_CONTROLLER_HEARTBEAT,
    })
    # Per-vehicle topics (need a VehicleState) vs station-wide topics.
    _VEHICLE_HANDLED_TOPICS = frozenset({TOPIC_TELEOP, TOPIC_ESTOP, TOPIC_CMD_MODE})

    def __init__(self, state, loop, robot_proto):
        self._state = state
        self._loop = loop
        self._proto = robot_proto
        self._subs: list = []
        self._session: zenoh.Session | None = None

    def start(self, session) -> None:
        self._session = session
        # Narrow each subscriber to a specific client-originated suffix so we
        # never receive server-published topics (cmd/cmd_mode/estop_cmd/
        # telemetry) or bot-originated telemetry. One subscriber per suffix.
        self._subs = [
            session.declare_subscriber(
                f"nev/teleop/*/{topic}", self._on_teleop
            )
            for topic in sorted(self._HANDLED_TOPICS)
        ]
        logger.info(
            f"StationBridge started (subs={sorted(self._HANDLED_TOPICS)})"
        )

    def stop(self) -> None:
        for sub in self._subs:
            sub.undeclare()

    def _call(self, fn, *args):
        self._loop.call_soon_threadsafe(fn, *args)

    def _on_teleop(self, sample):
        # nev/teleop/{vehicle_id}/{topic}
        # Subscribers are narrowed in start() to _HANDLED_TOPICS, but we
        # filter again BEFORE any state mutation (get_vehicle would
        # auto-create an empty VehicleState — a stray PUT must not do that).
        parts = str(sample.key_expr).split("/")
        if len(parts) != 4:
            return
        vehicle_id = parts[2]
        topic = parts[3]
        if topic not in self._HANDLED_TOPICS:
            return
        raw = bytes(sample.payload)
        try:
            M.inc_bytes_in(topic, len(raw))
        except Exception:
            pass

        # Per-vehicle commands need a VehicleState; station-wide heartbeats
        # do not. Only auto-create AFTER the topic filter above.
        if topic in self._VEHICLE_HANDLED_TOPICS:
            self._call(self._state.get_vehicle, vehicle_id)

        if topic == TOPIC_TELEOP:
            self._handle_teleop(vehicle_id, raw)

        elif topic == TOPIC_ESTOP:
            self._handle_estop(vehicle_id, raw)

        elif topic == TOPIC_CMD_MODE:
            self._handle_cmd_mode(vehicle_id, raw)

        elif topic == TOPIC_CLIENT_HEARTBEAT:
            self._handle_client_hb()

        elif topic == TOPIC_CONTROLLER_HEARTBEAT:
            self._handle_controller_hb(raw)

    def _handle_teleop(self, vehicle_id: str, raw: bytes):
        try:
            _v, data = parse_envelope(raw)
            if not self._state.station_connected:
                return
            lx = float(data.get("linear_x", 0.0))
            steer = float(data.get("steer_angle", 0.0))
            self._proto.send_teleop(vehicle_id, lx, steer)
            steer_deg = math.degrees(steer)
            self._call(self._update_control, lx, steer_deg)
        except IncompatibleSchemaError as e:
            logger.warning(f"station teleop drop: {e}")
            try:
                M.inc_parse_error(TOPIC_TELEOP)
            except Exception:
                pass
        except Exception as e:
            logger.warning(f"station teleop parse error: {e}")
            try:
                M.inc_parse_error(TOPIC_TELEOP)
            except Exception:
                pass

    def _handle_estop(self, vehicle_id: str, raw: bytes):
        try:
            _v, data = parse_envelope(raw)
            active = bool(data.get("active", False))
            self._proto.send_estop(vehicle_id, active)
            self._call(self._update_estop, active)
            logger.info(f"[{vehicle_id}] Station e-stop -> {active}")
        except IncompatibleSchemaError as e:
            logger.warning(f"station estop drop: {e}")
            try:
                M.inc_parse_error(TOPIC_ESTOP)
            except Exception:
                pass
        except Exception as e:
            logger.warning(f"station estop parse error: {e}")
            try:
                M.inc_parse_error(TOPIC_ESTOP)
            except Exception:
                pass

    def _handle_cmd_mode(self, vehicle_id: str, raw: bytes):
        try:
            _v, data = parse_envelope(raw)
            mode = int(data.get("mode", -1))
            self._proto.send_cmd_mode(vehicle_id, mode)
            self._call(self._update_mode, mode)
            logger.info(f"[{vehicle_id}] Station cmd_mode -> {mode}")
        except IncompatibleSchemaError as e:
            logger.warning(f"station cmd_mode drop: {e}")
            try:
                M.inc_parse_error(TOPIC_CMD_MODE)
            except Exception:
                pass
        except Exception as e:
            logger.warning(f"station cmd_mode parse error: {e}")
            try:
                M.inc_parse_error(TOPIC_CMD_MODE)
            except Exception:
                pass

    def _handle_client_hb(self):
        """`client_heartbeat` received -> GCS process is alive.

        Both the station_last_recv write and update_station_connected must
        happen on the event-loop thread (station_last_recv is read by the
        send loop; without the hop a torn read on 32-bit floats is possible
        and the two writes can race). Schedule both inside one lambda.
        """
        now = time.monotonic()

        def _apply():
            self._state.station_last_recv = now
            if not self._state.station_connected:
                self._state.update_station_connected(True)

        self._call(_apply)

    def _handle_controller_hb(self, raw: bytes):
        """`controller_heartbeat` received -> update joystick connection state."""
        try:
            _v, data = parse_envelope(raw)
            val = bool(data.get("connected", False))
            self._call(self._state.update_joystick_connected, val)
        except IncompatibleSchemaError as e:
            logger.warning(f"controller_heartbeat drop: {e}")
            try:
                M.inc_parse_error(TOPIC_CONTROLLER_HEARTBEAT)
            except Exception:
                pass
        except Exception as e:
            logger.warning(f"controller_heartbeat parse error: {e}")
            try:
                M.inc_parse_error(TOPIC_CONTROLLER_HEARTBEAT)
            except Exception:
                pass

    def _update_control(self, lx: float, steer_deg: float):
        self._state.control.linear_x = lx
        self._state.control.steer_angle_deg = steer_deg

    def _update_estop(self, active: bool):
        self._state.control.estop = active

    def _update_mode(self, mode: int):
        self._state.control.mode = mode
