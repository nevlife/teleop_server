"""SharedState / VehicleState — telemetry-only multi-vehicle state model."""

import json
import time

from teleop_server.config.schema import TelemetryConfig
from teleop_server.state import SharedState


def _state(control_keys=None):
    cfg = TelemetryConfig(control_keys=control_keys) if control_keys else None
    return SharedState(cfg)


class TestVehicleLookup:
    def test_get_vehicle_creates_once(self):
        s = _state()
        v1 = s.get_vehicle("0")
        v2 = s.get_vehicle("0")
        assert v1 is v2
        assert "0" in s.vehicles

    def test_separate_vehicles_isolated(self):
        s = _state()
        s.get_vehicle("0").update_packet("mux", {"requested_mode": 2})
        s.get_vehicle("1").update_packet("mux", {"requested_mode": 5})
        assert s.vehicles["0"].mux.requested_mode == 2
        assert s.vehicles["1"].mux.requested_mode == 5


class TestUpdatePacket:
    def test_mux_fields(self):
        v = _state().get_vehicle("0")
        v.update_packet("mux", {"requested_mode": 2, "remote_enabled": True})
        assert v.mux.requested_mode == 2
        assert v.mux.remote_enabled is True

    def test_unknown_field_ignored(self):
        v = _state().get_vehicle("0")
        v.update_packet("mux", {"nonexistent_field": 99})
        assert not hasattr(v.mux, "nonexistent_field")

    def test_unknown_section_no_op(self):
        v = _state().get_vehicle("0")
        v.update_packet("nonexistent", {"foo": 1})

    def test_control_key_touches_last_control_recv(self):
        v = _state().get_vehicle("0")
        assert v.last_control_recv == 0.0
        v.update_packet("mux", {"requested_mode": 0})
        assert v.last_control_recv > 0

    def test_non_control_key_does_not_touch(self):
        s = _state(control_keys=["mux"])
        v = s.get_vehicle("0")
        v.update_packet("resources", {"cpu_usage": 50.0})
        assert v.last_control_recv == 0.0


class TestListUpdates:
    def test_gpu_extends(self):
        v = _state().get_vehicle("0")
        v.update_gpu(2, {"gpu_usage": 50.0})
        assert len(v.gpu_list) == 3
        assert v.gpu_list[2]["gpu_usage"] == 50.0

    def test_disk_partition(self):
        v = _state().get_vehicle("0")
        v.update_disk_partition(0, {"mountpoint": "/", "percent": 45.0})
        assert v.disk_partitions[0]["percent"] == 45.0

    def test_net_interface(self):
        v = _state().get_vehicle("0")
        v.update_net_interface(0, {"name": "eth0", "is_up": True})
        assert v.net_interfaces[0]["name"] == "eth0"


class TestNoVideoFields:
    """All video-related state has moved to stream_server."""

    def test_no_cameras_attribute(self):
        v = _state().get_vehicle("0")
        assert not hasattr(v, "cameras")

    def test_no_video_stats_attribute(self):
        v = _state().get_vehicle("0")
        assert not hasattr(v, "video_stats")

    def test_no_touch_camera_method(self):
        v = _state().get_vehicle("0")
        assert not hasattr(v, "touch_camera")
        assert not hasattr(v, "prune_cameras")

    def test_network_no_video_fields(self):
        v = _state().get_vehicle("0")
        assert not hasattr(v.network, "bw_video_rx")
        assert not hasattr(v.network, "bw_video_tx")
        assert not hasattr(v.network, "encode_delay")
        assert not hasattr(v.network, "video_net_delay")
        assert not hasattr(v.network, "video_loss_pct")
        assert not hasattr(v.network, "relay_max_ms")


class TestValidation:
    def test_estop_moving_alert(self):
        s = _state()
        v = s.get_vehicle("0")
        v.estop.is_estop = True
        v.twist.final_lx = 1.0
        s.validate()
        assert any(a.level == "error" and "E-STOP" in a.message for a in s.alerts)

    def test_no_robot_data_alert(self):
        s = _state()
        v = s.get_vehicle("0")
        v.last_control_recv = time.monotonic() - 10.0
        s.validate()
        assert any(a.level == "error" and "No robot data" in a.message for a in s.alerts)

    def test_station_disconnected_alert(self):
        s = _state()
        s.validate()
        assert any("Station" in a.message for a in s.alerts)


class TestToJson:
    def test_top_level_shape(self):
        s = _state()
        s.get_vehicle("0")
        data = json.loads(s.to_json())
        for k in ("vehicles", "station_connected", "control", "alerts", "server_time"):
            assert k in data
        # Envelope fields added by make_envelope.
        assert "v" in data and isinstance(data["v"], int)
        assert "ts" in data and isinstance(data["ts"], (int, float))
        assert "0" in data["vehicles"]

    def test_vehicle_shape(self):
        s = _state()
        s.get_vehicle("0")
        v = json.loads(s.to_json())["vehicles"]["0"]
        for k in (
            "vehicle_id", "mux", "twist", "network", "estop", "resources",
            "gpu_list", "disk_partitions", "net_interfaces", "remote_enabled",
            "robot_age",
        ):
            assert k in v
        # video_stats is absent.
        assert "video_stats" not in v

    def test_robot_age_negative_when_no_recv(self):
        s = _state()
        s.get_vehicle("0")
        assert json.loads(s.to_json())["vehicles"]["0"]["robot_age"] == -1
