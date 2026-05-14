#!/usr/bin/env python
"""
Recent backtest — runs the full backtest pipeline over the last N trading days.

Usage:
  uv run python scripts/backtest_recent.py [--days 5]

Delegates to scripts/backtest.py with computed date range.
Called automatically by the WEEKEND_DEEP_WORK handler on Saturdays.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import date, timedelta


def _last_n_trading_days(n: int) -> tuple[date, date]:
    """Return (start, end) covering the last `n` calendar days (simple approximation)."""
    end = date.today() - timedelta(days=1)  # Yesterday
    start = end - timedelta(days=n + 3)     # Buffer for weekends/holidays
    return start, end


def main() -> None:
    parser = argparse.ArgumentParser(description="Run backtest over recent N days")
    parser.add_argument("--days", type=int, default=5, help="Number of past days to backtest")
    args = parser.parse_args()

    start, end = _last_n_trading_days(args.days)
    print(f"Running recent backtest: {start} → {end} ({args.days}d window)")

    result = subprocess.run(
        [sys.executable, "scripts/backtest.py",
         "--start", start.isoformat(),
         "--end", end.isoformat()],
        capture_output=False,
    )
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
