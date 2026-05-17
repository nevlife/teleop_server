"""RobotProtocol — telemetry-only multi-vehicle sequence/bandwidth/send tests."""

import json
import time
from unittest.mock import MagicMock

import pytest

from teleop_server.config.schema import TelemetryConfig
from teleop_server.state import SharedState
from teleop_server.robot_bridge import RobotProtocol


def _proto(telemetry_cfg=None):
    """Dummy loop runs callbacks synchronously, immediately. No zenoh session."""
    loop = MagicMock()
    loop.call_soon_threadsafe = lambda fn, *a: fn(*a)
    state = SharedState(telemetry_cfg)
    proto = RobotProtocol(state, loop, telemetry_cfg)
    proto._session = MagicMock()
    proto._session.declare_publisher = lambda *a, **kw: MagicMock()
    return proto, state


class TestSeqCounter:
    def test_increments(self):
        p, _ = _proto()
        assert p._next_seq() == 0
        assert p._next_seq() == 1
        assert p._next_seq() == 2

    def test_wraps(self):
        p, _ = _proto(TelemetryConfig(seq_max=4))
        for _ in range(4):
            p._next_seq()
        assert p._next_seq() == 0


class TestBandwidth:
    def test_telemetry_bandwidth(self):
        p, state = _proto()
        p._add_tele_bytes("0", 100_000)
        p._bw_ts = time.monotonic() - 1.0
        p.calc_bandwidth()
        veh = state.vehicles["0"]
        assert veh.network.bw_telemetry > 0

    def test_no_calc_before_interval(self):
        p, state = _proto()
        p._add_tele_bytes("0", 100_000)
        p._bw_ts = time.monotonic()
        p.calc_bandwidth()
        assert "0" not in state.vehicles


class TestSendMethods:
    def test_teleop_payload(self):
        p, _ = _proto()
        pub = MagicMock()
        p._veh_pubs["0"] = {"cmd": pub}
        p.send_teleop("0", 1.5, 0.3)
        data = json.loads(pub.put.call_args[0][0])
        assert data["linear_x"] == 1.5
        assert data["steer_angle"] == 0.3
        assert "seq" in data
        assert "v" in data and "ts" in data

    def test_estop_payload(self):
        p, _ = _proto()
        pub = MagicMock()
        p._veh_pubs["0"] = {"estop_cmd": pub}
        p.send_estop("0", True)
        data = json.loads(pub.put.call_args[0][0])
        assert data["active"] is True
        assert "v" in data and "ts" in data

    def test_cmd_mode_payload(self):
        p, _ = _proto()
        pub = MagicMock()
        p._veh_pubs["0"] = {"cmd_mode_bot": pub}
        p.send_cmd_mode("0", 2)
        data = json.loads(pub.put.call_args[0][0])
        assert data["mode"] == 2
        assert "v" in data and "ts" in data

    def test_publish_exception_no_crash(self):
        p, _ = _proto()
        pub = MagicMock()
        pub.put.side_effect = RuntimeError("connection lost")
        p._veh_pubs["0"] = {"cmd": pub}
        p.send_teleop("0", 0.0, 0.0)


class TestBotHeartbeat:
    def test_heartbeat_touches_last_recv(self):
        p, state = _proto()
        before = time.monotonic()
        p._handle_bot_heartbeat("0")
        veh = state.vehicles["0"]
        assert veh.last_robot_recv >= before


class TestTelemetryPrefix:
    def test_subscribes_to_teleop_prefix(self):
        # Subscribes only to nev/teleop/** (not nev/robot or nev/stream).
        loop = MagicMock()
        loop.call_soon_threadsafe = lambda fn, *a: fn(*a)
        state = SharedState()
        proto = RobotProtocol(state, loop, None)
        captured_keys = []
        sess = MagicMock()
        sess.declare_subscriber = lambda key, cb: (captured_keys.append(key), MagicMock())[1]
        sess.declare_publisher = lambda *a, **kw: MagicMock()
        proto.start(sess)
        assert any("teleop" in k for k in captured_keys)
        assert not any("nev/robot" in k for k in captured_keys)
        assert not any("nev/stream" in k for k in captured_keys)

    def test_publish_uses_teleop_prefix(self):
        loop = MagicMock()
        loop.call_soon_threadsafe = lambda fn, *a: fn(*a)
        state = SharedState()
        proto = RobotProtocol(state, loop, None)
        captured = []

        def _declare(key, **kwargs):
            captured.append(key)
            return MagicMock()

        proto._session = MagicMock()
        proto._session.declare_publisher = _declare
        proto._get_pub("0", "teleop")
        assert captured == ["nev/teleop/0/teleop"]


class TestNoVideoMethods:
    """teleop_server.RobotProtocol has no video-related methods."""

    def test_no_camera_handlers(self):
        p, _ = _proto()
        assert not hasattr(p, "_handle_camera")
        assert not hasattr(p, "_handle_video_stats")

    def test_no_video_send_methods(self):
        p, _ = _proto()
        assert not hasattr(p, "send_video_bitrate")
        assert not hasattr(p, "send_video_ping")

    def test_no_dedupe_state(self):
        p, _ = _proto()
        assert not hasattr(p, "_veh_dedupe")
        assert not hasattr(p, "_veh_cam_bytes")


class TestNoServerRtt:
    """RTT only passes through between client and bot; the server does not measure it."""

    def test_no_server_ping_method(self):
        p, _ = _proto()
        assert not hasattr(p, "send_server_ping")

    def test_no_server_pong_handler(self):
        p, _ = _proto()
        assert not hasattr(p, "_handle_server_pong")

    def test_no_bot_pong_relay(self):
        p, _ = _proto()
        assert not hasattr(p, "_relay_bot_pong")

    def test_no_rtt_field(self):
        p, state = _proto()
        veh = state.get_vehicle("0")
        assert not hasattr(veh.network, "rtt_server_bot_ms")
