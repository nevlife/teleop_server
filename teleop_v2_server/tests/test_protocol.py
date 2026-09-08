import pytest

from teleop_v2_server.protocol import ProtocolError, parse_hello, validate_signal


def test_parse_hello():
    assert parse_hello(
        {"type": "hello", "protocol_version": 2, "role": "rover", "robot_id": "r-1"}
    ) == ("rover", "r-1")


@pytest.mark.parametrize(
    "message",
    [
        {},
        {"type": "hello", "protocol_version": 1, "role": "rover", "robot_id": "r"},
        {"type": "hello", "protocol_version": 2, "role": "admin", "robot_id": "r"},
        {"type": "hello", "protocol_version": 2, "role": "client", "robot_id": "../r"},
    ],
)
def test_bad_hello(message):
    with pytest.raises(ProtocolError):
        parse_hello(message)


def test_validate_signals():
    assert validate_signal({"type": "offer", "sdp": "v=0"})["type"] == "offer"
    assert validate_signal({"type": "ice", "candidate": {"candidate": "x"}})["type"] == "ice"
    with pytest.raises(ProtocolError):
        validate_signal({"type": "control", "payload": {}})
