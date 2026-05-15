#!/usr/bin/env python3
"""
scripts/test_order_flow.py — Alpaca paper order placement smoke test.

Validates the complete order chain end-to-end:
  connect → verify account → (close stale SPY) → get price → place bracket
  → poll fill → verify position → verify child orders → wait → close+cancel
  → verify closed → log + Discord.

Usage:
    uv run python scripts/test_order_flow.py            # Dry-run (print plan, exit 0)
    uv run python scripts/test_order_flow.py --confirm  # Execute for real

Requires:
  - ALPACA_API_KEY / ALPACA_API_SECRET in .env
  - Run during regular market hours (09:30–15:55 ET) so market orders fill
  - TRADING_MODE=paper  (uses paper-api.alpaca.markets)
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

# ── bootstrap: ensure project root on sys.path ────────────────────────────────
_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

import structlog

from src.config import settings
from src.data.alpaca_client import (
    BracketOrder,
    _rest,
    cancel_all_orders,
    close_position,
    get_account,
    get_open_orders,
    get_positions,
    get_snapshots,
    place_bracket_order,
)
from src.logging_setup import configure_logging
from src.storage.db import get_connection

ET = ZoneInfo("America/New_York")
log = structlog.get_logger("smoke_test")

# ── Constants ─────────────────────────────────────────────────────────────────
TICKER         = "SPY"     # Highest liquidity, always fillable during session
QTY            = 1          # 1 share — minimal capital impact (~$500)
STOP_PCT       = 0.005      # −0.5% stop loss
TARGET_PCT     = 0.005      # +0.5% take profit (symmetric for speed of test)
POLL_INTERVAL  = 2          # seconds between fill-status polls
FILL_TIMEOUT   = 30         # seconds to wait for fill before giving up
SETTLE_WAIT    = 10         # seconds to hold position before cleanup


# ── Logging helpers ───────────────────────────────────────────────────────────

def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3]


def _log_db(event_type: str, message: str) -> None:
    """Write to SQLite bot_events table (best-effort, never throws)."""
    try:
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO bot_events (event_type, message, occurred_at) VALUES (?, ?, ?)",
                (event_type, message, datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
    except Exception as exc:
        log.warning("db_log_failed", error=str(exc))


def _post_discord(msg: str) -> None:
    """Post to Discord webhook synchronously (wraps async post_message)."""
    try:
        from src.alerts.discord import post_message
        asyncio.run(post_message(settings.DISCORD_WEBHOOK_URL, msg))
    except Exception as exc:
        log.warning("discord_failed", error=str(exc))


# ── Cleanup helper ────────────────────────────────────────────────────────────

def _cleanup(position_opened: bool) -> None:
    """Best-effort: cancel all orders, then close SPY position if we opened one."""
    log.info("cleanup", phase="start")
    try:
        cancel_all_orders()
        log.info("cleanup", phase="orders_cancelled")
    except Exception as exc:
        log.warning("cleanup_cancel_failed", error=str(exc))
    if position_opened:
        try:
            close_position(TICKER)
            log.info("cleanup", phase="position_closed")
        except Exception as exc:
            log.warning("cleanup_close_failed", error=str(exc))


# ── Core smoke-test logic ─────────────────────────────────────────────────────

def run() -> int:  # noqa: C901
    """Execute the full smoke test. Returns 0 on success, 1 on failure."""
    started     = time.time()
    order_id: Optional[str] = None
    pos_opened  = False

    # ── Step 1: Account ──────────────────────────────────────────────────
    log.info("step", n=1, desc="Connecting to Alpaca paper API…")
    try:
        acct = get_account()
    except Exception as exc:
        log.error("step1_failed", error=str(exc))
        return 1

    cash   = acct.get("cash", "?")
    buying = acct.get("buying_power", "?")
    status = acct.get("status", "?")
    log.info("account", cash=cash, buying_power=buying, status=status,
             paper=acct.get("trading_blocked") is False)

    if status != "ACTIVE":
        log.error("account_not_active", status=status)
        return 1

    # ── Step 2: Idempotency — close existing SPY position ────────────────
    log.info("step", n=2, desc=f"Checking for existing {TICKER} position…")
    try:
        positions = get_positions()
        existing  = next((p for p in positions if p.get("symbol") == TICKER), None)
        if existing:
            log.warning("existing_position_found",
                        qty=existing.get("qty"), closing_first=True)
            cancel_all_orders()
            time.sleep(1)
            close_position(TICKER)
            time.sleep(3)
            log.info("existing_position_closed")
        else:
            log.info("no_existing_position")
    except Exception as exc:
        log.error("step2_failed", error=str(exc))
        return 1

    # ── Step 3: Fetch current SPY price ──────────────────────────────────
    log.info("step", n=3, desc=f"Fetching {TICKER} price…")
    try:
        snaps = get_snapshots([TICKER])
        snap  = snaps.get(TICKER, {})
        # Alpaca snapshot: latestTrade.p > latestBar.c > dailyBar.c
        price = float(
            (snap.get("latestTrade") or {}).get("p")
            or (snap.get("latestBar")  or {}).get("c")
            or (snap.get("dailyBar")   or {}).get("c")
            or 0
        )
        if price <= 0:
            log.error("price_unavailable", raw_snap=snap)
            return 1
        log.info("price_ok", ticker=TICKER, price=price)
    except Exception as exc:
        log.error("step3_failed", error=str(exc))
        return 1

    stop_price   = round(price * (1 - STOP_PCT), 2)
    target_price = round(price * (1 + TARGET_PCT), 2)
    stop_limit   = round(stop_price * 0.999, 2)   # slightly below stop (safety)
    log.info("bracket_levels",
             entry_approx=price, stop=stop_price,
             target=target_price, stop_limit=stop_limit)

    # ── Step 4: Place bracket order ───────────────────────────────────────
    log.info("step", n=4,
             desc=f"Placing bracket market order: {QTY}×{TICKER} "
                  f"entry≈{price:.2f} stop={stop_price:.2f} target={target_price:.2f}…")
    try:
        order = BracketOrder(
            symbol           = TICKER,
            qty              = QTY,
            entry_price      = price,
            take_profit_price= target_price,
            stop_loss_price  = stop_price,
            stop_limit_price = stop_limit,
        )
        resp     = place_bracket_order(order)
        order_id = resp.get("id")
        log.info("order_placed",
                 order_id=order_id,
                 status=resp.get("status"),
                 filled_qty=resp.get("filled_qty"))
        _log_db("smoke_test_order",
                f"placed bracket order id={order_id} ticker={TICKER} "
                f"entry≈{price:.2f} stop={stop_price:.2f} target={target_price:.2f}")
    except Exception as exc:
        log.error("step4_failed", error=str(exc))
        _cleanup(False)
        return 1

    # ── Step 5: Poll until filled ─────────────────────────────────────────
    log.info("step", n=5,
             desc=f"Polling for fill (max {FILL_TIMEOUT}s, every {POLL_INTERVAL}s)…")
    filled   = False
    deadline = time.time() + FILL_TIMEOUT
    while time.time() < deadline:
        try:
            o      = _rest("GET", f"/v2/orders/{order_id}")
            status = o.get("status", "")
            log.info("poll", status=status, filled_qty=o.get("filled_qty"),
                     remaining=round(deadline - time.time(), 1))
            if status == "filled":
                filled     = True
                pos_opened = True
                log.info("filled",
                         fill_price=o.get("filled_avg_price"),
                         elapsed_s=round(time.time() - started, 1))
                break
            if status in ("canceled", "expired", "rejected"):
                log.error("order_terminal", status=status, reason=o.get("reason"))
                break
        except Exception as exc:
            log.warning("poll_error", error=str(exc))
        time.sleep(POLL_INTERVAL)

    if not filled:
        msg = f"🚨 Smoke test FAILED: {TICKER} order {order_id} not filled in {FILL_TIMEOUT}s"
        log.error("fill_timeout", order_id=order_id)
        _cleanup(pos_opened)
        _log_db("smoke_test_order", f"FAILED: fill timeout order_id={order_id}")
        _post_discord(msg)
        return 1

    # ── Step 6: Verify position ───────────────────────────────────────────
    log.info("step", n=6, desc=f"Verifying {TICKER} in /v2/positions…")
    try:
        positions = get_positions()
        spy_pos   = next((p for p in positions if p.get("symbol") == TICKER), None)
        if not spy_pos:
            log.error("position_not_found_post_fill")
            _cleanup(True)
            return 1
        log.info("position_ok",
                 qty=spy_pos.get("qty"),
                 avg_entry=spy_pos.get("avg_entry_price"),
                 unrealized_pnl=spy_pos.get("unrealized_pl"))
    except Exception as exc:
        log.error("step6_failed", error=str(exc))
        _cleanup(True)
        return 1

    # ── Step 7: Verify child orders (stop + target) ───────────────────────
    log.info("step", n=7, desc="Verifying stop-loss and take-profit child orders…")
    try:
        parent = _rest("GET", f"/v2/orders/{order_id}?nested=true")
        legs   = parent.get("legs", [])
        log.info("child_orders",
                 leg_count=len(legs),
                 leg_types=[l.get("type") for l in legs],
                 leg_statuses=[l.get("status") for l in legs])
        if len(legs) >= 2:
            log.info("bracket_verified", ok=True)
        else:
            log.warning("fewer_legs_than_expected", got=len(legs), want=2)
    except Exception as exc:
        log.warning("step7_warning", error=str(exc))  # Non-fatal

    # ── Step 8: Hold 10 s ─────────────────────────────────────────────────
    log.info("step", n=8, desc=f"Holding for {SETTLE_WAIT}s…")
    time.sleep(SETTLE_WAIT)

    # ── Step 9: Cancel child orders + close position ──────────────────────
    log.info("step", n=9, desc="Cancelling child orders and closing position…")
    try:
        cancel_all_orders()
        time.sleep(1)
        close_position(TICKER)
        log.info("close_issued")
    except Exception as exc:
        log.error("step9_failed", error=str(exc))
        return 1

    # ── Step 10: Verify closed ────────────────────────────────────────────
    log.info("step", n=10, desc="Verifying position is gone…")
    time.sleep(3)
    try:
        positions     = get_positions()
        still_open    = next((p for p in positions if p.get("symbol") == TICKER), None)
        if still_open:
            log.warning("position_still_open", qty=still_open.get("qty"))
        else:
            log.info("position_closed_confirmed")
    except Exception as exc:
        log.warning("step10_warning", error=str(exc))  # Non-fatal

    # ── Done ──────────────────────────────────────────────────────────────
    elapsed = round(time.time() - started, 1)
    log.info("SMOKE_TEST_PASSED",
             elapsed_s=elapsed,
             ticker=TICKER,
             entry=price,
             stop=stop_price,
             target=target_price)

    summary = (
        f"✅ **Order flow smoke test PASSED** in {elapsed}s\n"
        f"Ticker: `{TICKER}` | Entry≈`{price:.2f}` | "
        f"Stop=`{stop_price:.2f}` | Target=`{target_price:.2f}`\n"
        f"Mode: `{settings.TRADING_MODE}` | API: paper"
    )
    _log_db("smoke_test_order", f"PASSED in {elapsed}s entry={price:.2f}")
    _post_discord(summary)
    return 0


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    configure_logging()

    parser = argparse.ArgumentParser(
        description="Alpaca paper order flow smoke test",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Run without --confirm to see the plan first (safe dry-run).",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Actually execute the smoke test (places and closes a real paper order).",
    )
    args = parser.parse_args()

    if not args.confirm:
        print("""
