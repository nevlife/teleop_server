"""Centralized Prometheus metric definitions + healthz/metrics aiohttp app.

This module is imported from hot paths (robot_bridge / station_bridge /
main's send loop). To keep the data path safe even when the optional
`prometheus_client` dependency is missing, the module exposes a small
``M`` object whose attributes are either real Prometheus metric instances
or no-op stand-ins with the same surface (``inc``, ``observe``, ``set``,
and ``labels``). Call sites should ALWAYS wrap metric calls in a
try/except — a metric bug must never break the wire.

The aiohttp app factory ``make_metrics_app`` mounts:

  * ``GET /healthz``  — JSON liveness probe driven by ``HealthState``.
  * ``GET /metrics``  — Prometheus text exposition (omitted if
                        prometheus_client unavailable; a 503 is returned
                        instead so the operator notices in scraping).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Cardinality discipline.
#
# The wire is full of free-form strings (vehicle_ids picked by operators,
# stray PUTs from the field). We MUST NOT propagate any of that into label
# values — Prometheus explodes per unique (metric, label-tuple) and a
# noisy attacker / bug could push the server into multi-GB memory.
#
# Hence: a fixed allow-list of topic suffixes (matches the constants in
# teleop_contracts.topics). Any label that isn't in this set gets coerced
# to "other" before being passed to the Counter.
# ---------------------------------------------------------------------------
ALLOWED_TOPICS = frozenset({
    "mux",
    "teleop",
    "estop",
    "cmd_mode",
    "cpu",
    "mem",
    "gpu",
    "disk",
    "net",
    "bot_heartbeat",
    "controller_heartbeat",
    "client_heartbeat",
    "telemetry_ping",
    "telemetry_pong",
    "cmd",
    "estop_cmd",
    "telemetry",
    "estop_status",
})

_SEND_LOOP_BUCKETS = (0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0)


def safe_topic(topic: Optional[str]) -> str:
    """Coerce a possibly-untrusted topic suffix into the allow-list.

    Anything unknown is mapped to ``"other"`` so we still get a bounded
    cardinality signal without leaking attacker-controlled strings.
    """
    if isinstance(topic, str) and topic in ALLOWED_TOPICS:
        return topic
    return "other"


# ---------------------------------------------------------------------------
# Try to import prometheus_client. If absent, install no-op shims so call
# sites can stay unconditional and the /metrics route disables itself.
# ---------------------------------------------------------------------------
try:
    from prometheus_client import (
        CONTENT_TYPE_LATEST,
        CollectorRegistry,
        Counter,
        Gauge,
        Histogram,
        generate_latest,
    )
    PROM_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised in fallback env only
    PROM_AVAILABLE = False
    CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"

    class _NoopChild:
        def inc(self, *a, **kw): pass
        def observe(self, *a, **kw): pass
        def set(self, *a, **kw): pass

    class _NoopMetric:
        def __init__(self, *a, **kw): pass
        def labels(self, *a, **kw): return _NoopChild()
        def inc(self, *a, **kw): pass
        def observe(self, *a, **kw): pass
        def set(self, *a, **kw): pass

    Counter = Gauge = Histogram = _NoopMetric  # type: ignore
    CollectorRegistry = object  # type: ignore

    def generate_latest(_registry=None):  # type: ignore
        return b""


# ---------------------------------------------------------------------------
# Metric instances. We use a private registry so test runs don't leak
# state across each other (the default registry is process-global and
# raises on duplicate names — pytest re-imports break it otherwise).
# ---------------------------------------------------------------------------
class Metrics:
    """Holder for the server's Prometheus metric instances.

    Construct once at startup, then import the singleton ``M`` to
    increment/observe from anywhere in the codebase.
    """

    def __init__(self, registry=None):
        if PROM_AVAILABLE:
            self.registry = registry or CollectorRegistry()
            kw = {"registry": self.registry}
        else:
            self.registry = None
            kw = {}

        self.vehicles_connected = Gauge(
            "teleop_vehicles_connected",
            "Number of vehicles with bot data age < disconnect_timeout.",
            **kw,
        )
        self.station_connected = Gauge(
            "teleop_station_connected",
            "1 if the operator station is connected, else 0.",
            **kw,
        )
        self.send_loop_tick_seconds = Histogram(
            "teleop_send_loop_tick_seconds",
            "Wall time spent inside one iteration of run_send_loop.",
            buckets=_SEND_LOOP_BUCKETS,
            **kw,
        )
        self.send_loop_fell_behind_total = Counter(
            "teleop_send_loop_fell_behind_total",
            "Times the send loop missed its schedule (sleep_for <= 0).",
            **kw,
        )
        self.bytes_in_total = Counter(
            "teleop_bytes_in_total",
            "Bytes received per topic suffix.",
            ["topic"],
            **kw,
        )
        self.bytes_out_total = Counter(
            "teleop_bytes_out_total",
            "Bytes published per topic suffix.",
            ["topic"],
            **kw,
        )
        self.parse_errors_total = Counter(
            "teleop_parse_errors_total",
            "JSON / envelope decode failures, by topic.",
            ["topic"],
            **kw,
        )
        self.telemetry_age_seconds = Gauge(
            "teleop_telemetry_age_seconds",
            "Age (seconds) of the most-recent bot packet per vehicle.",
            ["vid"],
            **kw,
        )
        self.publish_errors_total = Counter(
            "teleop_publish_errors_total",
            "pub.put() failures, by topic.",
            ["topic"],
            **kw,
        )

    # ----- defensive helpers -------------------------------------------------
    # Each helper is wrapped in try/except so any metric-side bug never
    # breaks the data path. Counter.inc rarely raises, but coercing
    # via safe_topic() involves a string lookup and we'd rather pay the
    # cost than risk an UnboundLocalError on a hot loop.

    def inc_bytes_in(self, topic: str, n: int) -> None:
        try:
            self.bytes_in_total.labels(topic=safe_topic(topic)).inc(n)
        except Exception:  # pragma: no cover
            pass

    def inc_bytes_out(self, topic: str, n: int) -> None:
        try:
            self.bytes_out_total.labels(topic=safe_topic(topic)).inc(n)
        except Exception:  # pragma: no cover
            pass

    def inc_parse_error(self, topic: str) -> None:
        try:
            self.parse_errors_total.labels(topic=safe_topic(topic)).inc()
        except Exception:  # pragma: no cover
            pass

    def inc_publish_error(self, topic: str) -> None:
        try:
            self.publish_errors_total.labels(topic=safe_topic(topic)).inc()
        except Exception:  # pragma: no cover
            pass

    def set_vehicles_connected(self, n: int) -> None:
        try:
            self.vehicles_connected.set(n)
        except Exception:  # pragma: no cover
            pass

    def set_station_connected(self, connected: bool) -> None:
        try:
            self.station_connected.set(1 if connected else 0)
        except Exception:  # pragma: no cover
            pass

    def set_vehicle_age(self, vid: str, age_s: float) -> None:
        # vid label cardinality is bounded by deployment (2 robots today);
        # the caller is responsible for not synthesising new vids per tick.
        try:
            self.telemetry_age_seconds.labels(vid=vid).set(age_s)
        except Exception:  # pragma: no cover
            pass

    def observe_tick(self, seconds: float) -> None:
        try:
            self.send_loop_tick_seconds.observe(seconds)
        except Exception:  # pragma: no cover
            pass

    def inc_fell_behind(self) -> None:
        try:
            self.send_loop_fell_behind_total.inc()
        except Exception:  # pragma: no cover
            pass


# Module-level singleton. Imported as ``from telemetry.metrics import M``.
# The constructor is cheap (just declares metric objects) and idempotent
# w.r.t. semantics, but the private registry is allocated once here.
M = Metrics()


# ---------------------------------------------------------------------------
# Health state. The send loop updates ``last_send_loop_tick`` each tick;
# the /healthz handler reads it (no locking needed — single-threaded
# event loop). Default of 0 means "never ticked" which deliberately
# trips the staleness check on a freshly-started but stalled server.
# ---------------------------------------------------------------------------
@dataclass
class HealthState:
    start_time: float = field(default_factory=time.monotonic)
    last_send_loop_tick: float = 0.0
    # Staleness threshold = 2 * push_interval. Caller (main.run) fills this
    # in based on cfg.server.telemetry_rate.
    stale_after_s: float = 2.0

    def mark_tick(self) -> None:
        self.last_send_loop_tick = time.monotonic()

    def uptime_s(self) -> float:
        return time.monotonic() - self.start_time

    def last_tick_age_s(self) -> float:
        # Until the loop ticks once, expose the time since start so the
        # operator sees a meaningful number (not "0 seconds ago").
        if self.last_send_loop_tick <= 0:
            return self.uptime_s()
        return time.monotonic() - self.last_send_loop_tick

    def is_healthy(self) -> bool:
        # If we've never ticked AND we just started, give the loop one
        # full window before declaring stalled. ``stale_after_s`` is
        # already (2 * push_interval) so a server that boots and never
        # ticks will trip after that grace window — which is what we want.
        return self.last_tick_age_s() < self.stale_after_s


# ---------------------------------------------------------------------------
# aiohttp app factory.
# ---------------------------------------------------------------------------
def make_metrics_app(health: HealthState, metrics: Metrics = M):
    """Build an aiohttp app exposing /healthz and /metrics.

    aiohttp is a hard dependency (Stage 1 requirement). If it cannot be
    imported the caller should not invoke this factory; the higher-level
    config disables the metrics server when missing.
    """
    from aiohttp import web

    async def healthz(_request: "web.Request") -> "web.Response":
        age = health.last_tick_age_s()
        if health.is_healthy():
            return web.json_response(
                {
                    "status": "ok",
                    "uptime_s": health.uptime_s(),
                    "last_send_loop_tick_age_s": age,
                },
                status=200,
            )
        return web.json_response(
            {
                "status": "degraded",
                "reason": "send_loop_stalled",
                "last_tick_age_s": age,
            },
            status=503,
        )

    async def metrics_handler(_request: "web.Request") -> "web.Response":
        if not PROM_AVAILABLE:
            # Surface the mis-configuration loudly via HTTP status so a
            # scrape config fails fast rather than silently returning 200/empty.
            return web.Response(
                status=503,
                text="prometheus_client not installed",
                content_type="text/plain",
            )
        try:
            body = generate_latest(metrics.registry)
        except Exception as e:  # pragma: no cover - defensive
            logger.warning(f"generate_latest failed: {e}")
            return web.Response(status=500, text="metrics collection error")
        # CONTENT_TYPE_LATEST is the full Prometheus content type string;
        # passing it via headers (not content_type) avoids aiohttp's
        # "both header and param" guard.
        return web.Response(
            body=body,
            headers={"Content-Type": CONTENT_TYPE_LATEST},
        )

    app = web.Application()
    app.router.add_get("/healthz", healthz)
    # Always mount /metrics — if prometheus_client is missing the handler
    # returns 503 so a scrape config fails loudly rather than silently.
    app.router.add_get("/metrics", metrics_handler)
    # Attach the health/metrics objects so tests can grab them off the app.
    # Plain dict-style keys trigger NotAppKeyWarning in aiohttp >= 3.9;
    # use web.AppKey for forward compatibility.
    app[web.AppKey("health", HealthState)] = health
    app[web.AppKey("metrics", Metrics)] = metrics
    return app


__all__ = [
    "ALLOWED_TOPICS",
    "HealthState",
    "M",
    "Metrics",
    "PROM_AVAILABLE",
    "make_metrics_app",
    "safe_topic",
]
