#!/usr/bin/env python3
"""Unified server-side entry: telemetry/control relay + video relay in one process.

Opens a single Zenoh router session on the shared TCP listen port
(default 7447) and wires both subsystems onto it:

  * teleop side  — control + telemetry relay   (nev/teleop/{vid}/*)
  * stream side  — TCP-only AU video relay     (nev/stream_tcp/{vid}/*)

Both subsystems run their own send-loops as coroutines on the same
asyncio loop, joined via ``asyncio.gather``. A failure or shutdown
signal in either tears down both.

The legacy per-service entry points (``teleop_server/main.py`` and
``stream_server/main.py``) still expose ``main()`` for solo runs but
they open their OWN session — running them alongside this unified
process would conflict on the listener port.
"""
import argparse
import asyncio
import json
import logging

import zenoh

from teleop_server.config import load_config as load_teleop_config
from teleop_server.zenoh_utils import sync_zenohd_config as sync_teleop_zenohd
from teleop_server.main import (
    setup_relays as setup_teleop_relays,
    run_send_loop as run_teleop_send_loop,
)

from stream_server.config import load_config as load_stream_config
from stream_server.main import (
    setup_relays as setup_stream_relays,
    run_send_loop as run_stream_send_loop,
)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("robot_bridge").setLevel(logging.INFO)
logging.getLogger("bitrate_controller").setLevel(logging.INFO)
logger = logging.getLogger("server_main")


def _open_unified_router(tcp_port: int) -> zenoh.Session:
    """Open ONE Zenoh router session listening on TCP."""
    listen_eps = [f"tcp/0.0.0.0:{tcp_port}"]
    zconf = zenoh.Config()
    zconf.insert_json5("mode", '"router"')
    zconf.insert_json5("listen/endpoints", json.dumps(listen_eps))
    session = zenoh.open(zconf)
    logger.info(f"Zenoh UNIFIED router opened -> listening on {listen_eps}")
    return session


async def _async_main(teleop_cfg, stream_cfg) -> None:
    tcp_port = teleop_cfg.zenoh.tcp_port

    # Re-write zenohd.json5 on disk so an out-of-process zenohd (if used)
    # matches our listen port.
    sync_teleop_zenohd(tcp_port)

    loop = asyncio.get_running_loop()
    session = _open_unified_router(tcp_port)

    teleop_objs = None
    stream_objs = None
    try:
        teleop_objs = await setup_teleop_relays(session, teleop_cfg, loop)
        stream_objs = await setup_stream_relays(session, stream_cfg, loop)
    except Exception:
        if teleop_objs is not None:
            _stop_teleop(teleop_objs)
        if stream_objs is not None:
            _stop_stream(stream_objs)
        session.close()
        raise

    teleop_state, teleop_robot, teleop_station, teleop_health, metrics_runner = teleop_objs
    stream_state, stream_robot, stream_station, bitrate_ctl, stream_tele = stream_objs

    logger.info("Unified server running (teleop + stream on one Zenoh router)")
    try:
        await asyncio.gather(
            run_teleop_send_loop(
                teleop_state, teleop_robot, teleop_cfg, health=teleop_health,
            ),
            run_stream_send_loop(
                stream_state, stream_robot, stream_cfg, bitrate_ctl, stream_tele,
            ),
        )
    finally:
        _stop_teleop(teleop_objs)
        _stop_stream(stream_objs)
        try:
            session.close()
        except Exception as e:
            logger.warning(f"session close: {e}")
        if metrics_runner is not None:
            try:
                await metrics_runner.cleanup()
            except Exception as e:
                logger.warning(f"metrics_runner cleanup: {e}")
        logger.info("Shutdown complete")


def _stop_teleop(objs) -> None:
    _, robot, station, _, _ = objs
    try:
        station.stop()
    except Exception:
        pass
    try:
        robot.stop()
    except Exception:
        pass


def _stop_stream(objs) -> None:
    _, robot, station, _, _ = objs
    try:
        station.stop()
    except Exception:
        pass
    try:
        robot.stop()
    except Exception:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(
        description="NEV unified server (teleop + stream relays in one process)"
    )
    parser.add_argument("--teleop-config", default="teleop_server/config.yaml")
    parser.add_argument("--stream-config", default="stream_server/config.yaml")
    parser.add_argument("--zenoh-tcp-port", type=int, default=None,
                        help="Override TCP listen port (applied to both configs).")
    args = parser.parse_args()

    teleop_cfg = load_teleop_config(args.teleop_config, {
        "zenoh_tcp_port": args.zenoh_tcp_port,
    })
    stream_cfg = load_stream_config(args.stream_config, {
        "zenoh_tcp_port": args.zenoh_tcp_port,
    })

    if teleop_cfg.zenoh.tcp_port != stream_cfg.zenoh.tcp_port:
        logger.warning(
            "TCP port mismatch: teleop=%d stream=%d — using teleop value for both",
            teleop_cfg.zenoh.tcp_port, stream_cfg.zenoh.tcp_port,
        )

    try:
        asyncio.run(_async_main(teleop_cfg, stream_cfg))
    except KeyboardInterrupt:
        logger.info("Stopped by user")


if __name__ == "__main__":
    main()
