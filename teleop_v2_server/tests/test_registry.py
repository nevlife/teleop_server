import pytest

from teleop_v2_server.registry import PeerRole, SessionConflict, SessionRegistry


def test_one_rover_one_client_and_session_rotation():
    registry = SessionRegistry()
    rover_ws, client_ws = object(), object()
    rover = registry.attach(PeerRole.ROVER, "rover-1", rover_ws)
    assert registry.session_info("rover-1") is None

    with pytest.raises(SessionConflict):
        registry.attach(PeerRole.ROVER, "rover-1", object())

    client = registry.attach(PeerRole.CLIENT, "rover-1", client_ws)
    first = registry.session_info("rover-1")
    assert first and first["session_id"] and first["session_epoch"] > 0
    assert registry.counterpart(rover) is client
    assert registry.counterpart(client) is rover

    registry.detach(client)
    assert registry.session_info("rover-1") is None
    replacement = registry.attach(PeerRole.CLIENT, "rover-1", object())
    second = registry.session_info("rover-1")
    assert second and second != first
    assert registry.counterpart(replacement) is rover


def test_empty_slot_is_pruned():
    registry = SessionRegistry()
    peer = registry.attach("client", "rover-1", object())
    registry.detach(peer)
    assert registry.snapshot() == {}
