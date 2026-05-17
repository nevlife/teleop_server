import asyncio
import json
import math
import time
from unittest.mock import MagicMock

from teleop_server.state import SharedState
from teleop_server.station_bridge import StationBridge


def make_bridge():
    loop = asyncio.new_event_loop()
    state = SharedState()
    proto = MagicMock()
    bridge = StationBridge(state, loop, proto)
    return bridge, state, proto, loop


class TestTeleop:
    def _make_sample(self, data):
        sample = MagicMock()
        sample.payload = json.dumps(data).encode()
        return sample

    def test_passthrough(self):
        bridge, state, proto, _ = make_bridge()
        state.station_connected = True
        bridge._loop = MagicMock()
        bridge._loop.call_soon_threadsafe = lambda fn, *a: fn(*a)
        bridge._handle_teleop(
            "0", self._make_sample({"linear_x": 1.0, "steer_angle": 0.3}).payload
        )
        proto.send_teleop.assert_called_once_with("0", 1.0, 0.3)

    def test_not_connected_no_send(self):
        bridge, state, proto, _ = make_bridge()
        state.station_connected = False
        bridge._handle_teleop(
            "0", self._make_sample({"linear_x": 1.0, "steer_angle": 0.0}).payload
        )
        proto.send_teleop.assert_not_called()


class TestEstop:
    def test_estop_forwarded(self):
        bridge, state, proto, _ = make_bridge()
        bridge._loop = MagicMock()
        bridge._loop.call_soon_threadsafe = lambda fn, *a: fn(*a)
        bridge._handle_estop("0", json.dumps({"active": True}).encode())
        proto.send_estop.assert_called_once_with("0", True)
        assert state.control.estop is True


class TestCmdMode:
    def test_cmd_mode_forwarded(self):
        bridge, state, proto, _ = make_bridge()
        bridge._loop = MagicMock()
        bridge._loop.call_soon_threadsafe = lambda fn, *a: fn(*a)
        bridge._handle_cmd_mode("0", json.dumps({"mode": 2}).encode())
        proto.send_cmd_mode.assert_called_once_with("0", 2)
        assert state.control.mode == 2


class TestClientHeartbeat:
    def test_client_hb_marks_station_connected(self):
        bridge, state, _, _ = make_bridge()
        bridge._loop = MagicMock()
        bridge._loop.call_soon_threadsafe = lambda fn, *a: fn(*a)
        assert state.station_connected is False
        bridge._handle_client_hb()
        assert state.station_connected is True
        assert state.station_last_recv > 0


class TestControllerHeartbeat:
    def test_controller_hb_updates_joystick(self):
        bridge, state, _, _ = make_bridge()
        bridge._loop = MagicMock()
        bridge._loop.call_soon_threadsafe = lambda fn, *a: fn(*a)
        bridge._handle_controller_hb(json.dumps({"connected": True}).encode())
        assert state.control.joystick_connected is True

    def test_controller_hb_disconnect(self):
        bridge, state, _, _ = make_bridge()
        bridge._loop = MagicMock()
        bridge._loop.call_soon_threadsafe = lambda fn, *a: fn(*a)
        state.control.joystick_connected = True
        bridge._handle_controller_hb(json.dumps({"connected": False}).encode())
        assert state.control.joystick_connected is False


class TestNoVideoHandlers:
    """teleop_server.StationBridge has no video-related handlers."""

    def test_no_video_ctl_handler(self):
        bridge, *_ = make_bridge()
        assert not hasattr(bridge, "_relay_video_ctl")

    def test_no_rtx_handler(self):
        bridge, *_ = make_bridge()
        assert not hasattr(bridge, "_relay_rtx_request")

    def test_no_video_feedback_handler(self):
        bridge, *_ = make_bridge()
        assert not hasattr(bridge, "_handle_video_feedback")

    def test_video_topics_not_in_handled(self):
        for forbidden in ("video_ctl", "video_feedback", "rtx_request",
                          "stream_heartbeat"):
            assert forbidden not in StationBridge._HANDLED_TOPICS


class TestOnTeleopFiltering:
    """`_on_teleop` must ignore any topic outside `_HANDLED_TOPICS` and
    must NOT mutate state (auto-creating a VehicleState would be a bug)."""

    def _sample(self, key_expr: str, payload: bytes = b""):
        sample = MagicMock()
        sample.key_expr = key_expr
        sample.payload = payload
        return sample

    def _make_bridge_sync(self):
        bridge, state, proto, _ = make_bridge()
        bridge._loop = MagicMock()
        bridge._loop.call_soon_threadsafe = lambda fn, *a: fn(*a)
        return bridge, state, proto

    def test_unhandled_topic_no_state_mutation(self):
        bridge, state, proto = self._make_bridge_sync()
        # `telemetry` is server-published; if station bridge ever sees it
        # via a misconfigured subscriber, it must be a no-op.
        bridge._on_teleop(self._sample("nev/teleop/0/telemetry"))
        assert "0" not in state.vehicles
        proto.send_teleop.assert_not_called()
        proto.send_estop.assert_not_called()
        proto.send_cmd_mode.assert_not_called()

    def test_cmd_topic_ignored(self):
        # `cmd` is server-published. Must not be mistaken for a client teleop.
        bridge, state, proto = self._make_bridge_sync()
        bridge._on_teleop(
            self._sample("nev/teleop/0/cmd",
                         json.dumps({"linear_x": 1.0, "steer_angle": 0.0}).encode())
        )
        assert "0" not in state.vehicles
        proto.send_teleop.assert_not_called()

    def test_estop_cmd_topic_ignored(self):
        bridge, state, proto = self._make_bridge_sync()
        bridge._on_teleop(
            self._sample("nev/teleop/0/estop_cmd",
                         json.dumps({"active": True}).encode())
        )
        assert "0" not in state.vehicles
        proto.send_estop.assert_not_called()

    def test_handled_topic_creates_vehicle_state(self):
        bridge, state, proto = self._make_bridge_sync()
        state.station_connected = True
        bridge._on_teleop(
            self._sample("nev/teleop/9/teleop",
                         json.dumps({"linear_x": 0.5, "steer_angle": 0.1}).encode())
        )
        assert "9" in state.vehicles
        proto.send_teleop.assert_called_once()

    def test_heartbeat_does_not_create_vehicle_state(self):
        # client_heartbeat / controller_heartbeat are station-wide; must
        # not auto-create a per-vehicle entry even though they appear in
        # _HANDLED_TOPICS.
        bridge, state, proto = self._make_bridge_sync()
        bridge._on_teleop(self._sample("nev/teleop/0/client_heartbeat", b""))
        bridge._on_teleop(
            self._sample("nev/teleop/0/controller_heartbeat",
                         json.dumps({"connected": True}).encode())
        )
        assert "0" not in state.vehicles


class TestNoLegacyTopics:
    """No handlers for legacy topics (server_ping/station_ping/bot_ping/heartbeat)."""

    def test_no_legacy_handlers(self):
        bridge, *_ = make_bridge()
        assert not hasattr(bridge, "_handle_station_ping")
        assert not hasattr(bridge, "_relay_bot_ping")
        assert not hasattr(bridge, "_handle_joystick")

    def test_handled_topics_only_new(self):
        expected = {
            "teleop", "estop", "cmd_mode",
            "client_heartbeat", "controller_heartbeat",
        }
        assert StationBridge._HANDLED_TOPICS == expected
