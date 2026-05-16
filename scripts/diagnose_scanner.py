#!/usr/bin/env python3
"""
scripts/diagnose_scanner.py — Scanner pipeline diagnostics.

Traces the full watchlist-building pipeline step by step without placing any
orders or requiring market hours. Safe to run at any time.

Usage:
    uv run python scripts/diagnose_scanner.py
    uv run python scripts/diagnose_scanner.py --finnhub   # also test Finnhub (slow, 60 tickers)
    uv run python scripts/diagnose_scanner.py --ticker SIGA  # single-ticker deep dive

Output: coloured table per ticker showing exactly which criterion blocks it.
Exit 0 always — this is diagnostic, not a pass/fail test.
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

from src.config import settings  # loads .env
from src.scanner.criteria import (
    NewsCatalyst, TickerSnapshot,
    passes_stock_selection, quality_score,
)

# ── ANSI colours ─────────────────────────────────────────────────────────────
GREEN  = "\033[32m"
RED    = "\033[31m"
YELLOW = "\033[33m"
CYAN   = "\033[36m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

def _ok(s: str) -> str:  return f"{GREEN}✓ {s}{RESET}"
def _fail(s: str) -> str: return f"{RED}✗ {s}{RESET}"
def _warn(s: str) -> str: return f"{YELLOW}~ {s}{RESET}"
def _h(s: str) -> str:   return f"{BOLD}{CYAN}{s}{RESET}"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_universe() -> list[dict]:
    p = _ROOT / "small_cap_runners.csv"
    if not p.exists():
        print(_fail("small_cap_runners.csv not found"))
        return []
    with open(p) as f:
        return list(csv.DictReader(f))


def _check_api_keys() -> dict[str, bool]:
    results = {}
    results["ALPACA_API_KEY"]    = bool(settings.ALPACA_API_KEY)
    results["ALPACA_API_SECRET"] = bool(settings.ALPACA_API_SECRET)
    results["FINNHUB_API_KEY"]   = bool(getattr(settings, "FINNHUB_API_KEY", ""))
    return results


def _get_snapshots(tickers: list[str]) -> dict:
    from src.data.alpaca_client import get_snapshots
    try:
        return get_snapshots(tickers)
    except Exception as exc:
        print(_fail(f"Alpaca snapshot failed: {exc}"))
        return {}


def _test_finnhub(ticker: str) -> tuple[bool, str]:
    """Test Finnhub news fetch for one ticker. Returns (ok, summary)."""
    from src.news.ingest import ingest_news_for_ticker
    try:
        items = ingest_news_for_ticker(ticker)
        return True, f"{len(items)} items ingested"
    except Exception as exc:
        return False, str(exc)[:80]


def _evaluate_ticker(
    ticker: str,
    snap_data: dict,
    float_shares: int,
    catalyst: Optional[NewsCatalyst],
    verbose: bool = False,
) -> dict:
    """
    Evaluate all criteria and return a diagnosis dict.
    """
    prev_daily = snap_data.get("prevDailyBar") or {}
    daily      = snap_data.get("dailyBar") or {}
    latest_bar = snap_data.get("latestBar") or {}
    latest_trade = snap_data.get("latestTrade") or {}

    prev_close = prev_daily.get("c") or 0.0
    # Use latest trade price if dailyBar.c not available (pre-market)
    price = (
        daily.get("c")
        or latest_bar.get("c")
        or latest_trade.get("p")
        or 0.0
    )
    price = float(price) if price else 0.0

    gap_pct = 0.0
    if prev_close > 0 and price > 0:
        gap_pct = (price - prev_close) / prev_close * 100.0

    # Build snapshot (rel_vol hardcoded 5.0 like the real scanner)
    snap = TickerSnapshot(
        ticker=ticker,
        price=price,
        percent_change_today=gap_pct,
        relative_volume=5.0,
        float_shares=float_shares,
        news_catalyst=catalyst,
    )

    # Individual criterion results
    c1_price   = 1.00 <= price <= 20.00
    c2_gap     = gap_pct >= 10.0
    c3_relvol  = True  # always 5.0 pre-market
    c4_news    = catalyst is not None and catalyst.tier in ("A", "B")
    c5_float   = float_shares < 20_000_000
    passes_all = passes_stock_selection(snap)
    score      = quality_score(snap) if passes_all else 0.0

    return {
        "ticker":      ticker,
        "price":       price,
        "prev_close":  prev_close,
        "gap_pct":     gap_pct,
        "float_shares":float_shares,
        "catalyst":    catalyst,
        "has_data":    price > 0 and prev_close > 0,
        "c1_price":    c1_price,
        "c2_gap":      c2_gap,
        "c3_relvol":   c3_relvol,
        "c4_news":     c4_news,
        "c5_float":    c5_float,
        "passes_all":  passes_all,
        "score":       score,
        "raw_snap":    snap_data if verbose else {},
    }


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Scanner pipeline diagnostics")
    parser.add_argument("--finnhub", action="store_true",
                        help="Also test Finnhub news for all tickers (slow: ~60s)")
    parser.add_argument("--ticker", metavar="SYM",
                        help="Deep-dive a single ticker (shows raw Alpaca response)")
    parser.add_argument("--all", action="store_true",
                        help="Show all tickers, not just those with price data")
    args = parser.parse_args()

    print(f"\n{_h('═══ SCANNER PIPELINE DIAGNOSTICS ═══')}\n")

    # ── 1. API keys ──────────────────────────────────────────────────────
    print(_h("1. API keys"))
    keys = _check_api_keys()
    for k, ok in keys.items():
        print(f"   {_ok(k) if ok else _fail(k + ' NOT SET')}")
    if not keys["ALPACA_API_KEY"]:
        print(_fail("Cannot continue without Alpaca keys. Set in .env"))
        sys.exit(1)

    # ── 2. Universe ──────────────────────────────────────────────────────
    print(f"\n{_h('2. Universe')}")
    universe = _load_universe()
    if args.ticker:
        universe = [r for r in universe if r["ticker"].upper() == args.ticker.upper()]
        if not universe:
            universe = [{"ticker": args.ticker.upper(), "float_shares": "10000000", "sector": "?"}]
    float_map = {r["ticker"]: int(r.get("float_shares", 10_000_000)) for r in universe}
    tickers   = [r["ticker"] for r in universe]
    print(f"   {len(tickers)} tickers loaded")

    # ── 3. Alpaca snapshots ──────────────────────────────────────────────
    print(f"\n{_h('3. Alpaca snapshots')}")
    snaps = _get_snapshots(tickers)
    print(f"   Received data for {len(snaps)}/{len(tickers)} tickers")
    missing = [t for t in tickers if t not in snaps]
    if missing:
        print(_warn(f"   No data for: {', '.join(missing[:10])}" +
                    (f" … (+{len(missing)-10} more)" if len(missing) > 10 else "")))

    # ── 4. Finnhub news (optional) ────────────────────────────────────────
    catalysts: dict[str, Optional[NewsCatalyst]] = {}
    if args.finnhub or args.ticker:
        print(f"\n{_h('4. Finnhub news (testing first 5 tickers)')}")
        if not keys["FINNHUB_API_KEY"]:
            print(_warn("   FINNHUB_API_KEY not set — all tickers will fail C4 (news catalyst)"))
        else:
            from src.news.ingest import ingest_news_for_ticker, get_best_catalyst_today
            test_batch = tickers[:5] if not args.ticker else tickers
            for t in test_batch:
                ok, msg = _test_finnhub(t)
                icon = _ok(t) if ok else _fail(t)
                print(f"   {icon}: {msg}")
                best = get_best_catalyst_today(t)
                if best:
                    catalysts[t] = NewsCatalyst(
                        tier=best["tier"],
                        category=best["category"],
                        headline=best["headline"],
                    )
    else:
        print(f"\n{_h('4. Finnhub news')}")
        if not keys["FINNHUB_API_KEY"]:
            print(_warn("   FINNHUB_API_KEY not set → ALL tickers fail C4 (news catalyst)"))
            print(_warn("   This is the most likely reason the watchlist is empty."))
            print( "   Add FINNHUB_API_KEY=<key> to .env to enable news filtering.")
        else:
            print("   Skipped (pass --finnhub to test, takes ~60s for full universe)")
            # Still load from DB if available
            try:
                from src.news.ingest import get_best_catalyst_today
                for t in tickers:
                    best = get_best_catalyst_today(t)
                    if best:
                        catalysts[t] = NewsCatalyst(
                            tier=best["tier"],
                            category=best["category"],
                            headline=best["headline"][:60],
                        )
            except Exception:
                pass
            print(f"   Found existing catalysts in DB for {len(catalysts)} tickers")

    # ── 5. Criterion-by-criterion analysis ────────────────────────────────
    print(f"\n{_h('5. Ticker analysis')}")
    print(f"   {'TICKER':<6} {'PRICE':>6} {'GAP%':>6} {'FLOAT':>8}  "
          f"{'C1':>3} {'C2':>3} {'C3':>3} {'C4':>3} {'C5':>3}  {'STATUS'}")
    print("   " + "─" * 65)

    results = []
    for t in tickers:
        snap_data = snaps.get(t, {})
        catalyst  = catalysts.get(t)
        r = _evaluate_ticker(t, snap_data, float_map.get(t, 10_000_000), catalyst,
                             verbose=bool(args.ticker))
        results.append(r)

        if not r["has_data"] and not args.all:
            continue  # Skip no-data tickers unless --all

        def _c(ok: bool) -> str:
            return GREEN + "✓" + RESET if ok else RED + "✗" + RESET

        gap_str = f"{r['gap_pct']:+.1f}%" if r["has_data"] else "no data"
        float_str = f"{r['float_shares']//1_000_000:.0f}M"
        status = _ok("WATCHLIST") if r["passes_all"] else ""
        if not status:
            # First failing criterion
            for c, label in [("c1_price","price"), ("c2_gap","gap<10%"),
                              ("c3_relvol","relvol"), ("c4_news","no catalyst"),
                              ("c5_float","float>20M")]:
                if not r[c]:
                    status = _fail(label)
                    break

        print(f"   {t:<6} {r['price']:>6.2f} {gap_str:>7}  {float_str:>7}  "
              f"{_c(r['c1_price'])}   {_c(r['c2_gap'])}   "
              f"{_c(r['c3_relvol'])}   {_c(r['c4_news'])}   "
              f"{_c(r['c5_float'])}  {status}")

        if args.ticker and r["raw_snap"]:
            print(f"\n   Raw Alpaca snapshot for {t}:")
            import json
            for k, v in r["raw_snap"].items():
                print(f"     {k}: {json.dumps(v, indent=None)}")
            print()

    # ── 6. Summary ───────────────────────────────────────────────────────
    print(f"\n{_h('6. Summary')}")
    has_data  = [r for r in results if r["has_data"]]
    gap_10    = [r for r in has_data if r["c2_gap"]]
    gap_5     = [r for r in has_data if r["gap_pct"] >= 5.0]
    pass_all  = [r for r in results if r["passes_all"]]
    no_news   = [r for r in has_data if r["c2_gap"] and not r["c4_news"]]

    print(f"   Tickers in universe        : {len(results)}")
    print(f"   With Alpaca price data     : {len(has_data)}")
    print(f"   Gapping ≥5% (soft)         : {len(gap_5)}")
    print(f"   Gapping ≥10% (hard filter) : {len(gap_10)}")
    print(f"   Pass ALL 5 criteria        : {len(pass_all)} → {'WATCHLIST POPULATED ✓' if pass_all else 'WATCHLIST EMPTY'}")

    if no_news:
        print(f"\n   {YELLOW}Tickers that gap ≥10% but lack news catalyst:{RESET}")
        for r in sorted(no_news, key=lambda x: x["gap_pct"], reverse=True):
            print(f"     {r['ticker']:6} gap={r['gap_pct']:+.1f}% price={r['price']:.2f}")
        print(f"   → Set require_news_catalyst: false in config.yaml to trade these")

    if pass_all:
        print(f"\n   {GREEN}Watchlist candidates:{RESET}")
        for r in sorted(pass_all, key=lambda x: x["score"], reverse=True):
            cat = r["catalyst"]
            print(f"     {r['ticker']:6} gap={r['gap_pct']:+.1f}%  "
                  f"score={r['score']:.1f}  news=[{cat.tier}] {cat.headline[:50] if cat else '—'}")

    if not keys["FINNHUB_API_KEY"]:
        print(f"\n   {RED}ROOT CAUSE: FINNHUB_API_KEY not set.{RESET}")
        print( "   With require_news_catalyst: true (default), every ticker fails C4.")
        print( "   → Option 1: add FINNHUB_API_KEY to .env")
        print( "   → Option 2: set require_news_catalyst: false in config.yaml")

    print()


if __name__ == "__main__":
    main()
