from __future__ import annotations

import argparse
import logging

from aiohttp import web

from .app import create_app
from .config import ServerConfig


def main() -> None:
    defaults = ServerConfig.from_env()
    parser = argparse.ArgumentParser(description="Teleop v2 WebRTC signaling server")
    parser.add_argument("--bind", default=defaults.bind)
    parser.add_argument("--port", type=int, default=defaults.port)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    cfg = ServerConfig(
        bind=args.bind,
        port=args.port,
        hello_timeout_s=defaults.hello_timeout_s,
        max_message_bytes=defaults.max_message_bytes,
        turn_url=defaults.turn_url,
        turn_username=defaults.turn_username,
        turn_password=defaults.turn_password,
    )
    cfg.validate()
    web.run_app(create_app(cfg), host=cfg.bind, port=cfg.port)


if __name__ == "__main__":
    main()
