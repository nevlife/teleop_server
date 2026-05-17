"""Shared mutable state for stream_server.

Held by the asyncio loop thread; cross-thread updates are funnelled through
`loop.call_soon_threadsafe` (see RobotBridge._call). No mux/twist/estop
fields — this server is video-only.
"""
import json
import time
from dataclasses import dataclass

from stream_server.config.schema import StreamTelemetryConfig


@dataclass
class StreamNetworkStatus:
    # All metrics are stream-only. RTT is measured on this server's own
    # video_ping/video_pong channel (TCP_WIRE_SPEC §6.d).
    bw_video_rx: float = 0.0          # Mbps observed on bot->server AU stream
    bw_video_tx: float = 0.0          # Mbps observed on server->client relay
    rtt_server_bot_ms: float = 0.0
    # Client-reported video QoE — fed into the BitrateController for
    # informational logging; the controller decision uses put_latency + RTT.
    latency_p95_ms: float = 0.0
    freeze_ms_last_1s: float = 0.0
    feedback_recv_monotonic: float = 0.0


class VehicleStreamState:
    def __init__(self, vehicle_id: str):
        self.vehicle_id = vehicle_id
        self.network = StreamNetworkStatus()

        self.last_robot_recv: float = 0.0
        # cam_id "" is the single-camera channel (nev/stream_tcp/{vid}/camera).
        # Multi-camera bots use camera/{cam_id} (TCP_WIRE_SPEC §11).
        self.cameras: dict[str, dict] = {}

    def touch_camera(self, cam_id: str) -> dict:
        cam = self.cameras.get(cam_id)
        if cam is None:
            cam = {
                "last_seen": 0.0,
                "bw_video_rx": 0.0,
                "bw_video_tx": 0.0,
                "last_bitrate_kbps": None,
            }
            self.cameras[cam_id] = cam
        cam["last_seen"] = time.monotonic()
        return cam

    def prune_cameras(self, stale_s: float) -> list:
        # Returns removed cam_ids so callers can clean up parallel maps
        # (dedupe cache, byte counters) keyed on the same (vid, cam_id).
        now = time.monotonic()
        removed: list = []
        for cam_id, cam in list(self.cameras.items()):
            last = cam.get("last_seen", 0.0)
            if last > 0.0 and (now - last) > stale_s:
                self.cameras.pop(cam_id, None)
                removed.append(cam_id)
        return removed

    def to_dict(self) -> dict:
        return {
            "vehicle_id": self.vehicle_id,
            "network": {
                "bw_video_rx": self.network.bw_video_rx,
                "bw_video_tx": self.network.bw_video_tx,
                "rtt_server_bot_ms": self.network.rtt_server_bot_ms,
                "latency_p95_ms": self.network.latency_p95_ms,
                "freeze_ms_last_1s": self.network.freeze_ms_last_1s,
            },
            "cameras": {
                cid: {
                    "last_seen": cam.get("last_seen", 0.0),
                    "bw_video_rx": cam.get("bw_video_rx", 0.0),
                    "bw_video_tx": cam.get("bw_video_tx", 0.0),
                    "last_bitrate_kbps": cam.get("last_bitrate_kbps"),
                }
                for cid, cam in self.cameras.items()
            },
        }


class StreamSharedState:
    def __init__(self, telemetry_cfg: StreamTelemetryConfig = None):
        self._cfg = telemetry_cfg or StreamTelemetryConfig()
        self.vehicles: dict[str, VehicleStreamState] = {}

        # client_connected flips on stream_heartbeat receipt; the rising edge
        # triggers dedupe reset to recover from bot reconnect timestamp
        # restart (vehicle_ts may rewind).
        self.client_connected: bool = False
        self.client_last_recv: float = 0.0

    def get_vehicle(self, vehicle_id: str) -> VehicleStreamState:
        if vehicle_id not in self.vehicles:
            self.vehicles[vehicle_id] = VehicleStreamState(vehicle_id)
        return self.vehicles[vehicle_id]

    def update_client_connected(self, val: bool) -> None:
        if self.client_connected != val:
            self.client_connected = val

    def to_json(self) -> str:
        return json.dumps(
            {
                "vehicles": {vid: v.to_dict() for vid, v in self.vehicles.items()},
                "client_connected": self.client_connected,
                "server_time": time.time(),
            }
        )
