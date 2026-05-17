#!/usr/bin/env python3
"""teleop_server entry point.

Telemetry-only server, split from stream_server. Opens its own Zenoh
router (default TCP 7447) and handles only the nev/teleop/{vid}/*
prefix. Has no dependency on stream_server.
"""
import argparse
import asyncio
import json
import logging
import time

import zenoh

from teleop_server.config import load_config
from teleop_server.zenoh_utils import sync_zenohd_config
from teleop_server.state import SharedState
from teleop_server.robot_bridge import RobotProtocol
from teleop_server.station_bridge import StationBridge
from teleop_server.telemetry.metrics import HealthState, M, PROM_AVAILABLE, make_metrics_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("robot_bridge").setLevel(logging.INFO)
logger = logging.getLogger("main")


def _update_vehicle_connectivity(
    state: SharedState,
    proto: RobotProtocol,
    now: float,
    disconnect_timeout: float,
    veh_disconnected: set[str],
) -> None:
    """Two-state machine: vehicle is connected while age < disconnect_timeout,
    else disconnected. Emit a log only on edge transitions.

    Also releases per-vehicle resources on disconnect (publisher GC) and
    clears stale entries from veh_disconnected on reconnect to bound growth.
    """
    live_vids: set[str] = set()
    for vid, veh in list(state.vehicles.items()):
        if veh.last_robot_recv <= 0:
            continue
        live_vids.add(vid)
        age = now - veh.last_robot_recv
        is_disconnected_now = age > disconnect_timeout
        was_disconnected = vid in veh_disconnected

        if is_disconnected_now and not was_disconnected:
            veh_disconnected.add(vid)
            proto.reset_dedupe(vid)
            proto.release_vehicle_pubs(vid)
            logger.warning(f"[{vid}] Robot disconnected")
        elif not is_disconnected_now and was_disconnected:
            veh_disconnected.discard(vid)
            logger.info(f"[{vid}] Robot reconnected")

    # Drop stale entries for vehicles that were removed from state entirely.
    for vid in list(veh_disconnected):
        if vid not in live_vids:
            veh_disconnected.discard(vid)


async def run_send_loop(
    state: SharedState,
    proto: RobotProtocol,
    cfg,
    health: "HealthState | None" = None,
):
    push_interval = 1.0 / cfg.server.telemetry_rate
    station_timeout = cfg.server.station_timeout
    disconnect_timeout = cfg.telemetry.disconnect_timeout

    last_push = 0.0
    _veh_disconnected: set[str] = set()

    while True:
        # tick_start brackets the wall time spent in one iteration so the
        # histogram captures sleep-excluded work; the post-sleep block is
        # measurement-only.
        tick_start = time.monotonic()
        now = tick_start

        _update_vehicle_connectivity(
            state, proto, now, disconnect_timeout, _veh_disconnected
        )

        if state.station_connected and state.station_last_recv > 0:
            if now - state.station_last_recv > station_timeout:
                logger.warning("Station heartbeat timeout — marking disconnected")
                state.update_station_connected(False)

        proto.calc_bandwidth()

        # Update the per-tick gauges. Cardinality is bounded by the number
        # of currently-known vehicles (2 in production) — see ALLOWED_TOPICS
        # in telemetry.metrics for the cardinality discipline rationale.
        try:
            live_count = 0
            for vid, veh in state.vehicles.items():
                if veh.last_robot_recv > 0:
                    age = now - veh.last_robot_recv
                    M.set_vehicle_age(vid, age)
                    if age < disconnect_timeout:
                        live_count += 1
            M.set_vehicles_connected(live_count)
            M.set_station_connected(state.station_connected)
        except Exception:
            pass

        if now - last_push >= push_interval:
            state.validate()
            state_json = state.to_json()
            proto.send_telemetry(state_json)
            last_push = now

        # Healthz freshness: mark BEFORE sleep so /healthz reflects the
        # latest completed iteration rather than the inside of asyncio.sleep.
        if health is not None:
            health.mark_tick()

        # Observe wall-time spent in this iteration's work (excluding
        # sleep) so we don't pollute the histogram with the asyncio.sleep
        # cost. This is what we actually want to alert on.
        try:
            M.observe_tick(time.monotonic() - tick_start)
        except Exception:
            pass

        # If we fell behind (sleep_for < 0), drop the schedule rather than
        # spinning at 1ms — pretend the push just happened so we don't
        # immediately fire again next iteration.
        sleep_for = last_push + push_interval - now
        if sleep_for <= 0:
            try:
                M.inc_fell_behind()
            except Exception:
                pass
            last_push = now
            sleep_for = push_interval
        await asyncio.sleep(sleep_for)


