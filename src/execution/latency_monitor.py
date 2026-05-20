from __future__ import annotations

"""
Latency Monitor — System 4 (PROBLEEM 4).

Measures round-trip latency for every Alpaca API call (REST + WebSocket).
Exposes percentile stats for the dashboard. Auto-pauses the bot and
posts a Discord alert if P95 latency exceeds the critical threshold.

Design:
  - Singleton: use get_monitor() everywhere
  - Thread-safe deque, maxlen=1000 samples (rolling)
  - Auto-pause via kill-switch file (same mechanism as manual kill switch)
  - Dashboard reads .stats() dict

Usage:
    from src.execution.latency_monitor import get_monitor

    monitor = get_monitor()

    # Wrap any blocking API call:
    result = monitor.measure("alpaca_snapshot", my_fn, arg1, kw=kw)

    # Or use as decorator:
    @monitor.wrap("finnhub_profile")
    def fetch_profile(ticker):
        ...

    # Health check (called from main loop every 60s):
    monitor.check_health()

    # Dashboard:
    stats = monitor.stats()  # {"p50": 45.2, "p95": 120.1, "p99": 280.0, ...}
"""

import functools
import logging
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# Thresholds (ms) — from handoff spec
_P95_WARN_MS     = 200   # Discord warning
_P95_CRITICAL_MS = 500   # Discord alert + auto-pause

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_PAUSE_FLAG   = _PROJECT_ROOT / "state" / "bot_paused.flag"
_PAUSE_REASON = _PROJECT_ROOT / "state" / "pause_reason.txt"

_DISCORD_COOLDOWN_S = 300  # Don't spam Discord more than once per 5 min


@functools.lru_cache(maxsize=1)
def get_monitor() -> "LatencyMonitor":
    """Return the process-wide singleton LatencyMonitor."""
    return LatencyMonitor()


