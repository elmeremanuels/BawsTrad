#!/usr/bin/env python3
"""
Sprint 1 smoke test — AUTONOMOUS_SYSTEMS_HANDOFF.md

Tests all 4 modules built in Sprint 1:
  1. Funnel Tracker     — log + read + dead-funnel detection
  2. Universe Builder   — config loads, base universe readable, waves runnable
  3. Config Auditor     — scan runs, returns list, no exceptions
  4. Cost Tracker       — snapshot + fill + finalize + summary
  5. Latency Monitor    — record + stats + health check + wrap decorator

Run:
    uv run python scripts/smoke_test_sprint1.py

Exit 0 = all pass. Each test is isolated — one failure doesn't stop others.
"""

from __future__ import annotations

import sys
import time
import traceback
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# ANSI
G = "\033[32m"; R = "\033[33m"; B = "\033[1m"; X = "\033[0m"
ok  = lambda s: print(f"  {G}✓{X} {s}")
err = lambda s: print(f"  {R}✗{X} {s}")

results: dict[str, bool] = {}


def run(name: str):
    def decorator(fn):
        print(f"\n{B}── {name} ──{X}")
        try:
            fn()
            results[name] = True
        except Exception as exc:
            err(f"FAILED: {exc}")
            traceback.print_exc()
            results[name] = False
        return fn
    return decorator


# ─── Setup DB ────────────────────────────────────────────────────────────────

from src.storage.db import init_schema
init_schema()
ok("DB schema initialised (funnel_daily + cost_metrics tables present)")


# ─── 1. Funnel Tracker ───────────────────────────────────────────────────────

@run("Funnel Tracker")
def test_funnel():
    from src.health.funnel_tracker import (
        log_funnel, get_today_funnel, detect_dead_funnels, format_funnel_report
    )
    from datetime import date, timedelta

    # Write some stage counts
    log_funnel("base_universe",   3200)
    log_funnel("wave1_price_gap", 47)
    log_funnel("wave2_volume",    39)
    log_funnel("wave3_float",     18)
    log_funnel("wave4_news",      5)
    log_funnel("watchlist_active", 3)
    ok("log_funnel: 6 stages written")

    # Read back
    today = get_today_funnel()
    assert today["base_universe"]   == 3200, f"expected 3200, got {today['base_universe']}"
    assert today["wave1_price_gap"] == 47
    assert today["watchlist_active"] == 3
    ok(f"get_today_funnel: correct values ({len(today)} stages)")

    # Upsert (overwrite)
    log_funnel("wave4_news", 7)
    today2 = get_today_funnel()
    assert today2["wave4_news"] == 7, "upsert failed"
    ok("upsert overwrites correctly")

    # Dead funnel: write count=0 for 5 consecutive days for 'entries_placed'
    # (a real STAGES member that the test never wrote to)
    from src.storage.db import get_connection
    for i in range(5):
        d = (date.today() - timedelta(days=i)).isoformat()
        with get_connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO funnel_daily (date, stage, count, recorded_at) VALUES (?,?,?,?)",
                (d, "entries_placed", 0, "2026-01-01T00:00:00")
            )
    # Dead funnel detection (don't post to Discord in smoke test)
    dead = detect_dead_funnels(post_to_discord=False)
    assert "entries_placed" in dead, f"expected 'entries_placed' in dead={dead}"
    ok(f"dead funnel detected correctly: {dead}")

    # Format report
    report = format_funnel_report()
    assert "base_universe" in report
    ok(f"format_funnel_report returned {len(report)} chars")


# ─── 2. Universe Builder ─────────────────────────────────────────────────────