async def _start_metrics_server(cfg, health: HealthState):
    """Start the embedded aiohttp /healthz + /metrics listener.

    Returns the ``AppRunner`` so the caller can clean it up in the
    shutdown path. Returns ``None`` (and logs) when the endpoint is
    disabled or aiohttp isn't installed.
    """
    if not cfg.metrics.enabled:
        logger.info("Metrics endpoint disabled (metrics.enabled=false)")
        return None
    try:
        from aiohttp import web  # noqa: F401  (import surfaces missing dep)
    except ImportError as e:
        logger.warning(
            f"aiohttp not installed ({e}); /healthz + /metrics disabled. "
            "Install via requirements.txt to re-enable monitoring."
        )
        return None
    if not PROM_AVAILABLE:
        logger.warning(
            "prometheus_client not installed; /healthz still served but /metrics "
            "will return 503. Install prometheus_client to enable metrics."
        )

    app = make_metrics_app(health)
    from aiohttp import web
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, cfg.metrics.bind, cfg.metrics.port)
    try:
        await site.start()
    except OSError as e:
        # Most commonly EADDRINUSE — log and continue; the rest of the
        # server must stay up even if monitoring fails to bind.
        logger.error(
            f"Failed to start metrics endpoint on {cfg.metrics.bind}:{cfg.metrics.port}: {e}"
        )
        try:
            await runner.cleanup()
        except Exception:
            pass
        return None
    logger.info(
        f"Metrics endpoint listening on http://{cfg.metrics.bind}:{cfg.metrics.port}"
        f" (/healthz, /metrics)"
    )
    return runner


async def setup_relays(session, cfg, loop):
    """Wire teleop-side relays onto an externally-managed Zenoh session.

    Returns ``(state, robot_proto, station_bridge, health, metrics_runner)``.
    The caller is responsible for stopping the bridges and closing the
    session. Used by the unified ``server_main`` to share one router
    session between teleop + stream relays.
    """
    state = SharedState(cfg.telemetry)

    # Healthz staleness window is 2 * push_interval — anything looser and
    # we won't notice a stuck event loop before the operator does.
    push_interval = 1.0 / cfg.server.telemetry_rate
    health = HealthState(stale_after_s=2.0 * push_interval)
    metrics_runner = await _start_metrics_server(cfg, health)

    robot_proto = None
    station_bridge = None
    try:
        robot_proto = RobotProtocol(state, loop, cfg.telemetry)
        robot_proto.start(session)
        station_bridge = StationBridge(state, loop, robot_proto)
        station_bridge.start(session)
    except Exception:
        if station_bridge is not None:
            try:
                station_bridge.stop()
            except Exception:
                pass
        if robot_proto is not None:
            try:
                robot_proto.stop()
            except Exception:
                pass
        if metrics_runner is not None:
            try:
                await metrics_runner.cleanup()
            except Exception:
                pass
        raise

    return state, robot_proto, station_bridge, health, metrics_runner


async def run(cfg):
    """Solo entry: opens its own router session, runs the send-loop, cleans up."""
    tcp_port = cfg.zenoh.tcp_port

    sync_zenohd_config(tcp_port)

    loop = asyncio.get_running_loop()
    listen_eps = [f"tcp/0.0.0.0:{tcp_port}"]
    zconf = zenoh.Config()
    zconf.insert_json5("mode", '"router"')
    zconf.insert_json5("listen/endpoints", json.dumps(listen_eps))

    session = zenoh.open(zconf)
    logger.info(f"Zenoh TELEOP router opened -> listening on {listen_eps}")

    try:
        state, robot_proto, station_bridge, health, metrics_runner = (
            await setup_relays(session, cfg, loop)
        )
    except Exception:
        session.close()
        raise

    logger.info("teleop_server running (Zenoh relay, telemetry-only)")

    try:
        await run_send_loop(state, robot_proto, cfg, health=health)
    finally:
        station_bridge.stop()
        robot_proto.stop()
        session.close()
        if metrics_runner is not None:
            try:
                await metrics_runner.cleanup()
            except Exception as e:
                logger.warning(f"metrics_runner cleanup: {e}")
        logger.info("Shutdown complete")


def main():
    parser = argparse.ArgumentParser(description="NEV Teleop Server (telemetry-only)")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--zenoh-tcp-port", type=int, default=None)
    args = parser.parse_args()

    cfg = load_config(
        args.config,
        {
            "zenoh_tcp_port": args.zenoh_tcp_port,
        },
    )

    try:
        asyncio.run(run(cfg))
    except KeyboardInterrupt:
        logger.info("Stopped by user")


if __name__ == "__main__":
    main()
