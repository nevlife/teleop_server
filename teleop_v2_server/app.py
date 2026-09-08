from __future__ import annotations

import asyncio
import json
import logging

from aiohttp import WSMsgType, web

from .config import ServerConfig
from .protocol import ProtocolError, parse_hello, validate_signal
from .registry import Peer, PeerRole, SessionConflict, SessionRegistry


logger = logging.getLogger(__name__)
REGISTRY_KEY = web.AppKey("registry", SessionRegistry)
CONFIG_KEY = web.AppKey("config", ServerConfig)


async def _send_peer_ready(app: web.Application, robot_id: str) -> None:
    registry = app[REGISTRY_KEY]
    pair = registry.peers(robot_id)
    info = registry.session_info(robot_id)
    if pair is None or info is None:
        return
    rover, client = pair
    base = {
        "type": "peer_ready",
        "robot_id": robot_id,
        "session_id": info["session_id"],
        # JSON numbers cannot represent all uint64 values exactly. Keep the
        # protobuf epoch numeric on the data channel, but signal it as text.
        "session_epoch": str(info["session_epoch"]),
    }
    await rover.websocket.send_json({**base, "peer_role": "client", "create_offer": True})
    await client.websocket.send_json({**base, "peer_role": "rover", "create_offer": False})


async def healthz(request: web.Request) -> web.Response:
    snapshot = request.app[REGISTRY_KEY].snapshot()
    return web.json_response(
        {
            "ok": True,
            "protocol_version": 2,
            "robots": snapshot,
            "paired_count": sum(1 for item in snapshot.values() if item["paired"]),
        }
    )


async def websocket_handler(request: web.Request) -> web.StreamResponse:
    cfg = request.app[CONFIG_KEY]
    registry = request.app[REGISTRY_KEY]
    ws = web.WebSocketResponse(max_msg_size=cfg.max_message_bytes, heartbeat=10.0)
    await ws.prepare(request)

    peer: Peer | None = None
    try:
        first = await asyncio.wait_for(ws.receive(), timeout=cfg.hello_timeout_s)
        if first.type != WSMsgType.TEXT:
            raise ProtocolError("first websocket frame must be text JSON")
        try:
            hello = json.loads(first.data)
        except json.JSONDecodeError as exc:
            raise ProtocolError("hello is not valid JSON") from exc
        role_text, robot_id = parse_hello(hello)
        try:
            peer = registry.attach(PeerRole(role_text), robot_id, ws)
        except SessionConflict as exc:
            await ws.send_json({"type": "error", "code": "role_in_use", "detail": str(exc)})
            await ws.close(code=4009, message=b"role already connected")
            return ws

        await ws.send_json(
            {
                "type": "hello_ack",
                "protocol_version": 2,
                "role": peer.role.value,
                "robot_id": robot_id,
                "turn": {
                    "urls": [cfg.turn_url],
                    "username": cfg.turn_username,
                    "credential": cfg.turn_password,
                },
            }
        )
        await _send_peer_ready(request.app, robot_id)
        logger.info("%s connected for %s", peer.role.value, robot_id)

        async for message in ws:
            if message.type == WSMsgType.TEXT:
                try:
                    payload = validate_signal(json.loads(message.data))
                except (json.JSONDecodeError, ProtocolError) as exc:
                    await ws.send_json(
                        {"type": "error", "code": "bad_signal", "detail": str(exc)}
                    )
                    continue
                target = registry.counterpart(peer)
                if target is None:
                    await ws.send_json({"type": "error", "code": "peer_unavailable"})
                    continue
                await target.websocket.send_json({**payload, "from_role": peer.role.value})
            elif message.type in (WSMsgType.ERROR, WSMsgType.CLOSE, WSMsgType.CLOSED):
                break
            else:
                await ws.send_json(
                    {"type": "error", "code": "text_frames_only"}
                )
    except asyncio.TimeoutError:
        await ws.close(code=4008, message=b"hello timeout")
    except ProtocolError as exc:
        await ws.send_json({"type": "error", "code": "bad_hello", "detail": str(exc)})
        await ws.close(code=4002, message=b"bad hello")
    finally:
        if peer is not None:
            other = registry.counterpart(peer)
            registry.detach(peer)
            if other is not None and not other.websocket.closed:
                await other.websocket.send_json(
                    {"type": "peer_left", "role": peer.role.value, "robot_id": peer.robot_id}
                )
            logger.info("%s disconnected for %s", peer.role.value, peer.robot_id)
    return ws


def create_app(config: ServerConfig | None = None) -> web.Application:
    cfg = config or ServerConfig.from_env()
    cfg.validate()
    app = web.Application(client_max_size=cfg.max_message_bytes)
    app[CONFIG_KEY] = cfg
    app[REGISTRY_KEY] = SessionRegistry()
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/ws", websocket_handler)
    return app
