#!/usr/bin/env python3
"""
Daily pre-market universe refresh.

Reads data/small_cap_base_universe.csv, calls Alpaca snapshots in batches,
filters for gapping tickers, and writes universe_today.csv.

Usage:
    uv run python scripts/refresh_universe.py

Always exits 0. universe_today.csv is written to the project root (same
directory as small_cap_runners.csv) so main.py can find it with Path("universe_today.csv").
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

# Ensure project root is on sys.path so src imports work when run standalone
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


MIN_PRICE = 1.00
MAX_PRICE = 20.00
MIN_GAP_PCT = 5.0
TOP_N_FALLBACK = 30  # Always write at least this many (by gap%) even below filter


def _load_base_universe() -> list[dict]:
    base_path = PROJECT_ROOT / "data" / "small_cap_base_universe.csv"
    if not base_path.exists():
        print(f"[refresh_universe] ERROR: base universe not found at {base_path}", flush=True)
        return []

    rows = []
    with open(base_path, newline="") as f:
        for row in csv.DictReader(f):
            ticker = row.get("ticker", "").strip().upper()
            if ticker:
                rows.append({
                    "ticker": ticker,
                    "float_shares": row.get("float_shares", "15000000").strip(),
                    "sector": row.get("sector", "Unknown").strip(),
                })
    return rows


def _fetch_snapshots(tickers: list[str]) -> dict:
    """Call Alpaca get_snapshots() in batches of 100. Returns {ticker: snap_dict}."""
    from src.data.alpaca_client import get_snapshots

    result = {}
    batch_size = 100
    total_batches = (len(tickers) + batch_size - 1) // batch_size

    for i in range(0, len(tickers), batch_size):
        batch = tickers[i : i + batch_size]
        batch_num = i // batch_size + 1
        print(
            f"[refresh_universe] Fetching batch {batch_num}/{total_batches} "
            f"({len(batch)} tickers)...",
            flush=True,
        )
        try:
            snaps = get_snapshots(batch)
            result.update(snaps)
        except Exception as exc:
            print(f"[refresh_universe] WARNING: batch {batch_num} failed: {exc}", flush=True)

    return result


def _compute_gap(snap: dict) -> tuple[float, float]:
    """Return (price, gap_pct) from a snapshot dict. Returns (0, 0) if data missing."""
    prev_close = (snap.get("prevDailyBar") or {}).get("c", 0)
    daily = snap.get("dailyBar") or {}
    price = daily.get("c") or daily.get("o") or 0

    if prev_close <= 0 or price <= 0:
        return 0.0, 0.0

    gap_pct = (price - prev_close) / prev_close * 100.0
    return float(price), float(gap_pct)


def main() -> None:
    print("[refresh_universe] Loading base universe...", flush=True)
    base_rows = _load_base_universe()
    if not base_rows:
        print("[refresh_universe] No base universe tickers — aborting.", flush=True)
        sys.exit(0)

    tickers = [r["ticker"] for r in base_rows]
    float_map = {r["ticker"]: r["float_shares"] for r in base_rows}
    sector_map = {r["ticker"]: r["sector"] for r in base_rows}

    print(f"[refresh_universe] Base universe: {len(tickers)} tickers", flush=True)

    # Fetch snapshots
    snaps = _fetch_snapshots(tickers)
    total_scanned = len(snaps)
    print(f"[refresh_universe] Got snapshots for {total_scanned} tickers", flush=True)

    # Score every ticker with a snapshot
    scored: list[tuple[float, float, str]] = []  # (gap_pct, price, ticker)
    for ticker, snap in snaps.items():
        price, gap_pct = _compute_gap(snap)
        if price > 0:
            scored.append((gap_pct, price, ticker))

    # Sort descending by gap%
    scored.sort(reverse=True)

    # Primary filter: price in range AND gap >= 5%
    passed: list[tuple[float, float, str]] = [
        (g, p, t)
        for g, p, t in scored
        if MIN_PRICE <= p <= MAX_PRICE and g >= MIN_GAP_PCT
    ]
    gapping_count = len(passed)

    # Build final output set: union of passed + top-N fallback (by gap%)
    # The top-N fallback ensures the scanner always has tickers to work with
    # even on low-volatility days when few tickers pass the 5% filter.
    output_set: dict[str, tuple[float, float]] = {}  # ticker -> (gap_pct, price)

    for gap_pct, price, ticker in passed:
        output_set[ticker] = (gap_pct, price)

    # Add top-N from full scored list if we're short
    for gap_pct, price, ticker in scored:
        if len(output_set) >= max(TOP_N_FALLBACK, gapping_count):
            break
        if ticker not in output_set:
            output_set[ticker] = (gap_pct, price)

    # Write universe_today.csv to project root
    out_path = PROJECT_ROOT / "universe_today.csv"
    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["ticker", "float_shares", "sector"])
        writer.writeheader()
        # Order: passed first (sorted by gap%), then fallback additions
        ordered = sorted(output_set.items(), key=lambda kv: kv[1][0], reverse=True)
        for ticker, (gap_pct, price) in ordered:
            writer.writerow({
                "ticker": ticker,
                "float_shares": float_map.get(ticker, "15000000"),
                "sector": sector_map.get(ticker, "Unknown"),
            })

    print(
        f"[refresh_universe] Summary: "
        f"scanned={total_scanned} "
        f"gapping_5pct={gapping_count} "
        f"written={len(output_set)} "
        f"-> {out_path}",
        flush=True,
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[refresh_universe] FATAL: {exc}", flush=True)
        import traceback
        traceback.print_exc()
    sys.exit(0)
