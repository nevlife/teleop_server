"""Bot <-> server video bridge for stream_server.

Topics under `nev/stream_tcp/{vid}/...` (TCP_WIRE_SPEC §2):

  bot -> server : camera[/{cam}]  (16 B header + AU, §4)
                  video_pong      (JSON)
  server -> bot : video_ping      (JSON)
                  video_ctl       (JSON bitrate command, §6.a)
  server -> cli : camera[/{cam}]  (28 B header + AU, §5)

Everything publishes RELIABLE+BLOCK (§3). The AU payload is opaque bytes;
this server is a transparent relay — no NAL parsing.
"""
import asyncio
import json
import logging
import struct
import time
from collections import OrderedDict
from typing import Optional

import zenoh

from stream_server.config.schema import StreamTelemetryConfig, VideoConfig
from stream_server.state import StreamSharedState
from stream_server.wire_format import (
    BOT_HEADER_FMT,
    BOT_HEADER_SIZE,
    RELAY_HEADER_FMT,
    RELAY_HEADER_SIZE,
)
from stream_server.zenoh_utils import QOS_REL_BLOCK, QOS_REL_BLOCK_LOW

logger = logging.getLogger(__name__)


class RobotBridge:
    """Camera AU receive + dedupe + forward, plus video_ping/pong RTT."""

    def __init__(
        self,
        state: StreamSharedState,
        loop: asyncio.AbstractEventLoop,
        telemetry_cfg: StreamTelemetryConfig = None,
        video_cfg: VideoConfig = None,
        bitrate_controller=None,
    ):
        self.state = state
        self._loop = loop
        self._session: Optional[zenoh.Session] = None
        self._cfg = telemetry_cfg or StreamTelemetryConfig()
        self._video_cfg = video_cfg or VideoConfig()
        self._bitrate_controller = bitrate_controller
        self._subs: list = []

        # publishers: nested {vid: {suffix: Publisher}}
        self._veh_pubs: dict[str, dict[str, zenoh.Publisher]] = {}
        # rx bytes for bandwidth roll-up
        self._veh_cam_bytes: dict[tuple[str, str], int] = {}
        self._bw_ts = time.time()
        self._veh_last_pong: dict[str, float] = {}

        # Spec §13: drop counters by reason (exposed for logging).
        self.drop_vehicle_ts = 0
        self.drop_au_size = 0
        self.drop_duplicate = 0

        self._dedupe_cache_size = max(1, int(self._video_cfg.dedupe_cache_size))
        # OrderedDict per (vid, cam_id): key = vehicle_ts, value = None.
        # Move-to-end on hit, popitem(last=False) when over cap = LRU.
        self._veh_dedupe: dict[tuple[str, str], "OrderedDict[float, None]"] = {}

        self._vid_log_every = 60
        self._veh_cam_frames: dict[tuple[str, str], int] = {}
        self._veh_cam_vid_bytes: dict[tuple[str, str], int] = {}
        self._veh_cam_vid_t0: dict[tuple[str, str], float] = {}

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def start(self, session: zenoh.Session) -> None:
        self._session = session
        # Wildcard subscription mirrors stream_server's pattern. StationBridge
        # subscribes the same wildcard and routes by topic suffix; each side
        # ignores the other's directions.
        self._subs = [
            session.declare_subscriber("nev/stream_tcp/**", self._on_stream),
        ]
        logger.info("RobotBridge started (wildcard nev/stream_tcp/**)")

    def stop(self) -> None:
        for sub in self._subs:
            sub.undeclare()
        for pubs in self._veh_pubs.values():
            for pub in pubs.values():
                pub.undeclare()
        self._veh_pubs.clear()

    def attach_bitrate_controller(self, controller) -> None:
        self._bitrate_controller = controller

    def _call(self, fn, *args) -> None:
        self._loop.call_soon_threadsafe(fn, *args)

    # ------------------------------------------------------------------ #
    # Publishers
    # ------------------------------------------------------------------ #

    def _get_pub(self, vehicle_id: str, suffix: str) -> zenoh.Publisher:
        if vehicle_id not in self._veh_pubs:
            self._veh_pubs[vehicle_id] = {}
        pubs = self._veh_pubs[vehicle_id]
        if suffix not in pubs:
            key = f"nev/stream_tcp/{vehicle_id}/{suffix}"
            qos_key = suffix.split("/", 1)[0]
            # JSON control channels are deprioritised; AU camera traffic is
            # INTERACTIVE_HIGH so it wins egress contention vs. ping/heartbeat.
            qos = (
                QOS_REL_BLOCK_LOW
                if qos_key in ("video_ping", "video_ctl")
                else QOS_REL_BLOCK
            )
            pubs[suffix] = self._session.declare_publisher(key, **qos)
            logger.info(f"Declared publisher: {key} (RELIABLE+BLOCK)")
        return pubs[suffix]

    # ------------------------------------------------------------------ #
    # Subscription dispatch
    # ------------------------------------------------------------------ #

    def _on_stream(self, sample) -> None:
        # nev/stream_tcp/{vehicle_id}/{topic...}
        parts = str(sample.key_expr).split("/")
        if len(parts) < 4:
            return
        vehicle_id = parts[2]
        topic = "/".join(parts[3:])
        raw = bytes(sample.payload)

        if topic == "camera":
            self._handle_camera(vehicle_id, raw, cam_id="")
        elif topic.startswith("camera/") and len(parts) >= 5:
            self._handle_camera(vehicle_id, raw, cam_id=parts[4])
        elif topic == "video_pong":
            self._handle_video_pong(vehicle_id, raw)
        elif topic in ("video_ctl", "video_feedback", "stream_heartbeat", "video_ping"):
            # client-side or our own publishes; StationBridge owns the
            # client->server topics, video_ping/video_ctl we publish ourselves.
            return
        else:
            logger.debug(f"Unknown stream_tcp topic: nev/stream_tcp/{vehicle_id}/{topic}")

    # ------------------------------------------------------------------ #
    # Camera AU receive / relay
    # ------------------------------------------------------------------ #

    def _handle_camera(self, vehicle_id: str, raw: bytes, cam_id: str = "") -> None:
        cfg = self._video_cfg
        # Spec §13: AU size sanity (header excluded from "AU payload size").
        if len(raw) < cfg.au_min_bytes:
            self.drop_au_size += 1
            return
        au_size = len(raw) - BOT_HEADER_SIZE
        if au_size < 0 or au_size > cfg.au_max_bytes:
            self.drop_au_size += 1
            return

        try:
            ts, encode_ms, flags = struct.unpack_from(BOT_HEADER_FMT, raw, 0)
        except struct.error:
            self.drop_au_size += 1
            return

        # Spec §13: vehicle_ts sanity. Wall-clock epoch seconds.
        now_wall = time.time()
        if (
            ts < 0
            or ts < cfg.vehicle_ts_min
            or ts > (now_wall + cfg.vehicle_ts_future_s)
        ):
            self.drop_vehicle_ts += 1
            return

        # Dedupe (TCP_WIRE_SPEC §4: bot may dual-send same AU over multiple
        # links). Key = vehicle_ts per (vehicle_id, cam_id).
        dkey = (vehicle_id, cam_id)
        cache = self._veh_dedupe.get(dkey)
        if cache is None:
            cache = OrderedDict()
            self._veh_dedupe[dkey] = cache
        if ts in cache:
            cache.move_to_end(ts)
            self.drop_duplicate += 1
            return
        cache[ts] = None
        if len(cache) > self._dedupe_cache_size:
            cache.popitem(last=False)

        server_rx_ts = now_wall
        veh_to_srv_ms = max(0.0, (server_rx_ts - ts) * 1000.0)
        nal_size = len(raw) - BOT_HEADER_SIZE
        self._veh_cam_bytes[dkey] = self._veh_cam_bytes.get(dkey, 0) + len(raw)

        # Build 28 B relay header in-place. AU payload is appended unchanged
        # (TCP_WIRE_SPEC §5 — server is a transparent relay).
        out = bytearray(RELAY_HEADER_SIZE + nal_size)
        struct.pack_into(
            RELAY_HEADER_FMT,
            out,
            0,
            ts,
            encode_ms,
            flags,
            server_rx_ts,
            veh_to_srv_ms,
        )
        out[RELAY_HEADER_SIZE:] = memoryview(raw)[BOT_HEADER_SIZE:]

        suffix = "camera" if not cam_id else f"camera/{cam_id}"
        pub = self._get_pub(vehicle_id, suffix)

        # put_latency: monotonic before/after pub.put. With BLOCK enabled this
        # captures backpressure-induced waits, which is the ABR signal.
        put_t0 = time.monotonic()
        try:
            pub.put(bytes(out))
        except Exception as e:
            logger.warning(f"[{vehicle_id}/{cam_id or '-'}] pub.put failed: {e}")
            return
        put_latency_ms = (time.monotonic() - put_t0) * 1000.0

        if self._bitrate_controller is not None:
            try:
                self._bitrate_controller.on_put_latency(
                    vehicle_id, put_latency_ms, time.monotonic()
                )
            except Exception as e:
                logger.warning(f"on_put_latency notify failed: {e}")

        # Periodic per-camera log.
        self._veh_cam_frames[dkey] = self._veh_cam_frames.get(dkey, 0) + 1
        self._veh_cam_vid_bytes[dkey] = (
            self._veh_cam_vid_bytes.get(dkey, 0) + len(raw)
        )
        if dkey not in self._veh_cam_vid_t0:
            self._veh_cam_vid_t0[dkey] = server_rx_ts
        if self._veh_cam_frames[dkey] >= self._vid_log_every:
            t0_window = self._veh_cam_vid_t0.get(dkey, server_rx_ts)
            dt = max(1e-3, server_rx_ts - t0_window)
            rx_fps = self._veh_cam_frames[dkey] / dt
            bw_rx_mbps = self._veh_cam_vid_bytes[dkey] * 8.0 / (dt * 1e6)
            veh = self.state.vehicles.get(vehicle_id)
            rtt_ms = float(veh.network.rtt_server_bot_ms or 0.0) if veh else 0.0
            kbps = (
                veh.cameras.get(cam_id, {}).get("last_bitrate_kbps")
                if veh and cam_id
                else None
            )
            kbps_str = str(int(kbps)) if kbps is not None else "-"
            logger.info(
                f"[VID] vid={vehicle_id} cam={cam_id or '-'} "
                f"rx_fps={rx_fps:.1f} bw_rx_mbps={bw_rx_mbps:.2f} "
                f"rtt_ms={rtt_ms:.0f} put_lat_ms={put_latency_ms:.1f} "
                f"kbps={kbps_str}"
            )
            self._veh_cam_frames[dkey] = 0
            self._veh_cam_vid_bytes[dkey] = 0
            self._veh_cam_vid_t0[dkey] = server_rx_ts

        def _update():
            veh = self.state.get_vehicle(vehicle_id)
            veh.touch_camera(cam_id)
            veh.last_robot_recv = time.monotonic()

        self._call(_update)

    # ------------------------------------------------------------------ #
    # video_pong (server <-> bot RTT)
    # ------------------------------------------------------------------ #

    def _handle_video_pong(self, vehicle_id: str, raw: bytes) -> None:
        try:
            data = json.loads(raw)
            ts = data.get("ts")
            if ts is None:
                return
            rtt_ms = (time.time() - float(ts)) * 1000.0
            if rtt_ms < 0:
                return
            logger.debug(f"[{vehicle_id}] video pong: rtt={rtt_ms:.1f}ms")

            def _update():
                veh = self.state.get_vehicle(vehicle_id)
                veh.network.rtt_server_bot_ms = round(rtt_ms, 1)
                self._veh_last_pong[vehicle_id] = time.monotonic()

            self._call(_update)

            if self._bitrate_controller is not None:
                try:
                    self._bitrate_controller.on_rtt(
                        vehicle_id, rtt_ms, time.monotonic()
                    )
                except Exception as e:
                    logger.warning(f"on_rtt notify failed: {e}")
        except Exception as e:
            logger.warning(f"video pong parse error: {e}")

    # ------------------------------------------------------------------ #
    # Outgoing JSON channels
    # ------------------------------------------------------------------ #

    def _zput(self, vehicle_id: str, suffix: str, data: dict) -> None:
        try:
            pub = self._get_pub(vehicle_id, suffix)
            pub.put(json.dumps(data))
        except Exception as e:
            logger.warning(f"zenoh put [nev/stream_tcp/{vehicle_id}/{suffix}]: {e}")

    def send_video_ping(self, vehicle_id: str) -> None:
        self._zput(vehicle_id, "video_ping", {"ts": time.time()})

    def send_video_bitrate(self, vehicle_id: str, kbps: int, cam_id: str = "") -> None:
        # TCP_WIRE_SPEC §6.a — JSON `{"type":"bitrate","kbps":N[,"camera_id":...]}`.
        msg = {"type": "bitrate", "kbps": int(kbps)}
        if cam_id:
            msg["camera_id"] = cam_id
        self._zput(vehicle_id, "video_ctl", msg)

    # ------------------------------------------------------------------ #
    # Periodic maintenance (driven by main send loop)
    # ------------------------------------------------------------------ #

    def calc_bandwidth(self) -> None:
        now = time.time()
        dt = now - self._bw_ts
        if dt < 1.0:
            return
        self._bw_ts = now

        per_vid_cam_bytes: dict[str, dict[str, int]] = {}
        for (vid, cam_id), nbytes in list(self._veh_cam_bytes.items()):
            per_vid_cam_bytes.setdefault(vid, {})[cam_id] = nbytes
        self._veh_cam_bytes.clear()

        all_vids = set(self.state.vehicles.keys()) | set(per_vid_cam_bytes.keys())
        for vehicle_id in all_vids:
            cam_map = per_vid_cam_bytes.get(vehicle_id, {})
            cam_mbps_map = {
                cid: round(b * 8 / (dt * 1e6), 3) for cid, b in cam_map.items()
            }
            cam_mbps_total = round(sum(cam_mbps_map.values()), 3)

            def _update(vid=vehicle_id, cmap=cam_mbps_map, total=cam_mbps_total):
                veh = self.state.get_vehicle(vid)
                veh.network.bw_video_rx = total
                for cid, mbps in cmap.items():
                    cam = veh.cameras.get(cid)
                    if cam is None:
                        cam = veh.touch_camera(cid)
                    cam["bw_video_rx"] = mbps

            self._call(_update)

    def check_rtt_stale(self) -> None:
        now = time.monotonic()
        for vehicle_id in list(self.state.vehicles.keys()):
            last = self._veh_last_pong.get(vehicle_id, 0)
            if last > 0 and (now - last) > 3.0:

                def _update(vid=vehicle_id):
                    self.state.get_vehicle(vid).network.rtt_server_bot_ms = 0.0

                self._call(_update)

    def prune_stale_cameras(self) -> None:
        stale_s = float(self._video_cfg.camera_stale_s)
        for vid in list(self.state.vehicles.keys()):
            veh = self.state.vehicles.get(vid)
            if veh is None:
                continue
            removed = veh.prune_cameras(stale_s)
            for cid in removed:
                self._veh_dedupe.pop((vid, cid), None)
                self._veh_cam_bytes.pop((vid, cid), None)
                self._veh_cam_frames.pop((vid, cid), None)
                self._veh_cam_vid_bytes.pop((vid, cid), None)
                self._veh_cam_vid_t0.pop((vid, cid), None)
                logger.info(f"[{vid}] camera '{cid}' pruned (stale > {stale_s:.1f}s)")

    def reset_dedupe(self, vehicle_id: str) -> None:
        # Bot restart or stream_heartbeat-driven reconnect: vehicle_ts may
        # rewind, so cached pre-disconnect timestamps would falsely match
        # post-reconnect frames.
        for key in [k for k in self._veh_dedupe.keys() if k[0] == vehicle_id]:
            cache = self._veh_dedupe.get(key)
            if cache is not None:
                cache.clear()
