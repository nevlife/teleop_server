from __future__ import annotations

import re
from typing import Any


PROTOCOL_VERSION = 2
_ROBOT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
SIGNAL_TYPES = frozenset({"offer", "answer", "ice"})


class ProtocolError(ValueError):
    pass


def parse_hello(data: Any) -> tuple[str, str]:
    if not isinstance(data, dict):
        raise ProtocolError("hello must be a JSON object")
    if data.get("type") != "hello":
        raise ProtocolError("first message must be hello")
    if data.get("protocol_version") != PROTOCOL_VERSION:
        raise ProtocolError("unsupported protocol_version")
    role = data.get("role")
    if role not in ("rover", "client"):
        raise ProtocolError("role must be rover or client")
    robot_id = data.get("robot_id")
    if not isinstance(robot_id, str) or not _ROBOT_ID_RE.fullmatch(robot_id):
        raise ProtocolError("invalid robot_id")
    return role, robot_id


def validate_signal(data: Any) -> dict:
    if not isinstance(data, dict):
        raise ProtocolError("message must be a JSON object")
    kind = data.get("type")
    if kind not in SIGNAL_TYPES:
        raise ProtocolError(f"unsupported signaling type: {kind!r}")
    if kind in ("offer", "answer"):
        sdp = data.get("sdp")
        if not isinstance(sdp, str) or not sdp or len(sdp) > 1024 * 1024:
            raise ProtocolError("sdp must be a non-empty string <= 1MiB")
    else:
        candidate = data.get("candidate")
        if not isinstance(candidate, dict):
            raise ProtocolError("candidate must be an object")
    return data
