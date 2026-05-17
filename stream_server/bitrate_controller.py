"""ABR controller for stream_server.

Signals (TCP_WIRE_SPEC §9):
  * put_latency_ms_p95 — 1 s windowed p95 of pub.put duration on the
    server -> client camera channel. With RELIABLE+BLOCK, this rises when
    the downstream queue is full and backpressure stalls put.
  * rtt_ms_p95          — 1 s windowed p95 of server <-> bot ping RTT.

Policy:
  * Either signal over its high threshold => bitrate down 20%, 5 s dwell.
  * For 30 s, both signals at <= 50% of their respective thresholds =>
    bitrate up 10%, 5 s dwell.
  * bitrate clamped to [200, 3000] kbps (configurable).

No loss-based path. RELIABLE delivery means loss=0 by construction (§3).
"""
import logging
import time
from collections import deque
from typing import Optional

from stream_server.config.schema import VideoConfig
from stream_server.state import StreamSharedState

logger = logging.getLogger(__name__)


def _percentile(samples: list, pct: float) -> float:
    """Nearest-rank percentile. Returns 0.0 on empty list."""
    if not samples:
        return 0.0
    s = sorted(samples)
    k = max(0, min(len(s) - 1, int(round((pct / 100.0) * (len(s) - 1)))))
    return float(s[k])


class _WindowedP95:
    """Sliding 1 s window p95. Values come in as (sample, monotonic_ts)."""

    def __init__(self, window_s: float):
        self.window_s = max(0.05, window_s)
        self._buf: "deque[tuple[float, float]]" = deque()

    def add(self, value: float, now: float) -> None:
        self._buf.append((now, value))
        cutoff = now - self.window_s
        while self._buf and self._buf[0][0] < cutoff:
            self._buf.popleft()

    def p95(self, now: float) -> float:
        cutoff = now - self.window_s
        while self._buf and self._buf[0][0] < cutoff:
            self._buf.popleft()
        if not self._buf:
            return 0.0
        return _percentile([v for _, v in self._buf], 95.0)

    def count(self) -> int:
        return len(self._buf)


