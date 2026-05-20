from __future__ import annotations

"""
Alpaca order fill reconciliation loop.

Problem: The bot places bracket orders via Alpaca. When Alpaca's child orders
(take_profit / stop_loss) fill automatically, the bot never hears about it —
there is no WebSocket feed for order fills, only for price bars.
Result: trades stay stuck with closed_at=NULL forever.

Solution: every 5 seconds during ACTIVE_TRADING and POSITION_MGMT, poll
Alpaca's order API for each open DB trade and close it if the position is gone.

Exit reason detection:
  - parent order filled + take_profit child filled  → "target_hit"
  - parent order filled + stop_loss child filled    → "stop_hit"
  - parent order canceled/expired                   → "force_close"
  - position no longer exists in Alpaca             → "force_close" (EOD sweep)
"""

import logging
from datetime import datetime, timezone
from typing import Optional

import httpx

from src.config import settings
from src.storage.db import get_connection

logger = logging.getLogger(__name__)

_REST_BASE = settings.ALPACA_BASE_URL
_HEADERS = {
    "APCA-API-KEY-ID":     settings.ALPACA_API_KEY,
    "APCA-API-SECRET-KEY": settings.ALPACA_API_SECRET,
    "Content-Type":        "application/json",
}


def _get_order(order_id: str) -> Optional[dict]:
    """Fetch a single order from Alpaca with nested child orders."""
    try:
        url = f"{_REST_BASE}/v2/orders/{order_id}"
        with httpx.Client(headers=_HEADERS, timeout=10.0) as c:
            r = c.get(url, params={"nested": "true"})
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json()
    except Exception as exc:
        logger.warning("reconcile: failed to fetch order %s: %s", order_id, exc)
        return None


def _get_open_position_tickers() -> set[str]:
    """Return set of tickers currently held in Alpaca (live position)."""
    try:
        url = f"{_REST_BASE}/v2/positions"
        with httpx.Client(headers=_HEADERS, timeout=10.0) as c:
            r = c.get(url)
            r.raise_for_status()
            return {p["symbol"] for p in r.json()}
    except Exception as exc:
        logger.warning("reconcile: failed to fetch positions: %s", exc)
        return set()


def _determine_exit(order: dict) -> tuple[Optional[float], str]:
    """
    Inspect a filled bracket order and return (exit_price, exit_reason).

    Bracket orders have:
      order["legs"] — list with two child orders:
        - side=sell, type=limit  → take_profit
        - side=sell, type=stop_limit  → stop_loss

    Returns (None, "unknown") if we cannot determine the outcome.
    """
    parent_status = order.get("status", "")
    legs = order.get("legs") or []

    for leg in legs:
        leg_status = leg.get("status", "")
        if leg_status != "filled":
            continue

        leg_type   = leg.get("type", "")
        filled_avg = leg.get("filled_avg_price")
        if filled_avg is None:
            continue

        exit_price = float(filled_avg)
        if leg_type == "limit":
            return exit_price, "target_hit"
        elif leg_type in ("stop", "stop_limit"):
            return exit_price, "stop_hit"

    # No child filled yet — check if parent itself provides a fill price
    # (market buy should be filled, but in case order is in unusual state)
    if parent_status in ("canceled", "expired"):
        return None, "force_close"

    # Try filled_avg_price on the parent as last resort
    filled_avg = order.get("filled_avg_price")
    if filled_avg and parent_status == "filled":
        return float(filled_avg), "force_close"

    return None, "unknown"


def reconcile_open_trades(state=None) -> int:
    """
    Check every open DB trade against Alpaca.
    Calls close_trade() for each one that is no longer live.

    Returns the number of trades closed in this pass.
    """
    from src.execution.paper import close_trade, get_open_positions_from_db

    open_trades = get_open_positions_from_db()
    if not open_trades:
        return 0

    # Get live Alpaca positions for quick membership check
    live_tickers = _get_open_position_tickers()

    closed_count = 0

    for trade in open_trades:
        trade_id = trade["id"]
        ticker   = trade["ticker"]

        # --- Fast path: position is gone from Alpaca entirely ---
        # This covers: stop hit, target hit, force-close, EOD sweep
        position_gone = ticker not in live_tickers

        order = _get_order(trade_id)

        if order is None and position_gone:
            # Order doesn't exist (already archived) and position is gone
            # Use entry price as placeholder exit — will be updated by Alpaca history if needed
            entry = trade.get("entry_price", 0.0)
            _close_with_reason(trade_id, ticker, entry, "force_close", state)
            closed_count += 1
            continue

        if order is None:
            # Order not found but position might still be live — skip
            continue

        parent_status = order.get("status", "")

        # Parent order not yet filled (entry pending) — position not open yet
        if parent_status in ("new", "accepted", "pending_new", "held"):
            continue

        # Parent filled — check child orders
        if parent_status == "filled":
            exit_price, exit_reason = _determine_exit(order)

            if exit_price is not None:
                _close_with_reason(trade_id, ticker, exit_price, exit_reason, state)
                closed_count += 1
            elif position_gone:
                # Filled but no child fill info — position is gone; use entry as fallback
                entry = trade.get("entry_price", 0.0)
                _close_with_reason(trade_id, ticker, entry, "force_close", state)
                closed_count += 1

        elif parent_status in ("canceled", "expired", "done_for_day"):
            entry = trade.get("entry_price", 0.0)
            _close_with_reason(trade_id, ticker, entry, "force_close", state)
            closed_count += 1

    return closed_count


def _close_with_reason(
    trade_id: str,
    ticker: str,
    exit_price: float,
    exit_reason: str,
    state=None,
) -> None:
    """Call close_trade() and update RiskState."""
    from src.execution.paper import close_trade

    if exit_price <= 0:
        logger.warning("reconcile: skipping %s — invalid exit_price %.4f", trade_id, exit_price)
        return

    close_trade(trade_id, exit_price, exit_reason)
    logger.info("reconcile: closed trade %s %s exit=%.4f reason=%s",
                trade_id, ticker, exit_price, exit_reason)

    # Update in-memory risk state if available
    if state is not None and getattr(state, "risk_state", None) is not None:
        try:
            with get_connection() as conn:
                row = conn.execute(
                    "SELECT pnl_dollars, closed_at FROM trades WHERE id=?", (trade_id,)
                ).fetchone()
            if row and row["pnl_dollars"] is not None:
                from datetime import datetime, timezone
                state.risk_state.record_trade(
                    float(row["pnl_dollars"]),
                    datetime.now(timezone.utc),
                )
        except Exception as exc:
            logger.warning("reconcile: could not update risk_state for %s: %s", trade_id, exc)
