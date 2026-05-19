#!/usr/bin/env python3
"""
Daily pre-market universe refresh.

Reads data/base_universe.csv (or small_cap_base_universe.csv as fallback),
calls Alpaca snapshots in batches, filters for gapping tickers, and writes
state/universe_today.csv.

Run schedule (automated via pre_market_prep.py):
    04:00 ET  — initial refresh
    07:00 ET  — re-run after overnight news ingest
    09:20 ET  — final pre-open refresh with latest prices

Manual run:
    uv run python scripts/refresh_universe.py

Always exits 0. Prints delta vs previous state/universe_today.csv.
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def _load_config() -> dict:
    try:
        import yaml
        with open(PROJECT_ROOT / "config.yaml") as f:
            return (yaml.safe_load(f) or {}).get("scanner", {})
    except Exception:
        return {}


def _load_base_universe() -> list[dict]:
    # Prefer data/base_universe.csv (built by build_base_universe.py),
    # fall back to small_cap_base_universe.csv (hand-curated).
    candidates = [
        PROJECT_ROOT / "data" / "base_universe.csv",
        PROJECT_ROOT / "data" / "small_cap_base_universe.csv",
    ]
    for path in candidates:
        if path.exists():
            rows = []
            with open(path, newline="") as f:
                for row in csv.DictReader(f):
                    ticker = row.get("ticker", "").strip().upper()
                    if ticker:
                        rows.append({
                            "ticker":       ticker,
                            "float_shares": row.get("float_shares", "15000000").strip(),
                            "sector":       row.get("sector", row.get("exchange", "Unknown")).strip(),
                        })
            print(f"[refresh_universe] Base: {path.name} ({len(rows)} tickers)", flush=True)
            return rows

    print("[refresh_universe] ERROR: no base universe file found", flush=True)
    return []


def _load_previous_universe(out_path: Path) -> set[str]:
    """Return set of tickers from the previous universe_today.csv (for delta)."""
    if not out_path.exists():
        return set()
    try:
        with open(out_path, newline="") as f:
            return {row["ticker"].strip().upper() for row in csv.DictReader(f) if row.get("ticker")}
    except Exception:
        return set()


def _fetch_snapshots(tickers: list[str]) -> dict:
    from src.data.alpaca_client import get_snapshots

    result = {}
    batch_size = 100
    total_batches = (len(tickers) + batch_size - 1) // batch_size

    for i in range(0, len(tickers), batch_size):
        batch = tickers[i: i + batch_size]
        batch_num = i // batch_size + 1
        print(f"[refresh_universe] Batch {batch_num}/{total_batches} ({len(batch)} tickers)…",
              flush=True)
        try:
            result.update(get_snapshots(batch))
        except Exception as exc:
            print(f"[refresh_universe] WARNING: batch {batch_num} failed: {exc}", flush=True)

    return result


def _compute_gap(snap: dict) -> tuple[float, float]:
    prev_close = (snap.get("prevDailyBar") or {}).get("c", 0)
    daily      = snap.get("dailyBar") or {}
    price      = daily.get("c") or daily.get("o") or 0
    if prev_close <= 0 or price <= 0:
        return 0.0, 0.0
    return float(price), (float(price) - float(prev_close)) / float(prev_close) * 100.0


def main() -> None:
    cfg           = _load_config()
    min_price     = float(cfg.get("price_min", 1.00))
    max_price     = float(cfg.get("price_max", 20.00))
    min_gap_pct   = float(cfg.get("universe_gap_filter_pct", 3.0))
    top_n_fallback = 30

    # Output path
    state_dir = PROJECT_ROOT / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    out_path  = state_dir / "universe_today.csv"

    # Load previous universe for delta reporting
    prev_tickers = _load_previous_universe(out_path)

    base_rows = _load_base_universe()
    if not base_rows:
        sys.exit(0)

    tickers    = [r["ticker"] for r in base_rows]
    float_map  = {r["ticker"]: r["float_shares"]  for r in base_rows}
    sector_map = {r["ticker"]: r["sector"]        for r in base_rows}

    print(f"[refresh_universe] Scanning {len(tickers)} tickers…", flush=True)
    snaps = _fetch_snapshots(tickers)
    print(f"[refresh_universe] Got snapshots for {len(snaps)} tickers", flush=True)

    # Score every ticker that has price data
    scored: list[tuple[float, float, str]] = []   # (gap_pct, price, ticker)
    for ticker, snap in snaps.items():
        price, gap_pct = _compute_gap(snap)
        if price > 0:
            scored.append((gap_pct, price, ticker))
    scored.sort(reverse=True)

    # Primary filter
    passed = [
        (g, p, t) for g, p, t in scored
        if min_price <= p <= max_price and g >= min_gap_pct
    ]

    # Build output: all passed + top-N fallback to ensure scanner always has candidates
    output: dict[str, tuple[float, float]] = {t: (g, p) for g, p, t in passed}
    for g, p, t in scored:
        if len(output) >= max(top_n_fallback, len(passed)):
            break
        if t not in output:
            output[t] = (g, p)

    # Write
    ordered = sorted(output.items(), key=lambda kv: kv[1][0], reverse=True)
    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["ticker", "float_shares", "sector", "gap_pct"])
        writer.writeheader()
        for ticker, (gap_pct, _price) in ordered:
            writer.writerow({
                "ticker":       ticker,
                "float_shares": float_map.get(ticker, "15000000"),
                "sector":       sector_map.get(ticker, "Unknown"),
                "gap_pct":      f"{gap_pct:.2f}",
            })

    new_tickers = {t for t in output} - prev_tickers
    removed     = prev_tickers - {t for t in output}

    print(
        f"[refresh_universe] Done: scanned={len(snaps)} "
        f"gap≥{min_gap_pct}%={len(passed)} written={len(output)} → {out_path}",
        flush=True,
    )
    if new_tickers:
        print(f"[refresh_universe] NEW:     {', '.join(sorted(new_tickers))}", flush=True)
    if removed:
        print(f"[refresh_universe] REMOVED: {', '.join(sorted(removed))}", flush=True)
    if not prev_tickers:
        print("[refresh_universe] (no previous universe — delta N/A)", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[refresh_universe] FATAL: {exc}", flush=True)
        import traceback
        traceback.print_exc()
    sys.exit(0)
