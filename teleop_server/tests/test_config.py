import os
import tempfile
import pytest
import yaml

from teleop_server.config.schema import (
    AppConfig,
    ZenohConfig,
    ServerConfig,
    TelemetryConfig,
)
from teleop_server.config.loader import load_config


class TestSchemaDefaults:
    def test_zenoh_default(self):
        # teleop_server listens on TCP 7447. UDP listener was removed.
        c = ZenohConfig()
        assert c.tcp_port == 7447
        assert not hasattr(c, "udp_port")

    def test_server_defaults(self):
        c = ServerConfig()
        assert c.telemetry_rate == 2.0
        assert c.station_timeout == 3.0

    def test_telemetry_defaults(self):
        c = TelemetryConfig()
        assert c.disconnect_timeout == 3.0
        assert c.bw_calc_interval == 1.0
        assert c.seq_max == 65536
        assert set(c.control_keys) == {"mux", "twist", "network", "estop"}

    def test_telemetry_control_keys_set(self):
        c = TelemetryConfig(control_keys=["mux", "twist"])
        assert c.control_keys_set == frozenset({"mux", "twist"})


class TestAppConfigValidation:
    def test_valid_config_passes(self):
        cfg = AppConfig()
        cfg.validate()

    def test_zero_tcp_port(self):
        cfg = AppConfig(zenoh=ZenohConfig(tcp_port=0))
        with pytest.raises(ValueError, match="zenoh_tcp_port"):
            cfg.validate()

    def test_zero_telemetry_rate(self):
        cfg = AppConfig(server=ServerConfig(telemetry_rate=0))
        with pytest.raises(ValueError, match="telemetry_rate"):
            cfg.validate()

    def test_negative_station_timeout(self):
        cfg = AppConfig(server=ServerConfig(station_timeout=-1))
        with pytest.raises(ValueError, match="station_timeout"):
            cfg.validate()

    def test_negative_disconnect_timeout(self):
        cfg = AppConfig(telemetry=TelemetryConfig(disconnect_timeout=-1))
        with pytest.raises(ValueError, match="disconnect_timeout"):
            cfg.validate()

    def test_zero_bw_calc_interval(self):
        cfg = AppConfig(telemetry=TelemetryConfig(bw_calc_interval=0))
        with pytest.raises(ValueError, match="bw_calc_interval"):
            cfg.validate()

    def test_negative_seq_max(self):
        cfg = AppConfig(telemetry=TelemetryConfig(seq_max=-1))
        with pytest.raises(ValueError, match="seq_max"):
            cfg.validate()


class TestNoVideoConfig:
    """teleop_server config must not contain a VideoConfig."""

    def test_video_not_importable(self):
        from teleop_server.config import schema
        assert not hasattr(schema, "VideoConfig")

    def test_video_not_in_loader(self):
        # A video section in the YAML must be silently ignored.
        data = {
            "zenoh_tcp_port": 7447,
            "video": {"transport_mode": "tcp_stale"},
        }
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            yaml.dump(data, f)
            f.flush()
            cfg = load_config(f.name)
        os.unlink(f.name)
        # AppConfig has no `video` field.
        assert not hasattr(cfg, "video")


class TestLoader:
    def test_load_from_yaml(self):
        data = {
            "zenoh_tcp_port": 8447,
            "disconnect_timeout": 5.0,
            "control_keys": ["mux", "estop"],
        }
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            yaml.dump(data, f)
            f.flush()
            cfg = load_config(f.name)
        os.unlink(f.name)

        assert cfg.zenoh.tcp_port == 8447
        assert cfg.telemetry.disconnect_timeout == 5.0
        assert cfg.telemetry.control_keys == ["mux", "estop"]

    def test_load_nonexistent_file_uses_defaults(self):
        cfg = load_config("/tmp/does_not_exist_12345_teleop.yaml")
        assert cfg.zenoh.tcp_port == 7447