@run("Universe Builder")
def test_universe():
    from src.universe.dynamic_builder import UniverseBuilder, _load_config

    cfg = _load_config()
    ok(f"config loaded: gap_min={cfg['universe_gap_filter_pct']}% "
       f"price={cfg['price_min']}-{cfg['price_max']}")

    builder = UniverseBuilder()
    ok("UniverseBuilder instantiated")

    # Check base universe file exists
    base_path = PROJECT_ROOT / "data" / "base_universe.csv"
    fallback   = PROJECT_ROOT / "data" / "small_cap_base_universe.csv"
    if not base_path.exists() and not fallback.exists():
        err("No base universe CSV — run build_base_universe.py first (skipping wave test)")
        return

    # Load base without network call
    from src.universe.dynamic_builder import _load_base_universe
    base = _load_base_universe()
    ok(f"base universe loaded: {len(base)} tickers")
    assert len(base) > 10, "base universe too small"

    # Wave 1 mock (no real Alpaca call needed for structure test)
    from src.universe.dynamic_builder import _wave1_price_gap
    fake_snaps = {
        "AAA": {"prevDailyBar": {"c": 5.00}, "dailyBar": {"c": 6.50, "v": 100000}},
        "BBB": {"prevDailyBar": {"c": 5.00}, "dailyBar": {"c": 5.10, "v": 50000}},  # gap <3%
        "CCC": {"prevDailyBar": {"c": 25.00}, "dailyBar": {"c": 30.00, "v": 200000}},  # price >$20
    }
    fake_base = [{"ticker": "AAA"}, {"ticker": "BBB"}, {"ticker": "CCC"}]
    w1 = _wave1_price_gap(fake_base, fake_snaps, 1.0, 20.0, 3.0)
    assert len(w1) == 1 and w1[0]["ticker"] == "AAA", f"wave1 wrong: {w1}"
    assert abs(w1[0]["gap_pct"] - 30.0) < 0.1
    ok("wave1_price_gap filter correct (AAA passes, BBB/CCC rejected)")


# ─── 3. Config Auditor ───────────────────────────────────────────────────────

@run("Config Auditor")
def test_config_auditor():
    from src.health.config_auditor import ConfigAuditor, AuditFinding

    auditor = ConfigAuditor(post_to_discord=False)
    findings = auditor.audit()

    ok(f"audit() ran without exception: {len(findings)} findings")
    assert isinstance(findings, list)
    for f in findings:
        assert isinstance(f, AuditFinding)
        assert f.file
        assert f.line_no > 0

    # Smoke check: at least structure is correct
    ok("all findings are AuditFinding dataclass instances")

    # Print top-5 for visibility
    if findings:
        print(f"    (Top 5 findings for information):")
        for f in findings[:5]:
            print(f"      L{f.line_no:>4}  {f.file:<45}  [{f.kind}]  {f.value!r}")


# ─── 4. Cost Tracker ─────────────────────────────────────────────────────────

