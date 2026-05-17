"""Tests for the embedded /healthz + /metrics endpoint and the metric
module's public API.

Routes are exercised via :mod:`aiohttp.test_utils` so the suite does
not need to bind a real TCP socket — that keeps it safe to run in CI
sandboxes that block listening sockets.
"""
import time

import pytest
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase

from teleop_server.telemetry import metrics as metrics_module
from teleop_server.telemetry.metrics import (
    ALLOWED_TOPICS,
    HealthState,
    M,
    Metrics,
    make_metrics_app,
    safe_topic,
)


# ---------------------------------------------------------------------------
# Module shape: callers `from telemetry.metrics import M` and expect a
# fixed set of names. Bind this contract in a test so accidental
# renames blow up here rather than at a hot call site.
# ---------------------------------------------------------------------------
class TestMetricModuleImports:
    def test_singleton_M_exists(self):
        assert isinstance(M, Metrics)

    def test_metrics_class_has_expected_instruments(self):
        m = Metrics()
        # Gauges
        assert hasattr(m, "vehicles_connected")
        assert hasattr(m, "station_connected")
        assert hasattr(m, "telemetry_age_seconds")
        # Histograms
        assert hasattr(m, "send_loop_tick_seconds")
        # Counters
        assert hasattr(m, "send_loop_fell_behind_total")
        assert hasattr(m, "bytes_in_total")
        assert hasattr(m, "bytes_out_total")
        assert hasattr(m, "parse_errors_total")
        assert hasattr(m, "publish_errors_total")

    def test_helpers_exist(self):
        for name in (
            "inc_bytes_in",
            "inc_bytes_out",
            "inc_parse_error",
            "inc_publish_error",
            "set_vehicles_connected",
            "set_station_connected",
            "set_vehicle_age",
            "observe_tick",
            "inc_fell_behind",
        ):
            assert callable(getattr(M, name))

    def test_safe_topic_passes_allowlist(self):
        for t in ALLOWED_TOPICS:
            assert safe_topic(t) == t

    def test_safe_topic_coerces_unknown_to_other(self):
        # Cardinality discipline: anything off-list must collapse to
        # "other" so a noisy wire can't blow up Prometheus memory.
        assert safe_topic("attacker/../etc/passwd") == "other"
        assert safe_topic("") == "other"
        assert safe_topic(None) == "other"
        assert safe_topic(123) == "other"  # non-str

    def test_helpers_do_not_raise_on_bad_input(self):
        # The contract is "metric bug never breaks the data path" — feed
        # garbage and assert the helper swallows it.
        M.inc_bytes_in("nonsense", 10)  # coerces to "other"
        M.inc_bytes_in("teleop", -1)    # negative is weird but accepted
        M.set_vehicle_age("vid-1", float("nan"))


# ---------------------------------------------------------------------------
# HealthState: pure logic, no aiohttp needed.
# ---------------------------------------------------------------------------
class TestHealthState:
    def test_fresh_unticked_returns_uptime_as_age(self):
        h = HealthState(stale_after_s=10.0)
        # Without a tick, age == uptime — operator sees a meaningful number
        # rather than "0 seconds ago".
        assert h.last_tick_age_s() >= 0
        assert h.last_tick_age_s() < 1.0  # just-constructed

    def test_mark_tick_updates_age(self):
        h = HealthState(stale_after_s=10.0)
        h.mark_tick()
        assert h.last_tick_age_s() < 0.1
        assert h.is_healthy()

    def test_stale_after_window(self):
        h = HealthState(stale_after_s=0.01)
        h.last_send_loop_tick = time.monotonic() - 1.0
        assert not h.is_healthy()


# ---------------------------------------------------------------------------
# /healthz + /metrics route tests. AioHTTPTestCase spins up an in-process
# server backed by the asyncio loop — no real port binding required.
# ---------------------------------------------------------------------------
class TestHealthzFresh(AioHTTPTestCase):
    async def get_application(self) -> web.Application:
        h = HealthState(stale_after_s=10.0)
        h.mark_tick()  # fresh tick → healthy
        return make_metrics_app(h)

    async def test_returns_200_with_expected_keys(self):
        resp = await self.client.request("GET", "/healthz")
        assert resp.status == 200
        body = await resp.json()
        assert body["status"] == "ok"
        assert "uptime_s" in body
        assert "last_send_loop_tick_age_s" in body
        assert body["last_send_loop_tick_age_s"] >= 0
        assert body["last_send_loop_tick_age_s"] < 1.0


class TestHealthzStale(AioHTTPTestCase):
    async def get_application(self) -> web.Application:
        # Backdate last_send_loop_tick by 5s so it's well past the
        # 0.5s staleness window — independent of how fast the test
        # harness actually drives the request.
        h = HealthState(stale_after_s=0.5)
        h.last_send_loop_tick = time.monotonic() - 5.0
        return make_metrics_app(h)

    async def test_returns_503_with_reason(self):
        resp = await self.client.request("GET", "/healthz")
        assert resp.status == 503
        body = await resp.json()
        assert body["status"] == "degraded"
        assert body["reason"] == "send_loop_stalled"
        assert "last_tick_age_s" in body


class TestMetricsRoute(AioHTTPTestCase):
    async def get_application(self) -> web.Application:
        # Use a fresh Metrics() (private registry) so we don't depend on
        # whatever global counters happen to be set when this suite runs.
        m = Metrics()
        # Drive a handful of increments so /metrics output contains
        # something other than just metric metadata.
        m.inc_bytes_in("mux", 100)
        m.inc_bytes_out("telemetry", 250)
        m.inc_parse_error("teleop")
        m.set_vehicles_connected(2)
        m.set_station_connected(True)
        m.observe_tick(0.012)
        m.inc_fell_behind()
        h = HealthState(stale_after_s=10.0)
        h.mark_tick()
        return make_metrics_app(h, metrics=m)

    async def test_metrics_returns_text_with_expected_names(self):
        if not metrics_module.PROM_AVAILABLE:
            pytest.skip("prometheus_client not installed")
        resp = await self.client.request("GET", "/metrics")
        assert resp.status == 200
        text = await resp.text()
        for name in (
            "teleop_vehicles_connected",
            "teleop_station_connected",
            "teleop_send_loop_tick_seconds",
            "teleop_send_loop_fell_behind_total",
            "teleop_bytes_in_total",
            "teleop_bytes_out_total",
            "teleop_parse_errors_total",
            "teleop_publish_errors_total",
        ):
            assert name in text, f"missing {name} in /metrics output"

    async def test_app_has_expected_routes(self):
        app = self.app
        paths = {res.resource.canonical for res in app.router.routes()}
        assert "/healthz" in paths
        assert "/metrics" in paths


# ---------------------------------------------------------------------------
# Topic-label cardinality is the one thing we MUST get right — an unbounded
# vid or attacker-controlled suffix would explode Prometheus memory. Verify
# via the public API that off-list values collapse to "other".
# ---------------------------------------------------------------------------
class TestCardinalityDiscipline:
    def test_unknown_topic_increments_other_label(self):
        m = Metrics()
        m.inc_bytes_in("not_a_real_topic", 50)
        # Walk the underlying Counter samples — the only label value we
        # should see for this metric is "other".
        if not metrics_module.PROM_AVAILABLE:
            pytest.skip("prometheus_client not installed")
        samples_with_value = [
            s for metric_family in m.bytes_in_total.collect()
            for s in metric_family.samples
            if s.name.endswith("_total") and s.value > 0
        ]
        labels = {s.labels.get("topic") for s in samples_with_value}
        assert labels == {"other"}, f"expected only 'other', got {labels}"
