from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import secrets
import uuid
from typing import Any


class PeerRole(StrEnum):
    ROVER = "rover"
    CLIENT = "client"


class SessionConflict(RuntimeError):
    pass


@dataclass(eq=False)
class Peer:
    role: PeerRole
    robot_id: str
    websocket: Any


@dataclass
class SessionSlot:
    rover: Peer | None = None
    client: Peer | None = None
    session_id: str | None = None
    session_epoch: int = 0

    @property
    def paired(self) -> bool:
        return self.rover is not None and self.client is not None

    def rotate_session(self) -> None:
        self.session_id = str(uuid.uuid4())
        self.session_epoch = secrets.randbits(63) or 1

    def invalidate_session(self) -> None:
        self.session_id = None
        self.session_epoch = 0


class SessionRegistry:
    """Single-event-loop registry enforcing one rover and one driver."""

    def __init__(self) -> None:
        self._slots: dict[str, SessionSlot] = {}

    def attach(self, role: PeerRole | str, robot_id: str, websocket: Any) -> Peer:
        peer = Peer(PeerRole(role), robot_id, websocket)
        slot = self._slots.setdefault(robot_id, SessionSlot())
        attr = peer.role.value
        if getattr(slot, attr) is not None:
            raise SessionConflict(f"{peer.role.value} already connected for {robot_id}")
        setattr(slot, attr, peer)
        if slot.paired:
            slot.rotate_session()
        return peer

    def detach(self, peer: Peer) -> None:
        slot = self._slots.get(peer.robot_id)
        if slot is None:
            return
        attr = peer.role.value
        if getattr(slot, attr) is peer:
            setattr(slot, attr, None)
            slot.invalidate_session()
        if slot.rover is None and slot.client is None:
            self._slots.pop(peer.robot_id, None)

    def counterpart(self, peer: Peer) -> Peer | None:
        slot = self._slots.get(peer.robot_id)
        if slot is None:
            return None
        return slot.client if peer.role is PeerRole.ROVER else slot.rover

    def peers(self, robot_id: str) -> tuple[Peer, Peer] | None:
        slot = self._slots.get(robot_id)
        if slot is None or not slot.paired:
            return None
        assert slot.rover is not None and slot.client is not None
        return slot.rover, slot.client

    def session_info(self, robot_id: str) -> dict | None:
        slot = self._slots.get(robot_id)
        if slot is None or not slot.paired:
            return None
        return {
            "session_id": slot.session_id,
            "session_epoch": slot.session_epoch,
        }

    def snapshot(self) -> dict:
        return {
            robot_id: {
                "rover": slot.rover is not None,
                "client": slot.client is not None,
                "paired": slot.paired,
            }
            for robot_id, slot in self._slots.items()
        }
