"""Configuration schema + YAML loader for stream_server.

The TCP variant has a deliberately smaller surface than the UDP/RTP server:
no retransmission or picture-loss-indication channels, no loss-based ABR,
no UDP listen port. Keep this module free of any imports from sibling
packages so the static `no-dep` test stays trivial.
"""
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict

import yaml


@dataclass
class ZenohConfig:
    # TCP_WIRE_SPEC §1: stream router reuses 7457 TCP; no UDP locator.
    tcp_port: int = 7457


@dataclass
class StreamServerConfig:
    video_ping_rate: float = 1.0
    client_heartbeat_timeout: float = 3.0
    bot_disconnect_timeout: float = 3.0
    bw_calc_interval: float = 1.0


@dataclass
class StreamTelemetryConfig:
    # Kept for parity with the send-loop signature; intentionally minimal —
    # the TCP variant has no seq numbers and the camera header size is
    # fixed by the wire spec (16 B = §4).
    camera_header_bytes: int = 16


@dataclass
class VideoConfig:
    enabled: bool = True
    initial_kbps: int = 2500
    min_kbps: int = 200
    max_kbps: int = 3000
    decrease_factor: float = 0.8
    recovery_factor: float = 1.1
    put_latency_high_ms: float = 80.0
    rtt_high_ms: float = 200.0
    put_latency_low_ms: float = 40.0
    rtt_low_ms: float = 100.0
    dwell_s: float = 5.0
    recovery_dwell_s: float = 30.0
    window_s: float = 1.0
    au_min_bytes: int = 16
    au_max_bytes: int = 1_048_576
    vehicle_ts_min: float = 1.0e6
    vehicle_ts_future_s: float = 10.0
    dedupe_cache_size: int = 1024
    camera_stale_s: float = 5.0


@dataclass
class StreamAppConfig:
    vehicle_id: str = "0"
    zenoh: ZenohConfig = field(default_factory=ZenohConfig)
    server: StreamServerConfig = field(default_factory=StreamServerConfig)
    telemetry: StreamTelemetryConfig = field(default_factory=StreamTelemetryConfig)
    video: VideoConfig = field(default_factory=VideoConfig)

    def validate(self) -> None:
        if not (0 < self.zenoh.tcp_port < 65536):
            raise ValueError(
                f"zenoh_tcp_port must be in (0, 65535], got {self.zenoh.tcp_port}"
            )
        if self.server.video_ping_rate <= 0:
            raise ValueError(
                f"video_ping_rate must be positive, got {self.server.video_ping_rate}"
            )
        if self.server.client_heartbeat_timeout <= 0:
            raise ValueError("client_heartbeat_timeout must be positive")
        if self.server.bot_disconnect_timeout <= 0:
            raise ValueError("bot_disconnect_timeout must be positive")
        if self.server.bw_calc_interval <= 0:
            raise ValueError("bw_calc_interval must be positive")

        v = self.video
        if v.min_kbps <= 0:
            raise ValueError(f"video.min_kbps must be positive, got {v.min_kbps}")
        if v.max_kbps < v.min_kbps:
            raise ValueError(
                f"video.max_kbps ({v.max_kbps}) must be >= min_kbps ({v.min_kbps})"
            )
        if not (v.min_kbps <= v.initial_kbps <= v.max_kbps):
            raise ValueError(
                f"video.initial_kbps ({v.initial_kbps}) must be within "
                f"[min_kbps={v.min_kbps}, max_kbps={v.max_kbps}]"
            )
        if not (0.0 < v.decrease_factor < 1.0):
            raise ValueError(
                f"video.decrease_factor must be in (0, 1), got {v.decrease_factor}"
            )
        if v.recovery_factor <= 1.0:
            raise ValueError(
                f"video.recovery_factor must be > 1.0, got {v.recovery_factor}"
            )
        if v.put_latency_high_ms <= v.put_latency_low_ms:
            raise ValueError(
                f"video.put_latency_high_ms ({v.put_latency_high_ms}) must be > "
                f"low ({v.put_latency_low_ms})"
            )
        if v.rtt_high_ms <= v.rtt_low_ms:
            raise ValueError(
                f"video.rtt_high_ms ({v.rtt_high_ms}) must be > rtt_low_ms "
                f"({v.rtt_low_ms})"
            )
        if v.dwell_s <= 0:
            raise ValueError("video.dwell_s must be positive")
        if v.recovery_dwell_s <= 0:
            raise ValueError("video.recovery_dwell_s must be positive")
        if v.window_s <= 0:
            raise ValueError("video.window_s must be positive")
        if v.au_min_bytes < 16:
            raise ValueError("video.au_min_bytes must be >= 16 (wire header size)")
        if v.au_max_bytes <= v.au_min_bytes:
            raise ValueError("video.au_max_bytes must be > au_min_bytes")
        if v.dedupe_cache_size <= 0:
            raise ValueError("video.dedupe_cache_size must be positive")
        if v.camera_stale_s <= 0:
            raise ValueError("video.camera_stale_s must be positive")