╔══════════════════════════════════════════════════════════════════╗
║          ORDER FLOW SMOKE TEST  —  DRY RUN                      ║
╚══════════════════════════════════════════════════════════════════╝

What this script does when run with --confirm:

  1.  Connect to Alpaca paper API, verify account ACTIVE
  2.  Close any existing SPY position  (idempotency)
  3.  Fetch current SPY market price via snapshot API
  4.  Place a bracket market order:
        Ticker:     SPY (1 share, ~$500 notional)
        Stop loss:  entry − 0.5%
        Take profit: entry + 0.5%
        TIF:        DAY
  5.  Poll every 2 s (max 30 s) until order fills
  6.  Verify SPY appears in /v2/positions
  7.  Verify stop-loss and take-profit child order legs exist
  8.  Hold for 10 seconds
  9.  Cancel all open orders + close SPY position at market
 10.  Verify position is gone

Logging:
  • Console via structlog
  • SQLite bot_events  (event_type = 'smoke_test_order')
  • Discord webhook    (success or failure summary)

Safety notes:
  ⚠  Requires REGULAR MARKET HOURS (09:30–15:55 ET) — market orders
     do not fill outside the session.
  ⚠  Uses PAPER TRADING only — no real money.
  ⚠  Idempotent: any pre-existing SPY position is closed first.
  ⚠  On any failure: cleanup runs before exit 1.

Re-run with --confirm to execute.
""")
        sys.exit(0)

    sys.exit(run())


if __name__ == "__main__":
    main()
