from pathlib import Path
from typing import Dict, Any
import yaml

from .schema import AppConfig, ZenohConfig, ServerConfig, TelemetryConfig, MetricsConfig


def _cast(value, expected_type, field_name: str):
    if not isinstance(value, expected_type):
        try:
            return expected_type(value)
        except (ValueError, TypeError):
            raise ValueError(
                f"config field '{field_name}' expected {expected_type.__name__}, "
                f"got {type(value).__name__}: {value!r}"
            )
    return value


def load_config(path: str, overrides: Dict[str, Any] = None) -> AppConfig:
    cfg_dict = {}
    p = Path(path)

    if p.exists():
        with open(p, "r") as f:
            cfg_dict = yaml.safe_load(f) or {}

    if overrides:
        # NOTE: overrides are flat-merged (top-level keys only). The YAML
        # schema is currently flat too, so this works. If config.yaml is
        # ever made nested (e.g. `server: { station_timeout: ... }`), this
        # update() will clobber whole sections instead of merging keys —
        # switch to a recursive merge here at that point.
        cfg_dict.update({k: v for k, v in overrides.items() if v is not None})

    zenoh_cfg = ZenohConfig(
        tcp_port=_cast(cfg_dict.get("zenoh_tcp_port", 7447), int, "zenoh_tcp_port"),
    )

    server_cfg = ServerConfig(
        telemetry_rate=_cast(cfg_dict.get("telemetry_rate", 2.0), float, "telemetry_rate"),
        station_timeout=_cast(cfg_dict.get("station_timeout", 3.0), float, "station_timeout"),
    )

    raw_keys = cfg_dict.get("control_keys", ["mux", "twist", "network", "estop"])
    if not isinstance(raw_keys, list) or not all(isinstance(k, str) for k in raw_keys):
        raise ValueError(
            f"config field 'control_keys' must be a list of strings, got {raw_keys!r}"
        )

    telemetry_cfg = TelemetryConfig(
        control_keys=raw_keys,
        disconnect_timeout=_cast(
            cfg_dict.get("disconnect_timeout", 3.0), float, "disconnect_timeout"
        ),
        bw_calc_interval=_cast(cfg_dict.get("bw_calc_interval", 1.0), float, "bw_calc_interval"),
        seq_max=_cast(cfg_dict.get("seq_max", 65536), int, "seq_max"),
    )

    # `metrics:` is the first nested section in the YAML schema. Read it
    # defensively — an absent or non-dict value falls back to defaults
    # rather than raising (matches the rest of the loader's behaviour).
    raw_metrics = cfg_dict.get("metrics") or {}
    if not isinstance(raw_metrics, dict):
        raise ValueError(
            f"config field 'metrics' must be a mapping, got {type(raw_metrics).__name__}"
        )
    metrics_cfg = MetricsConfig(
        enabled=_cast(raw_metrics.get("enabled", True), bool, "metrics.enabled"),
        bind=_cast(raw_metrics.get("bind", "127.0.0.1"), str, "metrics.bind"),
        port=_cast(raw_metrics.get("port", 8080), int, "metrics.port"),
    )

    config = AppConfig(
        zenoh=zenoh_cfg,
        server=server_cfg,
        telemetry=telemetry_cfg,
        metrics=metrics_cfg,
    )

    config.validate()

    return config