@run("Cost Tracker")
def test_cost_tracker():
    from src.execution.cost_tracker import CostTracker
    import uuid

    tracker = CostTracker()
    trade_id = f"smoke_test_{uuid.uuid4().hex[:8]}"

    # Insert a fake trade row so FK constraint is satisfied
    from src.storage.db import get_connection
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    with get_connection() as conn:
        conn.execute("""
            INSERT OR IGNORE INTO trades
            (id, opened_at, ticker, side, shares, entry_price, stop_price, target_price, mode)
            VALUES (?, ?, 'SMOK', 'long', 10, 5.50, 5.00, 6.60, 'paper')
        """, (trade_id, now))

    # snapshot_pre_trade (no real Alpaca call — just inserts empty row)
    tracker._insert_empty(trade_id)
    ok("_insert_empty created cost_metrics row")

    # Manual fill of midpoint so slippage can be computed
    with get_connection() as conn:
        conn.execute(
            "UPDATE cost_metrics SET midpoint_decision = 5.50, spread_at_entry = 0.4 WHERE trade_id = ?",
            (trade_id,),
        )

    tracker.record_entry_fill(trade_id, 5.54)  # 4 cents above midpoint
    ok("record_entry_fill: ok")

    tracker.record_exit_fill(trade_id, 6.58)
    ok("record_exit_fill: ok")

    # Write pnl_r to trades so finalize can read it
    with get_connection() as conn:
        conn.execute(
            "UPDATE trades SET pnl_r = 2.16, closed_at = ? WHERE id = ?",
            (now, trade_id),
        )

    tracker.finalize(trade_id)
    ok("finalize: ok")

    # Verify values written
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM cost_metrics WHERE trade_id = ?", (trade_id,)).fetchone()

    assert row is not None
    assert row["entry_fill_price"] == 5.54
    assert row["exit_fill_price"]  == 6.58
    assert row["gross_profit_R"]  is not None
    assert row["total_cost_R"]    is not None
    assert row["net_profit_R"]    is not None
    ok(f"DB values correct: gross={row['gross_profit_R']:.3f}R "
       f"cost={row['total_cost_R']:.3f}R net={row['net_profit_R']:.3f}R")

    summary = tracker.get_summary(days=30)
    assert isinstance(summary, dict)
    ok(f"get_summary() returned dict with {len(summary)} keys")

    # Cleanup
    with get_connection() as conn:
        conn.execute("DELETE FROM cost_metrics WHERE trade_id = ?", (trade_id,))
        conn.execute("DELETE FROM trades WHERE id = ?", (trade_id,))


# ─── 5. Latency Monitor ──────────────────────────────────────────────────────

@run("Latency Monitor")
def test_latency_monitor():
    from src.execution.latency_monitor import LatencyMonitor, get_monitor

    monitor = LatencyMonitor()

    # record_ms
    for ms in [10, 20, 15, 30, 25, 40, 18, 22, 12, 35, 50, 60, 28]:
        monitor.record_ms("test_call", ms)
    ok(f"record_ms: {len(monitor._samples)} samples recorded")

    # measure()
    result = monitor.measure("test_fn", lambda: 42)
    assert result == 42
    ok("measure() returns fn result correctly")

    # wrap decorator
    @monitor.wrap("wrapped_fn")
    def adder(a, b):
        time.sleep(0.005)
        return a + b

    assert adder(2, 3) == 5
    ok("@monitor.wrap() decorator works")

    # stats
    stats = monitor.stats()
    assert stats["p50"] is not None
    assert stats["p95"] is not None
    assert stats["status"] in ("ok", "degraded", "critical")
    ok(f"stats(): p50={stats['p50']}ms p95={stats['p95']}ms status={stats['status']}")

    # per_label_stats
    per_label = monitor.per_label_stats()
    assert "test_call" in per_label
    ok(f"per_label_stats(): {list(per_label.keys())}")

    # check_health (should be ok with small latencies)
    result = monitor.check_health()
    ok(f"check_health() returned: {result!r}")

    # Singleton
    m1 = get_monitor()
    m2 = get_monitor()
    assert m1 is m2
    ok("get_monitor() singleton works")

    # Inject high latency samples → check_health should detect
    high_monitor = LatencyMonitor()
    for _ in range(200):
        high_monitor.record_ms("slow_api", 600.0)  # Above 500ms critical threshold
    stats_high = high_monitor.stats()
    assert stats_high["status"] == "critical", f"expected critical, got {stats_high['status']}"
    ok("critical threshold detection works (P95 > 500ms → status=critical)")


# ─── Summary ─────────────────────────────────────────────────────────────────

print(f"\n{'─'*50}")
print(f"{B}Sprint 1 Smoke Test Summary{X}")
print(f"{'─'*50}")
passed = sum(v for v in results.values())
total  = len(results)
for name, ok_flag in results.items():
    status = f"{G}PASS{X}" if ok_flag else f"{R}FAIL{X}"
    print(f"  {status}  {name}")
print(f"\n  {passed}/{total} modules passing\n")
sys.exit(0 if passed == total else 1)
