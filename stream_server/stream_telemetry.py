"""Send-loop helpers for stream_server.

video_ping scheduling (1 Hz default) and liveness checks for both client
heartbeats and bot AU/pong arrivals. Mirrors the role of stream_server's
StreamTelemetryDriver but with no UDP-specific paths.
"""
import logging
import time

from stream_server.state import StreamSharedState

logger = logging.getLogger(__name__)


class StreamTelemetryDriver:
    def __init__(
        self,
        state: StreamSharedState,
        proto,
        cfg_vehicle_id: str,
        client_heartbeat_timeout: float,
        bot_disconnect_timeout: float,
    ):
        self._state = state
        self._proto = proto
        self._vehicle_id = cfg_vehicle_id
        self._client_timeout = client_heartbeat_timeout
        self._bot_timeout = bot_disconnect_timeout
        self._bot_disconnected: set[str] = set()

    def configured_vehicle_id(self) -> str:
        return self._vehicle_id

    def observed_vehicle_ids(self) -> list[str]:
        return list(self._state.vehicles.keys())

    def send_video_pings(self) -> None:
        # Always include the configured vid so RTT measurement starts before
        # the first camera frame arrives (TCP_WIRE_SPEC §6.d).
        targets = set(self.observed_vehicle_ids())
        if self._vehicle_id:
            targets.add(self._vehicle_id)
        for vid in targets:
            try:
                self._proto.send_video_ping(vid)
            except Exception as e:
                logger.warning(f"[{vid}] send_video_ping failed: {e}")

    def check_client_heartbeat(self) -> None:
        if not self._state.client_connected:
            return
        last = self._state.client_last_recv
        if last <= 0.0:
            return
        if (time.monotonic() - last) > self._client_timeout:
            logger.warning(
                f"[stream_tcp] client heartbeat timeout "
                f"({self._client_timeout:.1f}s) — marking disconnected"
            )
            self._state.update_client_connected(False)

    def check_bot_disconnect(self) -> None:
        # On disconnect detection, reset dedupe so the next first frame
        # (whose vehicle_ts may rewind) is not masked by stale cache entries.
        # The set guards against repeated resets while the bot stays down.
        now = time.monotonic()
        for vid in list(self._state.vehicles.keys()):
            veh = self._state.vehicles.get(vid)
            if veh is None:
                continue
            if veh.last_robot_recv <= 0.0:
                continue
            age = now - veh.last_robot_recv
            if age > self._bot_timeout and vid not in self._bot_disconnected:
                self._bot_disconnected.add(vid)
                self._proto.reset_dedupe(vid)
                logger.warning(
                    f"[{vid}] stream_tcp bot disconnected "
                    f"(no camera/pong for {age:.1f}s) — dedupe reset"
                )
            elif age < 1.0 and vid in self._bot_disconnected:
                self._bot_disconnected.discard(vid)
                logger.info(f"[{vid}] stream_tcp bot reconnected")
