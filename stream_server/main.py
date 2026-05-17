#!/usr/bin/env python3
"""stream_server entry point.

TCP-only video relay defined by /home/nev/teleop/TCP_WIRE_SPEC.md. Owns
its own Zenoh router on TCP 7457 (no UDP locator) and handles only the
nev/stream_tcp/{vid}/* prefix. Independent of stream_server (UDP variant).
"""
import argparse
import asyncio
import logging
import time

from stream_server.config import load_config
from stream_server.zenoh_utils import open_router_session, sync_zenohd_config
from stream_server.state import StreamSharedState
from stream_server.robot_bridge import RobotBridge
from stream_server.bitrate_controller import BitrateController
from stream_server.station_bridge import StationBridge
from stream_server.stream_telemetry import StreamTelemetryDriver

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("robot_bridge").setLevel(logging.INFO)
logging.getLogger("bitrate_controller").setLevel(logging.INFO)
logger = logging.getLogger("main")


async def run_send_loop(
    state: StreamSharedState,
    proto: RobotBridge,
    cfg,
    bitrate_ctl: BitrateController,
    tele: StreamTelemetryDriver,
):
    ping_interval = 1.0 / cfg.server.video_ping_rate
    last_ping = 0.0

    while True:
        now = time.monotonic()

        tele.check_bot_disconnect()
        tele.check_client_heartbeat()

        proto.calc_bandwidth()
        proto.check_rtt_stale()
        proto.prune_stale_cameras()
        bitrate_ctl.step()
        bitrate_ctl.log_snapshot()

        if now - last_ping >= ping_interval:
            tele.send_video_pings()
            last_ping = now

        next_ping = last_ping + ping_interval - now
        sleep_for = max(0.05, next_ping)
        await asyncio.sleep(sleep_for)


async def setup_relays(session, cfg, loop):
    """Wire stream-side relays onto an externally-managed Zenoh session.

    Returns ``(state, robot_proto, station_bridge, bitrate_ctl, tele)``.
    Caller stops/closes. Used by the unified ``server_main`` to share
    one router session between teleop + stream relays.
    """
    state = StreamSharedState(cfg.telemetry)

    robot_proto = RobotBridge(state, loop, cfg.telemetry, cfg.video)
    robot_proto.start(session)

    station_bridge = StationBridge(state, loop, robot_proto)
    station_bridge.start(session)

    bitrate_ctl = BitrateController(robot_proto, state, cfg.video)
    robot_proto.attach_bitrate_controller(bitrate_ctl)
    station_bridge.attach_bitrate_controller(bitrate_ctl)

    if cfg.video.enabled:
        logger.info(
            f"Adaptive bitrate ENABLED: init={cfg.video.initial_kbps}kbps "
            f"range=[{cfg.video.min_kbps},{cfg.video.max_kbps}]kbps "
            f"put_lat[low/high]={cfg.video.put_latency_low_ms:.0f}/"
            f"{cfg.video.put_latency_high_ms:.0f}ms "
            f"rtt[low/high]={cfg.video.rtt_low_ms:.0f}/"
            f"{cfg.video.rtt_high_ms:.0f}ms "
            f"dwell={cfg.video.dwell_s:.0f}s recovery_dwell={cfg.video.recovery_dwell_s:.0f}s"
        )
    else:
        logger.info("Adaptive bitrate DISABLED (config.video.enabled=false)")

    tele = StreamTelemetryDriver(
        state,
        robot_proto,
        cfg.vehicle_id,
        cfg.server.client_heartbeat_timeout,
        cfg.server.bot_disconnect_timeout,
    )

    return state, robot_proto, station_bridge, bitrate_ctl, tele


async def run(cfg):
    """Solo entry: opens its own router session, runs the send-loop, cleans up."""
    tcp_port = cfg.zenoh.tcp_port
    sync_zenohd_config(tcp_port)

    loop = asyncio.get_running_loop()
    session = open_router_session(tcp_port)
    logger.info(f"  TCP {tcp_port} = camera + control (TCP_WIRE_SPEC §1)")
    logger.info(f"[CFG] vehicle_id={cfg.vehicle_id}")
    logger.info("[CFG] transport=tcp_only qos=RELIABLE+BLOCK abr_signals=put_latency+rtt")

    try:
        state, robot_proto, station_bridge, bitrate_ctl, tele = (
            await setup_relays(session, cfg, loop)
        )
    except Exception:
        session.close()
        raise

    logger.info("stream_server running (Zenoh relay, TCP-only video)")

    try:
        await run_send_loop(state, robot_proto, cfg, bitrate_ctl, tele)
    finally:
        station_bridge.stop()
        robot_proto.stop()
        session.close()
        logger.info("Shutdown complete")


def main():
    parser = argparse.ArgumentParser(description="NEV Stream Server (TCP variant)")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--zenoh-tcp-port", type=int, default=None)
    parser.add_argument("--vehicle-id", default=None)
    args = parser.parse_args()

    cfg = load_config(
        args.config,
        {
            "zenoh_tcp_port": args.zenoh_tcp_port,
            "vehicle_id": args.vehicle_id,
        },
    )

    try:
        asyncio.run(run(cfg))
    except KeyboardInterrupt:
        logger.info("Stopped by user")


if __name__ == "__main__":
    main()
