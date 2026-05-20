#!/usr/bin/env python3
"""
One-shot backfill: close any open DB trades that Alpaca already filled/closed.

Run this to fix trades stuck with closed_at=NULL (e.g. from yesterday).
The script queries every open trade, fetches the Alpaca order, and calls
close_trade() if the position is gone.

After running, the dashboard will show the closed trades and P&L.
Claude learning extraction will also be able to analyse them.

Usage:
    uv run python scripts/backfill_trades.py
    uv run python scripts/backfill_trades.py --dry-run   # show what would change
    uv run python scripts/backfill_trades.py --trigger-learning  # also run Claude analysis
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill stuck open trades from Alpaca history")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be closed without writing to DB")
    parser.add_argument("--trigger-learning", action="store_true",
                        help="Run Claude learning extraction after backfill")
    args = parser.parse_args()

    from src.storage.db import init_schema, get_connection
    from src.execution.paper import get_open_positions_from_db
    from src.execution.reconcile import _get_order, _get_open_position_tickers, _determine_exit

    init_schema()

    open_trades = get_open_positions_from_db()
    if not open_trades:
        print("✅ No open trades in DB — nothing to backfill.")
        return

    print(f"Found {len(open_trades)} open trade(s) in DB:\n")
    for t in open_trades:
        print(f"  {t['ticker']:6s}  opened {t['opened_at'][:16]}  "
              f"entry={t['entry_price']:.2f}  id={t['id'][:16]}…")

    print()

    live_tickers = _get_open_position_tickers()
    print(f"Live Alpaca positions: {live_tickers or '(none)'}\n")

    closed = []

    for trade in open_trades:
        trade_id = trade["id"]
        ticker   = trade["ticker"]
        entry    = trade.get("entry_price", 0.0)

        order = _get_order(trade_id)
        position_gone = ticker not in live_tickers

        exit_price  = None
        exit_reason = None

        if order is None:
            if position_gone:
                exit_price  = entry  # best we can do without order data
                exit_reason = "force_close"
                print(f"  {ticker}: order not found in Alpaca + position gone → force_close (entry price fallback)")
            else:
                print(f"  {ticker}: order not found in Alpaca, position still live — skipping")
            # Try Alpaca order history as fallback
            if order is None:
                exit_price, exit_reason = _try_order_history(trade_id, ticker, entry, position_gone)

        else:
            parent_status = order.get("status", "")
            print(f"  {ticker}: Alpaca order status = {parent_status}")

            if parent_status == "filled":
                exit_price, exit_reason = _determine_exit(order)
                if exit_price is None and position_gone:
                    exit_price  = entry
                    exit_reason = "force_close"
            elif parent_status in ("canceled", "expired", "done_for_day"):
                exit_price  = entry
                exit_reason = "force_close"
            else:
                print(f"         → still pending ({parent_status}), skipping")
                continue

        if exit_price and exit_reason:
            print(f"         → will close at {exit_price:.4f} ({exit_reason})")
            closed.append((trade_id, ticker, exit_price, exit_reason))
        else:
            print(f"         → could not determine exit, skipping")

    print()

    if not closed:
        print("Nothing to backfill.")
        return

    if args.dry_run:
        print(f"DRY RUN — would close {len(closed)} trade(s). Re-run without --dry-run to apply.")
        return

    # Apply closures
    from src.execution.paper import close_trade
    for trade_id, ticker, exit_price, exit_reason in closed:
        close_trade(trade_id, exit_price, exit_reason)
        print(f"  ✅ Closed {ticker} at {exit_price:.4f} ({exit_reason})")

    print(f"\nBackfill complete — {len(closed)} trade(s) closed.")

    # Show what's now in DB
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT ticker, closed_at, exit_price, pnl_dollars, pnl_r, exit_reason
            FROM trades
            WHERE id IN ({})
        """.format(",".join("?" * len(closed))), [t[0] for t in closed]).fetchall()

    print("\nUpdated trade records:")
    for row in rows:
        pnl = row["pnl_dollars"] or 0
        r   = row["pnl_r"] or 0
        print(f"  {row['ticker']:6s}  closed {str(row['closed_at'])[:16]}  "
              f"exit={row['exit_price']:.4f}  pnl=${pnl:+.2f}  {r:+.2f}R  ({row['exit_reason']})")

    # Optional: trigger Claude learning analysis
    if args.trigger_learning:
        print("\n🤖 Triggering Claude learning analysis…")
        try:
            import asyncio
            from src.learnings.triggers import _todays_closed_trades
            from src.learnings.extractor import extract_period_learning, extract_trade_learning
            from src.alerts.discord import post_message
            from src.config import settings

            # Individual trade learnings
            for trade_id, ticker, _, _ in closed:
                with get_connection() as conn:
                    row = conn.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
                if row:
                    msg = extract_trade_learning(dict(row))
                    if msg:
                        print(f"  Learning for {ticker}: {msg[:80]}…")
                        if settings.DISCORD_WEBHOOK_URL:
                            asyncio.run(post_message(settings.DISCORD_WEBHOOK_URL,
                                                     f"💡 **Trade learning** ({ticker})\n{msg}"))

            # Period-level learning across all closed trades
            all_closed_today = _todays_closed_trades()
            if len(all_closed_today) >= 2:
                msg = extract_period_learning(all_closed_today, "manual_backfill", "day")
                if msg:
                    print(f"\n  Period learning: {msg[:120]}…")
                    if settings.DISCORD_WEBHOOK_URL:
                        asyncio.run(post_message(settings.DISCORD_WEBHOOK_URL,
                                                 f"💡 **Period learning**\n{msg}"))

        except Exception as exc:
            print(f"  ⚠️  Learning extraction failed: {exc}")
            import traceback; traceback.print_exc()


def _try_order_history(trade_id: str, ticker: str, entry: float, position_gone: bool):
    """
    Try to find the order in Alpaca's recent closed-order history.
    Returns (exit_price, exit_reason) or (None, None).
    """
    import httpx
    from src.config import settings
    from src.execution.reconcile import _HEADERS, _REST_BASE

    try:
        url = f"{_REST_BASE}/v2/orders"
        with httpx.Client(headers=_HEADERS, timeout=10.0) as c:
            r = c.get(url, params={
                "status": "closed",
                "limit":  100,
                "nested": "true",
            })
            r.raise_for_status()
            orders = r.json()

        for order in orders:
            if order.get("id") == trade_id:
                from src.execution.reconcile import _determine_exit
                ep, er = _determine_exit(order)
                if ep:
                    return ep, er
                if position_gone:
                    return entry, "force_close"
    except Exception as exc:
        print(f"         order history lookup failed: {exc}")

    if position_gone:
        return entry, "force_close"
    return None, None


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"FATAL: {exc}")
        import traceback; traceback.print_exc()
    sys.exit(0)
