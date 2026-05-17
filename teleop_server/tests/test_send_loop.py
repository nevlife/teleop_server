"""run_send_loop disconnect→reconnect state machine (H1) — unit tests for
the `_update_vehicle_connectivity` helper.

Tests run synchronously without an asyncio loop or zenoh session: only the
helper is exercised. `time.monotonic` is bypassed by passing `now` directly.
"""

import logging
from unittest.mock import MagicMock

from main import _update_vehicle_connectivity
from teleop_server.state import SharedState


def _state_with_vehicle(vid: str, last_robot_recv: float) -> SharedState:
    state = SharedState()
    veh = state.get_vehicle(vid)
    veh.last_robot_recv = last_robot_recv
    return state


def _proto_mock():
    proto = MagicMock()
    proto.reset_dedupe = MagicMock()
    proto.release_vehicle_pubs = MagicMock()
    return proto


class TestDisconnectEdge:
    def test_emits_warning_and_calls_release_on_first_disconnect(self, caplog):
        # last_robot_recv was 5s ago, threshold 3s → disconnected.
        state = _state_with_vehicle("0", last_robot_recv=100.0)
        proto = _proto_mock()
        veh_disconnected: set[str] = set()

        with caplog.at_level(logging.WARNING, logger="main"):
            _update_vehicle_connectivity(
                state, proto, now=105.0,
                disconnect_timeout=3.0,
                veh_disconnected=veh_disconnected,
            )

        assert "0" in veh_disconnected
        proto.reset_dedupe.assert_called_once_with("0")
        proto.release_vehicle_pubs.assert_called_once_with("0")
        assert any("disconnected" in r.message for r in caplog.records)

    def test_no_duplicate_log_while_still_disconnected(self, caplog):
        state = _state_with_vehicle("0", last_robot_recv=100.0)
        proto = _proto_mock()
        veh_disconnected: set[str] = {"0"}  # already known-disconnected

        with caplog.at_level(logging.WARNING, logger="main"):
            _update_vehicle_connectivity(
                state, proto, now=110.0,
                disconnect_timeout=3.0,
                veh_disconnected=veh_disconnected,
            )

        proto.reset_dedupe.assert_not_called()
        proto.release_vehicle_pubs.assert_not_called()
        assert not any("disconnected" in r.message for r in caplog.records)


class TestReconnectEdge:
    def test_reconnect_clears_set_and_logs_once(self, caplog):
        # Vehicle was previously marked disconnected; now last_robot_recv
        # is fresh (age 0.2s) < disconnect_timeout=3.0 → reconnected.
        state = _state_with_vehicle("0", last_robot_recv=100.0)
        proto = _proto_mock()
        veh_disconnected: set[str] = {"0"}

        with caplog.at_level(logging.INFO, logger="main"):
            _update_vehicle_connectivity(
                state, proto, now=100.2,
                disconnect_timeout=3.0,
                veh_disconnected=veh_disconnected,
            )

        assert "0" not in veh_disconnected
        assert any("reconnected" in r.message for r in caplog.records)

    def test_steady_connected_no_log_no_set_growth(self, caplog):
        state = _state_with_vehicle("0", last_robot_recv=100.0)
        proto = _proto_mock()
        veh_disconnected: set[str] = set()

        with caplog.at_level(logging.INFO, logger="main"):
            _update_vehicle_connectivity(
                state, proto, now=100.5,
                disconnect_timeout=3.0,
                veh_disconnected=veh_disconnected,
            )

        assert veh_disconnected == set()
        # No transition → no log.
        assert not any(
            "disconnected" in r.message or "reconnected" in r.message
            for r in caplog.records
        )


class TestSetGrowthBounded:
    def test_stale_vid_purged_when_no_data(self):
        # Vehicle was disconnected but its last_robot_recv has been zeroed
        # (never received). Should be purged from veh_disconnected so the
        # set doesn't grow unboundedly on churn.
        state = SharedState()
        state.get_vehicle("0")  # last_robot_recv stays 0.0
        proto = _proto_mock()
        veh_disconnected: set[str] = {"0", "stale-vid-not-in-state"}

        _update_vehicle_connectivity(
            state, proto, now=10.0,
            disconnect_timeout=3.0,
            veh_disconnected=veh_disconnected,
        )

        # last_robot_recv == 0 → skipped (never connected) → considered
        # not live; the helper purges anything in the set whose vid is
        # not currently "live".
        assert "stale-vid-not-in-state" not in veh_disconnected
        assert "0" not in veh_disconnected


class TestMultipleVehiclesIndependent:
    def test_disconnect_one_reconnect_other(self, caplog):
        state = SharedState()
        state.get_vehicle("a").last_robot_recv = 90.0  # age 15s → disc
        state.get_vehicle("b").last_robot_recv = 104.5  # age 0.5s → conn
        proto = _proto_mock()
        veh_disconnected: set[str] = {"b"}  # b was previously disconnected

        with caplog.at_level(logging.INFO, logger="main"):
            _update_vehicle_connectivity(
                state, proto, now=105.0,
                disconnect_timeout=3.0,
                veh_disconnected=veh_disconnected,
            )

        assert "a" in veh_disconnected
        assert "b" not in veh_disconnected
        proto.reset_dedupe.assert_called_once_with("a")
        proto.release_vehicle_pubs.assert_called_once_with("a")
        msgs = [r.message for r in caplog.records]
        assert any("a" in m and "disconnected" in m for m in msgs)
        assert any("b" in m and "reconnected" in m for m in msgs)