class LatencyMonitor:
    """
    Rolling latency tracker with auto-pause capability.
    Thread-safe (deque append/read is GIL-atomic in CPython).
    """

    def __init__(self, maxlen: int = 1000) -> None:
        self._samples:      deque = deque(maxlen=maxlen)
        self._labels:       deque = deque(maxlen=maxlen)  # parallel to _samples
        self._last_warn_ts: float = 0.0
        self._paused:       bool  = False

    # ── Measurement API ───────────────────────────────────────────────────────

    def measure(self, label: str, fn: Callable, *args: Any, **kwargs: Any) -> Any:
        """
        Call `fn(*args, **kwargs)`, record its wall-clock latency, return result.
        Exceptions from fn propagate normally.
        """
        t0 = time.perf_counter()
        try:
            return fn(*args, **kwargs)
        finally:
            latency_ms = (time.perf_counter() - t0) * 1000.0
            self._record(label, latency_ms)

    def wrap(self, label: str) -> Callable:
        """Decorator: @monitor.wrap('label') wraps any function."""
        def decorator(fn: Callable) -> Callable:
            @functools.wraps(fn)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                return self.measure(label, fn, *args, **kwargs)
            return wrapper
        return decorator

    def record_ms(self, label: str, latency_ms: float) -> None:
        """Manually record a pre-measured latency (e.g. from async timing)."""
        self._record(label, latency_ms)

    # ── Stats ─────────────────────────────────────────────────────────────────

    def stats(self) -> Dict[str, Any]:
        """
        Return current rolling stats dict.
        Safe to call from any thread; returns empty dict if < 10 samples.
        """
        samples = list(self._samples)  # snapshot
        if len(samples) < 10:
            return {
                "n_samples": len(samples),
                "p50": None, "p95": None, "p99": None,
                "avg": None, "max": None,
                "status": "collecting" if samples else "empty",
            }

        sorted_s = sorted(samples)
        n = len(sorted_s)

        def _pct(p: float) -> float:
            idx = int(n * p / 100)
            return round(sorted_s[min(idx, n - 1)], 1)

        avg = round(sum(sorted_s) / n, 1)
        mx  = round(sorted_s[-1], 1)
        p95 = _pct(95)

        status = "ok"
        if p95 > _P95_CRITICAL_MS:
            status = "critical"
        elif p95 > _P95_WARN_MS:
            status = "degraded"

        return {
            "n_samples": n,
            "p50":       _pct(50),
            "p95":       p95,
            "p99":       _pct(99),
            "avg":       avg,
            "max":       mx,
            "status":    status,
        }

    def per_label_stats(self) -> Dict[str, Dict]:
        """Return stats broken down by label. Used by dashboard detail panel."""
        from collections import defaultdict
        buckets: Dict[str, List[float]] = defaultdict(list)
        for label, ms in zip(self._labels, self._samples):
            buckets[label].append(ms)

        result = {}
        for label, ms_list in buckets.items():
            if not ms_list:
                continue
            ms_sorted = sorted(ms_list)
            n = len(ms_sorted)
            result[label] = {
                "n":   n,
                "avg": round(sum(ms_sorted) / n, 1),
                "p95": round(ms_sorted[min(int(n * 0.95), n - 1)], 1),
            }
        return result

    # ── Health check ─────────────────────────────────────────────────────────

    def check_health(self) -> Optional[str]:
        """
        Compare current P95 against thresholds.
        Returns a status string (for logging), and posts Discord / sets pause
        flag when critical. Call from main loop every ~60s.
        """
        s = self.stats()
        if s.get("status") == "collecting":
            return None

        p95 = s.get("p95")
        if p95 is None:
            return None

        now_ts = time.monotonic()

        if p95 > _P95_CRITICAL_MS and not self._paused:
            self._pause_bot(p95)
            return f"CRITICAL: P95={p95:.0f}ms"

        if p95 > _P95_WARN_MS:
            if now_ts - self._last_warn_ts > _DISCORD_COOLDOWN_S:
                self._last_warn_ts = now_ts
                self._discord_warn(p95)
            return f"DEGRADED: P95={p95:.0f}ms"

        if self._paused and p95 < _P95_WARN_MS:
            logger.info("latency_monitor: latency recovered (P95=%.0fms), pause flag left for manual review", p95)

        return f"OK: P95={p95:.0f}ms"

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _record(self, label: str, latency_ms: float) -> None:
        self._samples.append(latency_ms)
        self._labels.append(label)
        if latency_ms > _P95_WARN_MS:
            logger.debug("latency_monitor: %s %.0fms (above warn threshold)", label, latency_ms)

    def _pause_bot(self, p95_ms: float) -> None:
        self._paused = True
        msg = f"latency_p95_critical:{p95_ms:.0f}ms"
        try:
            _PAUSE_FLAG.parent.mkdir(parents=True, exist_ok=True)
            _PAUSE_FLAG.touch()
            _PAUSE_REASON.write_text(msg)
        except Exception as exc:
            logger.warning("latency_monitor: could not write pause flag: %s", exc)

        logger.critical("latency_monitor: AUTO-PAUSE triggered — %s", msg)
        self._discord_critical(p95_ms)

    def _discord_warn(self, p95_ms: float) -> None:
        try:
            import asyncio
            from src.alerts.discord import post_message
            from src.config import settings
            asyncio.run(post_message(
                settings.DISCORD_WEBHOOK_URL,
                f"⚠️ **API latency degraded**: P95 = {p95_ms:.0f}ms (warn >{_P95_WARN_MS}ms)",
            ))
        except Exception as exc:
            logger.debug("latency_monitor: discord warn failed: %s", exc)

    def _discord_critical(self, p95_ms: float) -> None:
        try:
            import asyncio
            from src.alerts.discord import post_message
            from src.config import settings
            asyncio.run(post_message(
                settings.DISCORD_WEBHOOK_URL,
                f"🚨 **AUTO-PAUSE**: Latency P95 = {p95_ms:.0f}ms "
                f"(critical >{_P95_CRITICAL_MS}ms)\n"
                f"Bot paused. Remove `state/bot_paused.flag` to resume.",
            ))
        except Exception as exc:
            logger.debug("latency_monitor: discord critical failed: %s", exc)
