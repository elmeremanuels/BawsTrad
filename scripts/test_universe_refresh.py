#!/usr/bin/env python3
"""
Universe refresh pipeline smoke test.

Run this every morning before market open (or via systemd timer) to verify
that the bot's daily universe is populated with real, active gappers.

Steps:
  1. Show base universe stats (data/base_universe.csv or small_cap_base_universe.csv)
  2. Run scripts/refresh_universe.py (rebuilds state/universe_today.csv)
  3. Show top-20 tickers by gap%, sector distribution, filter pass rate
  4. Post summary to Discord
  5. Exit 0 always

Usage:
    uv run python scripts/test_universe_refresh.py
    uv run python scripts/test_universe_refresh.py --no-discord
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import subprocess
import sys
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# ANSI colours
GREEN  = "\033[32m"
YELLOW = "\033[33m"
CYAN   = "\033[36m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

def _h(s: str) -> str:  return f"{BOLD}{CYAN}{s}{RESET}"
def _g(s: str) -> str:  return f"{GREEN}{s}{RESET}"
def _y(s: str) -> str:  return f"{YELLOW}{s}{RESET}"


def _load_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def _run_refresh() -> bool:
    """Run refresh_universe.py and return True on success."""
    script = PROJECT_ROOT / "scripts" / "refresh_universe.py"
    print(f"\n{_h('Running refresh_universe.py…')}")
    result = subprocess.run(
        [sys.executable, str(script)],
        capture_output=False,  # let output stream to terminal
    )
    return result.returncode == 0


def _format_summary(today_rows: list[dict], base_count: int) -> str:
    """Build a Discord-friendly text summary."""
    if not today_rows:
        return "⚠️ Universe refresh produced 0 tickers — check logs."

    gapping = [r for r in today_rows if float(r.get("gap_pct", 0) or 0) >= 3.0]
    top5    = sorted(today_rows, key=lambda r: float(r.get("gap_pct", 0) or 0), reverse=True)[:5]
    sectors = Counter(r.get("sector", "?") for r in today_rows)

    lines = [
        f"📊 **Universe Refresh** | Base: {base_count} → Today: {len(today_rows)} tickers",
        f"   Gap ≥3%: {len(gapping)} | Top gappers:",
    ]
    for r in top5:
        gap = float(r.get("gap_pct", 0) or 0)
        lines.append(f"   • **{r['ticker']}** {gap:+.1f}% ({r.get('sector','?')})")
    top_sectors = sectors.most_common(3)
    lines.append("   Sectors: " + ", ".join(f"{s}={n}" for s, n in top_sectors))
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Universe refresh smoke test")
    parser.add_argument("--no-discord", action="store_true",
                        help="Skip Discord post (useful in CI)")
    args = parser.parse_args()

    # ── 1. Base universe ─────────────────────────────────────────────────────
    print(f"\n{_h('1. Base universe')}")
    base_candidates = [
        PROJECT_ROOT / "data" / "base_universe.csv",
        PROJECT_ROOT / "data" / "small_cap_base_universe.csv",
    ]
    base_rows = []
    base_file = None
    for p in base_candidates:
        base_rows = _load_csv(p)
        if base_rows:
            base_file = p
            break

    if base_rows:
        print(f"   {_g(str(base_file.name))}: {len(base_rows)} tickers")
    else:
        print(f"   {_y('No base universe found — run build_base_universe.py first')}")

    # ── 2. Run refresh ───────────────────────────────────────────────────────
    print(f"\n{_h('2. Running refresh')}")
    _run_refresh()

    # ── 3. Analyse today's universe ──────────────────────────────────────────
    print(f"\n{_h('3. Today universe: state/universe_today.csv')}")
    today_path = PROJECT_ROOT / "state" / "universe_today.csv"
    today_rows = _load_csv(today_path)

    if not today_rows:
        print(f"   {_y('state/universe_today.csv is empty or missing!')}")
    else:
        print(f"   Total tickers: {len(today_rows)}")

        # Sort by gap%
        sorted_rows = sorted(today_rows,
                             key=lambda r: float(r.get("gap_pct", 0) or 0),
                             reverse=True)

        print(f"\n   {'TICKER':<8} {'GAP%':>7}  SECTOR")
        print("   " + "─" * 40)
        for r in sorted_rows[:20]:
            gap = float(r.get("gap_pct", 0) or 0)
            gap_str = f"{gap:+.1f}%"
            marker = _g("▲") if gap >= 10 else (_y("▲") if gap >= 3 else " ")
            print(f"   {r['ticker']:<8} {gap_str:>7}  {marker} {r.get('sector','?')}")

        # Sector distribution
        sectors = Counter(r.get("sector", "?") for r in today_rows)
        print(f"\n   Sector distribution:")
        for sector, count in sectors.most_common(8):
            print(f"     {sector:<20} {count:>3}")

        # Trading criteria preview
        gapping_3  = sum(1 for r in today_rows if float(r.get("gap_pct", 0) or 0) >= 3.0)
        gapping_10 = sum(1 for r in today_rows if float(r.get("gap_pct", 0) or 0) >= 10.0)
        priced_ok  = sum(1 for r in today_rows
                         if 1.0 <= float(r.get("gap_pct", 0) or 0))  # rough proxy
        print(f"\n   Gap ≥ 3%  (daily filter): {gapping_3}")
        print(f"   Gap ≥ 10% (trade filter): {gapping_10}")

    # ── 4. Discord summary ───────────────────────────────────────────────────
    if not args.no_discord:
        print(f"\n{_h('4. Discord summary')}")
        try:
            from src.config import settings
            from src.alerts.discord import post_message
            msg = _format_summary(today_rows, len(base_rows))
            asyncio.run(post_message(settings.DISCORD_WEBHOOK_URL, msg))
            print("   Posted to Discord ✓")
        except Exception as exc:
            print(f"   Discord post failed: {exc}")

    print()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"FATAL: {exc}")
        import traceback
        traceback.print_exc()
    sys.exit(0)
