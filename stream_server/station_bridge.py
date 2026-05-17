"""Client -> server signaling for stream_server.

Topics (TCP_WIRE_SPEC §6):
  video_feedback     JSON {latency_p95_ms, freeze_ms_last_1s, ...}
                     Consumed locally by BitrateController (informational —
                     ABR decisions are made from put_latency + RTT, per spec
                     §9, but we still log/expose these from the client).
  stream_heartbeat   JSON {ts}. 5 Hz; rising edge => dedupe reset.
                     3 s silence => mark client disconnected.

There is no client->server video_ctl in the TCP variant: bitrate decisions
live entirely in this server's BitrateController.
"""
import json
import logging
import time
from typing import Optional

import zenoh

logger = logging.getLogger(__name__)


class StationBridge:
    _HANDLED_TOPICS = frozenset({"video_feedback", "stream_heartbeat"})

    def __init__(self, state, loop, robot_bridge, bitrate_controller=None):
        self._state = state
        self._loop = loop
        self._proto = robot_bridge
        self._bitrate_controller = bitrate_controller
        self._subs: list = []
        self._session: Optional[zenoh.Session] = None

    def start(self, session) -> None:
        self._session = session
        # Same wildcard as RobotBridge; RobotBridge filters out client topics,
        # so each side only handles its own direction.
        self._subs = [
            session.declare_subscriber("nev/stream_tcp/**", self._on_stream),
        ]
        logger.info("StationBridge started (wildcard nev/stream_tcp/**, client side)")

    def stop(self) -> None:
        for sub in self._subs:
            sub.undeclare()

    def attach_bitrate_controller(self, controller) -> None:
        self._bitrate_controller = controller

    def _call(self, fn, *args) -> None:
        self._loop.call_soon_threadsafe(fn, *args)

    def _on_stream(self, sample) -> None:
        # nev/stream_tcp/{vehicle_id}/{topic}
        parts = str(sample.key_expr).split("/")
        if len(parts) < 4:
            return
        vehicle_id = parts[2]
        topic = parts[3]
        if topic not in self._HANDLED_TOPICS:
            return
        raw = bytes(sample.payload)

        if topic == "video_feedback":
            self._handle_video_feedback(vehicle_id, raw)
        elif topic == "stream_heartbeat":
            self._handle_stream_heartbeat(vehicle_id, raw)

    def _handle_video_feedback(self, vehicle_id: str, raw: bytes) -> None:
        # TCP_WIRE_SPEC §6.b — client QoE telemetry. Stored in state for
        # logging/observability; BitrateController accepts these but its
        # mandatory inputs are put_latency + RTT (spec §9).
        try:
            data = json.loads(raw)
            latency = float(data.get("latency_p95_ms", 0.0))
            freeze = float(data.get("freeze_ms_last_1s", 0.0))
            cam_id = str(data.get("camera_id", "") or "")
        except Exception as e:
            logger.warning(f"video_feedback parse error: {e}")
            return

        recv_mono = time.monotonic()
        self._call(
            self._update_video_feedback, vehicle_id, cam_id, latency, freeze, recv_mono
        )
        if self._bitrate_controller is not None:
            try:
                self._bitrate_controller.on_video_feedback(
                    vehicle_id, latency, freeze, recv_mono
                )
            except Exception as e:
                logger.warning(f"on_video_feedback notify failed: {e}")

    def _handle_stream_heartbeat(self, vehicle_id: str, raw: bytes) -> None:
        # TCP_WIRE_SPEC §6.c — 5 Hz pulse; rising edge resets dedupe to
        # cover the case where the bot restarted and vehicle_ts rewound.
        try:
            json.loads(raw) if raw else {}
        except Exception:
            pass

        was_connected = self._state.client_connected

        def _update():
            self._state.client_last_recv = time.monotonic()
            if not self._state.client_connected:
                self._state.update_client_connected(True)
                logger.info(
                    f"[stream_tcp] client connected (heartbeat vid={vehicle_id})"
                )

        self._call(_update)

        if not was_connected:
            try:
                self._proto.reset_dedupe(vehicle_id)
            except Exception as e:
                logger.warning(f"reset_dedupe on heartbeat resume failed: {e}")

    def _update_video_feedback(
        self,
        vehicle_id: str,
        cam_id: str,
        latency_p95_ms: float,
        freeze_ms_last_1s: float,
        recv_mono: float,
    ) -> None:
        veh = self._state.get_vehicle(vehicle_id)
        if cam_id:
            veh.touch_camera(cam_id)
        veh.network.latency_p95_ms = round(latency_p95_ms, 2)
        veh.network.freeze_ms_last_1s = round(freeze_ms_last_1s, 2)
        veh.network.feedback_recv_monotonic = recv_mono