class BitrateController:
    """Per-vehicle ABR: put_latency + RTT signals, dual hysteresis."""

    def __init__(
        self,
        proto,
        state: StreamSharedState,
        video_cfg: VideoConfig,
    ):
        self._proto = proto
        self._state = state
        self._cfg = video_cfg
        self._veh: dict[str, dict] = {}
        self._last_snapshot_log: float = 0.0
        self._snapshot_interval_s: float = 2.0

    # ------------------------------------------------------------------ #
    # Signal ingestion (called from IO thread)
    # ------------------------------------------------------------------ #

    def on_put_latency(self, vehicle_id: str, latency_ms: float, now: float) -> None:
        s = self._veh_state(vehicle_id)
        s["put_lat_win"].add(latency_ms, now)

    def on_rtt(self, vehicle_id: str, rtt_ms: float, now: float) -> None:
        s = self._veh_state(vehicle_id)
        s["rtt_win"].add(rtt_ms, now)

    def on_video_feedback(
        self,
        vehicle_id: str,
        latency_p95_ms: float,
        freeze_ms_last_1s: float,
        now: float,
    ) -> None:
        # Informational only — kept for observability. Spec §9 mandates that
        # ABR decisions use put_latency + RTT, not client-reported metrics.
        s = self._veh_state(vehicle_id)
        s["client_latency_p95_ms"] = float(latency_p95_ms)
        s["client_freeze_ms"] = float(freeze_ms_last_1s)
        s["client_fb_recv"] = now

    # ------------------------------------------------------------------ #
    # Per-vehicle state
    # ------------------------------------------------------------------ #

    def _veh_state(self, vehicle_id: str) -> dict:
        s = self._veh.get(vehicle_id)
        if s is None:
            s = {
                "target_kbps": int(self._cfg.initial_kbps),
                "last_adjust": 0.0,
                "low_since": 0.0,
                "initialized": False,
                "last_num_cams": 0,
                "put_lat_win": _WindowedP95(self._cfg.window_s),
                "rtt_win": _WindowedP95(self._cfg.window_s),
                "client_latency_p95_ms": 0.0,
                "client_freeze_ms": 0.0,
                "client_fb_recv": 0.0,
            }
            self._veh[vehicle_id] = s
        return s

    # ------------------------------------------------------------------ #
    # Publish
    # ------------------------------------------------------------------ #

    def _publish(self, vehicle_id: str, kbps: int) -> None:
        veh = self._state.vehicles.get(vehicle_id)
        if veh is None or not veh.cameras:
            try:
                # Broadcast (no camera_id) — spec §6.a permits this.
                self._proto.send_video_bitrate(vehicle_id, kbps, "")
            except Exception as e:
                logger.warning(f"[{vehicle_id}] video_ctl publish failed: {e}")
            return

        cam_ids = list(veh.cameras.keys())
        per_cam = int(kbps) // max(1, len(cam_ids))
        s = self._veh.get(vehicle_id, {})
        prev_n = s.get("last_num_cams", 0)
        # Force re-publish when camera count changes so per-cam shares get
        # rebalanced even if the cached value matches.
        force = prev_n != len(cam_ids)
        s["last_num_cams"] = len(cam_ids)

        for cid in cam_ids:
            cam = veh.cameras[cid]
            if not force and cam.get("last_bitrate_kbps") == per_cam:
                continue
            try:
                self._proto.send_video_bitrate(vehicle_id, per_cam, cid)
                cam["last_bitrate_kbps"] = per_cam
            except Exception as e:
                logger.warning(
                    f"[{vehicle_id}/{cid or '-'}] video_ctl publish failed: {e}"
                )

    # ------------------------------------------------------------------ #
    # Main step (called from asyncio loop)
    # ------------------------------------------------------------------ #

    def step(self) -> None:
        if not self._cfg.enabled:
            return

        cfg = self._cfg
        now = time.monotonic()

        for vid in list(self._state.vehicles.keys()):
            veh = self._state.vehicles.get(vid)
            if veh is None:
                continue
            s = self._veh_state(vid)

            # Initial publish: wait until we have any signal so a slow link
            # doesn't get hit with a blind initial bitrate.
            if not s["initialized"]:
                has_signal = (
                    s["put_lat_win"].count() > 0 or s["rtt_win"].count() > 0
                )
                if has_signal:
                    self._publish(vid, s["target_kbps"])
                    s["last_adjust"] = now
                    s["initialized"] = True
                    logger.info(
                        f"[{vid}] BitrateController initialized "
                        f"-> {s['target_kbps']} kbps"
                    )
                continue

            # Rebalance per-cam shares when camera count changes.
            cur_n = len(veh.cameras)
            if cur_n != s.get("last_num_cams", 0) and cur_n > 0:
                self._publish(vid, s["target_kbps"])

            put_p95 = s["put_lat_win"].p95(now)
            rtt_p95 = s["rtt_win"].p95(now)

            # Decrease trigger: either signal above its high threshold,
            # subject to the global 5 s dwell.
            over = (put_p95 > cfg.put_latency_high_ms) or (rtt_p95 > cfg.rtt_high_ms)
            if over and (now - s["last_adjust"]) >= cfg.dwell_s:
                current = s["target_kbps"]
                candidate = int(round(current * cfg.decrease_factor))
                new_kbps = max(cfg.min_kbps, candidate)
                if new_kbps < current:
                    logger.info(
                        f"[{vid}] ABR DOWN: "
                        f"put_p95={put_p95:.0f}ms (lim {cfg.put_latency_high_ms:.0f}), "
                        f"rtt_p95={rtt_p95:.0f}ms (lim {cfg.rtt_high_ms:.0f}) "
                        f"-> {current} -> {new_kbps} kbps"
                    )
                    s["target_kbps"] = new_kbps
                    s["last_adjust"] = now
                    s["low_since"] = 0.0
                    self._publish(vid, new_kbps)
                    continue

            # Recovery trigger: both signals at or below 50% of their high
            # thresholds for `recovery_dwell_s`, no decrease in dwell.
            calm = (
                put_p95 <= cfg.put_latency_low_ms
                and rtt_p95 <= cfg.rtt_low_ms
                # require some samples in the rtt window; put_latency drives
                # the camera path so the put window naturally has data
                and s["rtt_win"].count() > 0
            )
            if calm:
                if s["low_since"] == 0.0:
                    s["low_since"] = now
                low_dwell = now - s["low_since"]
                if (
                    low_dwell >= cfg.recovery_dwell_s
                    and (now - s["last_adjust"]) >= cfg.dwell_s
                ):
                    current = s["target_kbps"]
                    candidate = int(round(current * cfg.recovery_factor))
                    new_kbps = min(cfg.max_kbps, candidate)
                    # Make at least 1 kbps of forward progress so we don't
                    # stall when recovery_factor rounds to current.
                    if new_kbps <= current and current < cfg.max_kbps:
                        new_kbps = min(cfg.max_kbps, current + 1)
                    if new_kbps > current:
                        logger.info(
                            f"[{vid}] ABR UP: "
                            f"calm for {low_dwell:.1f}s (put_p95={put_p95:.0f}, "
                            f"rtt_p95={rtt_p95:.0f}) -> "
                            f"{current} -> {new_kbps} kbps"
                        )
                        s["target_kbps"] = new_kbps
                        s["last_adjust"] = now
                        s["low_since"] = now
                        self._publish(vid, new_kbps)
            else:
                s["low_since"] = 0.0

    # ------------------------------------------------------------------ #
    # Diagnostics
    # ------------------------------------------------------------------ #

    def log_snapshot(self) -> None:
        now = time.monotonic()
        if (now - self._last_snapshot_log) < self._snapshot_interval_s:
            return
        self._last_snapshot_log = now

        for vid in list(self._state.vehicles.keys()):
            veh = self._state.vehicles.get(vid)
            if veh is None:
                continue
            s = self._veh.get(vid)
            if s is None:
                continue

            put_p95 = s["put_lat_win"].p95(now)
            rtt_p95 = s["rtt_win"].p95(now)
            low_dwell = (now - s["low_since"]) if s["low_since"] > 0.0 else 0.0
            n_cams = len(veh.cameras)
            client_latency = float(s.get("client_latency_p95_ms", 0.0))
            client_freeze = float(s.get("client_freeze_ms", 0.0))
            logger.info(
                f"[ABR-TCP {vid}] target={s['target_kbps']}kbps "
                f"init={int(s['initialized'])} cams={n_cams} "
                f"put_p95={put_p95:.0f}ms rtt_p95={rtt_p95:.0f}ms "
                f"client[lat_p95={client_latency:.0f}ms freeze1s={client_freeze:.0f}ms] "
                f"low_dwell={low_dwell:.1f}s"
            )