def _cast(value, expected_type, field_name: str):
    if not isinstance(value, expected_type):
        try:
            return expected_type(value)
        except (ValueError, TypeError):
            raise ValueError(
                f"config field '{field_name}' expected "
                f"{expected_type.__name__}, got {type(value).__name__}: {value!r}"
            )
    return value


def load_config(path: str, overrides: Dict[str, Any] = None) -> StreamAppConfig:
    cfg_dict: Dict[str, Any] = {}
    p = Path(path)
    if p.exists():
        with open(p, "r") as f:
            cfg_dict = yaml.safe_load(f) or {}

    if overrides:
        cfg_dict.update({k: v for k, v in overrides.items() if v is not None})

    vehicle_id = str(cfg_dict.get("vehicle_id", "0"))

    zenoh_cfg = ZenohConfig(
        tcp_port=_cast(cfg_dict.get("zenoh_tcp_port", 7457), int, "zenoh_tcp_port"),
    )

    server_cfg = StreamServerConfig(
        video_ping_rate=_cast(
            cfg_dict.get("video_ping_rate", 1.0), float, "video_ping_rate"
        ),
        client_heartbeat_timeout=_cast(
            cfg_dict.get("client_heartbeat_timeout", 3.0),
            float,
            "client_heartbeat_timeout",
        ),
        bot_disconnect_timeout=_cast(
            cfg_dict.get("bot_disconnect_timeout", 3.0),
            float,
            "bot_disconnect_timeout",
        ),
        bw_calc_interval=_cast(
            cfg_dict.get("bw_calc_interval", 1.0), float, "bw_calc_interval"
        ),
    )

    telemetry_cfg = StreamTelemetryConfig(
        camera_header_bytes=_cast(
            cfg_dict.get("camera_header_bytes", 16), int, "camera_header_bytes"
        ),
    )

    v_raw = cfg_dict.get("video", {}) or {}
    if not isinstance(v_raw, dict):
        raise ValueError(
            f"config field 'video' must be a mapping, got {type(v_raw).__name__}"
        )
    vd = VideoConfig()
    video_cfg = VideoConfig(
        enabled=_cast(v_raw.get("enabled", vd.enabled), bool, "video.enabled"),
        initial_kbps=_cast(
            v_raw.get("initial_kbps", vd.initial_kbps), int, "video.initial_kbps"
        ),
        min_kbps=_cast(v_raw.get("min_kbps", vd.min_kbps), int, "video.min_kbps"),
        max_kbps=_cast(v_raw.get("max_kbps", vd.max_kbps), int, "video.max_kbps"),
        decrease_factor=_cast(
            v_raw.get("decrease_factor", vd.decrease_factor),
            float,
            "video.decrease_factor",
        ),
        recovery_factor=_cast(
            v_raw.get("recovery_factor", vd.recovery_factor),
            float,
            "video.recovery_factor",
        ),
        put_latency_high_ms=_cast(
            v_raw.get("put_latency_high_ms", vd.put_latency_high_ms),
            float,
            "video.put_latency_high_ms",
        ),
        rtt_high_ms=_cast(
            v_raw.get("rtt_high_ms", vd.rtt_high_ms), float, "video.rtt_high_ms"
        ),
        put_latency_low_ms=_cast(
            v_raw.get("put_latency_low_ms", vd.put_latency_low_ms),
            float,
            "video.put_latency_low_ms",
        ),
        rtt_low_ms=_cast(
            v_raw.get("rtt_low_ms", vd.rtt_low_ms), float, "video.rtt_low_ms"
        ),
        dwell_s=_cast(v_raw.get("dwell_s", vd.dwell_s), float, "video.dwell_s"),
        recovery_dwell_s=_cast(
            v_raw.get("recovery_dwell_s", vd.recovery_dwell_s),
            float,
            "video.recovery_dwell_s",
        ),
        window_s=_cast(v_raw.get("window_s", vd.window_s), float, "video.window_s"),
        au_min_bytes=_cast(
            v_raw.get("au_min_bytes", vd.au_min_bytes), int, "video.au_min_bytes"
        ),
        au_max_bytes=_cast(
            v_raw.get("au_max_bytes", vd.au_max_bytes), int, "video.au_max_bytes"
        ),
        vehicle_ts_min=_cast(
            v_raw.get("vehicle_ts_min", vd.vehicle_ts_min),
            float,
            "video.vehicle_ts_min",
        ),
        vehicle_ts_future_s=_cast(
            v_raw.get("vehicle_ts_future_s", vd.vehicle_ts_future_s),
            float,
            "video.vehicle_ts_future_s",
        ),
        dedupe_cache_size=_cast(
            v_raw.get("dedupe_cache_size", vd.dedupe_cache_size),
            int,
            "video.dedupe_cache_size",
        ),
        camera_stale_s=_cast(
            v_raw.get("camera_stale_s", vd.camera_stale_s),
            float,
            "video.camera_stale_s",
        ),
    )

    cfg = StreamAppConfig(
        vehicle_id=vehicle_id,
        zenoh=zenoh_cfg,
        server=server_cfg,
        telemetry=telemetry_cfg,
        video=video_cfg,
    )
    cfg.validate()
    return cfg
