from __future__ import annotations

"""
Paper trading execution layer.
Wraps Alpaca paper API calls and records every trade to SQLite.
"""

import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from src.data.alpaca_client import (
    BracketOrder, cancel_all_orders, close_all_positions,
    close_position, get_positions, place_bracket_order,
)
from src.execution.bracket import build_bracket
from src.storage.db import get_connection

logger = logging.getLogger(__name__)


def open_long(
    symbol: str,
    qty: int,
    entry_price: float,
    stop_price: float,
    target_price: float,
    setup_type: str,
    news_item_id: Optional[str] = None,
) -> Optional[dict]:
    """
    Place a bracket long order and record it to the DB.
    Returns the Alpaca order dict, or None on failure.
    """
    if qty <= 0:
        logger.warning("open_long: qty=0 for %s — skipping", symbol)
        return None

    order = build_bracket(symbol, qty, entry_price, stop_price)
    # Override target with the one from decision engine (already 2R)
    order.take_profit_price = round(target_price, 2)

    try:
        alpaca_order = place_bracket_order(order)
    except Exception as exc:
        logger.error("open_long %s failed: %s", symbol, exc)
        return None

    alpaca_id = alpaca_order.get("id", str(uuid4()))
    now = datetime.now(timezone.utc).isoformat()

    trade = {
        "id":           alpaca_id,
        "opened_at":    now,
        "closed_at":    None,
        "ticker":       symbol,
        "side":         "long",
        "shares":       qty,
        "entry_price":  round(entry_price, 4),
        "exit_price":   None,
        "stop_price":   round(stop_price, 4),
        "target_price": round(target_price, 4),
        "pnl_dollars":  None,
        "pnl_r":        None,
        "setup_type":   setup_type,
        "exit_reason":  None,
        "news_item_id": news_item_id,
        "mode":         "paper",
    }

    with get_connection() as conn:
        conn.execute("""
            INSERT OR IGNORE INTO trades
            (id, opened_at, ticker, side, shares, entry_price, stop_price,
             target_price, setup_type, news_item_id, mode)
            VALUES (:id, :opened_at, :ticker, :side, :shares, :entry_price,
                    :stop_price, :target_price, :setup_type, :news_item_id, :mode)
        """, trade)

    # Cost tracker: snapshot bid/ask spread at order placement
    try:
        from src.execution.cost_tracker import CostTracker
        CostTracker().snapshot_pre_trade(alpaca_id, symbol)
        CostTracker().record_entry_fill(alpaca_id, entry_price)
    except Exception as _ct_exc:
        logger.debug("open_long: cost_tracker snapshot failed: %s", _ct_exc)

    logger.info("ORDER PLACED: %s %d shares entry=%.2f stop=%.2f target=%.2f",
                symbol, qty, entry_price, stop_price, target_price)
    return trade


def close_trade(
    trade_id: str,
    exit_price: float,
    exit_reason: str,
) -> None:
    """Record trade closure in SQLite (fill may come from Alpaca order updates)."""
    now = datetime.now(timezone.utc).isoformat()

    with get_connection() as conn:
        row = conn.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
        if not row:
            logger.warning("close_trade: trade %s not found in DB", trade_id)
            return

        entry = row["entry_price"]
        stop  = row["stop_price"]
        shares = row["shares"]
        risk_per_share = abs(entry - stop)
        pnl = (exit_price - entry) * shares
        pnl_r = (exit_price - entry) / risk_per_share if risk_per_share > 0 else 0.0

        conn.execute("""
            UPDATE trades
            SET closed_at=?, exit_price=?, pnl_dollars=?, pnl_r=?, exit_reason=?
            WHERE id=?
        """, (now, round(exit_price, 4), round(pnl, 2), round(pnl_r, 4), exit_reason, trade_id))

    logger.info("TRADE CLOSED: id=%s exit=%.2f pnl=$%.2f (%.2fR) reason=%s",
                trade_id, exit_price, pnl, pnl_r, exit_reason)


def emergency_close_all() -> None:
    """Cancel all open orders and close all positions. Called by kill switch / EOD."""
    logger.critical("EMERGENCY CLOSE ALL: cancelling orders and closing positions")
    cancel_all_orders()
    close_all_positions()


def get_open_positions_from_db() -> list:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM trades WHERE mode='paper' AND closed_at IS NULL ORDER BY opened_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]
