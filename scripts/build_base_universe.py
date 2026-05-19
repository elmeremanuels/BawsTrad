#!/usr/bin/env python3
"""
Build the static base universe from Alpaca's full asset list.

Fetches all active US equities from Alpaca, filters to common stocks on
NYSE/NASDAQ/AMEX/ARCA, excludes ETFs/funds/trusts by name heuristics,
and writes data/base_universe.csv (ticker,exchange,name).

This file is the input for scripts/refresh_universe.py, which queries
Alpaca snapshots on these tickers every morning to find today's gappers.

Run schedule: weekly (Sunday evening) or after major index rebalances.

Usage:
    uv run python scripts/build_base_universe.py

Always exits 0. Typical result: 3000–5000 tickers after filtering.
"""

from __future__ import annotations

import csv
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Exchanges we want — NYSE, NASDAQ, AMEX (American Stock Exchange), ARCA
_VALID_EXCHANGES = {"NYSE", "NASDAQ", "AMEX", "ARCA"}

# Name substrings that indicate ETF/fund/trust — check as lowercase word boundaries
_EXCLUDE_NAME_PATTERNS = re.compile(
    r"\betf\b|\bfund\b|\btrust\b|\bindex\b|\breit\b|\bmlp\b"
    r"|\bholding\b|\bholdings\b|\bincome\b|\bportfolio\b|\bportfolios\b",
    re.IGNORECASE,
)

# Symbol patterns that indicate warrants, rights, units, preferred shares
_EXCLUDE_SYMBOL_RE = re.compile(r"[.\-/]|W$|R$|U$|P$")


def _is_common_stock(asset: dict) -> bool:
    symbol = (asset.get("symbol") or "").strip()
    name   = (asset.get("name")   or "").strip()
    exch   = (asset.get("exchange") or "").strip().upper()

    # Must be on a recognised exchange
    if exch not in _VALID_EXCHANGES:
        return False

    # Must be tradeable
    if not asset.get("tradable"):
        return False

    # Symbol must be 1–5 uppercase letters only (no dots, dashes, suffixes)
    if not re.match(r"^[A-Z]{1,5}$", symbol):
        return False

    # Exclude by name patterns
    if _EXCLUDE_NAME_PATTERNS.search(name):
        return False

    return True


def main() -> None:
    from src.data.alpaca_client import get_all_tradeable_assets

    print("[build_base_universe] Fetching all active US equity assets from Alpaca…", flush=True)
    all_assets = get_all_tradeable_assets()
    if not all_assets:
        print("[build_base_universe] ERROR: no assets returned — check Alpaca credentials", flush=True)
        sys.exit(0)

    print(f"[build_base_universe] Total fetched: {len(all_assets)}", flush=True)

    # Filter
    common = [a for a in all_assets if _is_common_stock(a)]
    print(f"[build_base_universe] After filter:  {len(common)} common stocks", flush=True)

    # Write
    data_dir = PROJECT_ROOT / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    out_path = data_dir / "base_universe.csv"

    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["ticker", "exchange", "name"])
        writer.writeheader()
        for a in sorted(common, key=lambda x: x.get("symbol", "")):
            writer.writerow({
                "ticker":   a["symbol"],
                "exchange": a.get("exchange", ""),
                "name":     a.get("name", ""),
            })

    print(f"[build_base_universe] Written {len(common)} tickers → {out_path}", flush=True)
    print("[build_base_universe] Run weekly or after index rebalances.", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[build_base_universe] FATAL: {exc}", flush=True)
        import traceback
        traceback.print_exc()
    sys.exit(0)
