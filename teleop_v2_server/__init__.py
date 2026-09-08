"""Teleop v2 signaling service.

The media plane is handled by TURN and remains encoded end to end. This
package only pairs one rover with one native operator and relays SDP/ICE.
"""

from .registry import PeerRole, SessionConflict, SessionRegistry

__all__ = ["PeerRole", "SessionConflict", "SessionRegistry", "create_app"]


def create_app(config=None):
    """Lazy import keeps protocol/registry tooling usable without aiohttp."""
    from .app import create_app as _create_app

    return _create_app(config)
