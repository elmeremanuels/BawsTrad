from __future__ import annotations

"""
Cost Tracker — System 2 (PROBLEEM 2).

SPRINT 1 SCOPE: measurement only.
  ✅ Snapshot bid/ask spread at setup detection
  ✅ Record entry & exit fill slippage
  ✅ Finalize per-trade cost metrics (gross R, net R, cost R)
  ✅ Expose summary statistics

NOT YET (Sprint 2):
  ❌ Integration with decision engine (edge check before trade)
  ❌ Auto-switch to limit orders when cost_pct > 50%
  ❌ CostPredictor ML model (needs 50+ trades of training data)

DB table: cost_metrics (see db.py init_schema)

Slippage convention:
  entry_slippage_pct = (fill_price - midpoint) / midpoint × 100
  Positive = paid more than midpoint (bad for buyer = us)
  Negative = got fill below midpoint (rare, good)

Usage:
    from src.execution.cost_tracker import CostTracker
    tracker = CostTracker()

    # At setup detection (before order placement):
    tracker.snapshot_pre_trade(trade_id, ticker)

    # After entry fill (called from reconcile loop):
    tracker.record_entry_fill(trade_id, fill_price=5.42)

    # After exit fill:
    tracker.record_exit_fill(trade_id, fill_price=5.91)

    # After trade is closed in DB:
    tracker.finalize(trade_id)

    # Daily / dashboard query:
    summary = tracker.get_summary(days=30)
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


def _get_latest_quote(ticker: str) -> Optional[dict]:
    """Fetch latest NBBO quote from Alpaca data API."""
    try:
        url = f"{settings.ALPACA_DATA_URL}/v2/stocks/{ticker}/quotes/latest"
        with httpx.Client(headers=_HEADERS, timeout=8.0) as c:
            r = c.get(url, params={"feed": "iex"})
            r.raise_for_status()
            data = r.json()
            return data.get("quote") or {}
    except Exception as exc:
        logger.debug("cost_tracker: quote fetch failed for %s: %s", ticker, exc)
        return None


class CostTracker:
    """
    Per-trade execution cost measurement.
    All writes are idempotent (INSERT OR IGNORE / UPDATE).
    Silently swallows errors — never breaks the main trading loop.
    """

    # ── Pre-trade snapshot ────────────────────────────────────────────────────

    def snapshot_pre_trade(self, trade_id: str, ticker: str) -> None:
        """
        Capture current bid/ask/midpoint/spread at setup detection time.
        Called BEFORE order is placed.
        """
        quote = _get_latest_quote(ticker)
        if not quote:
            logger.debug("cost_tracker: no quote for %s, skipping snapshot", ticker)
            self._insert_empty(trade_id)
            return

        bid = float(quote.get("bp", 0) or 0)
        ask = float(quote.get("ap", 0) or 0)

        if bid <= 0 or ask <= 0 or ask < bid:
            logger.debug("cost_tracker: invalid quote for %s (bid=%.4f ask=%.4f)", ticker, bid, ask)
            self._insert_empty(trade_id)
            return

        midpoint    = (bid + ask) / 2.0
        spread_pct  = (ask - bid) / ask * 100.0

        try:
            with get_connection() as conn:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO cost_metrics
                    (trade_id, quote_bid_decision, quote_ask_decision,
                     midpoint_decision, spread_at_entry, snapshot_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (trade_id, round(bid, 4), round(ask, 4),
                     round(midpoint, 4), round(spread_pct, 4),
                     datetime.now(timezone.utc).isoformat()),
                )
        except Exception as exc:
            logger.warning("cost_tracker.snapshot_pre_trade failed: %s", exc)

        logger.debug(
            "cost_tracker: snapshot %s bid=%.4f ask=%.4f spread=%.3f%%",
            trade_id, bid, ask, spread_pct,
        )

    # ── Fill recording ────────────────────────────────────────────────────────

    def record_entry_fill(self, trade_id: str, fill_price: float) -> None:
        """Record actual entry fill price and compute entry slippage."""
        self._record_fill(trade_id, fill_price, side="entry")

    def record_exit_fill(self, trade_id: str, fill_price: float) -> None:
        """Record actual exit fill price and compute exit slippage."""
        self._record_fill(trade_id, fill_price, side="exit")

    def _record_fill(self, trade_id: str, fill_price: float, side: str) -> None:
        try:
            with get_connection() as conn:
                row = conn.execute(
                    "SELECT midpoint_decision FROM cost_metrics WHERE trade_id = ?",
                    (trade_id,),
                ).fetchone()

            midpoint = float(row["midpoint_decision"]) if row else None
            slippage_pct: Optional[float] = None
            if midpoint and midpoint > 0:
                slippage_pct = round((fill_price - midpoint) / midpoint * 100.0, 4)

            col_price    = f"{side}_fill_price"
            col_slippage = f"{side}_slippage_pct"

            with get_connection() as conn:
                # Ensure row exists
                conn.execute(
                    "INSERT OR IGNORE INTO cost_metrics (trade_id) VALUES (?)",
                    (trade_id,),
                )
                conn.execute(
                    f"UPDATE cost_metrics SET {col_price} = ?, {col_slippage} = ? WHERE trade_id = ?",
                    (round(fill_price, 4), slippage_pct, trade_id),
                )
        except Exception as exc:
            logger.warning("cost_tracker.record_%s_fill failed: %s", side, exc)

    # ── Finalization ──────────────────────────────────────────────────────────

    def finalize(self, trade_id: str) -> None:
        """
        Compute gross_profit_R, total_cost_R, net_profit_R after trade closes.
        Called from reconcile loop after close_trade() writes pnl_r to trades table.
        """
        try:
            with get_connection() as conn:
                cost_row = conn.execute(
                    "SELECT * FROM cost_metrics WHERE trade_id = ?", (trade_id,)
                ).fetchone()
                trade_row = conn.execute(
                    "SELECT pnl_r, entry_price, stop_price FROM trades WHERE id = ?",
                    (trade_id,),
                ).fetchone()

            if not cost_row or not trade_row:
                return

            gross_r = float(trade_row["pnl_r"]) if trade_row["pnl_r"] is not None else None
            if gross_r is None:
                return

            # Total cost in R-units:
            # spread cost + entry slippage + exit slippage
            risk_per_share = abs(
                float(trade_row["entry_price"]) - float(trade_row["stop_price"])
            )
            if risk_per_share <= 0:
                return

            entry_price = float(trade_row["entry_price"])

            def _to_r(slippage_pct: Optional[float]) -> float:
                if slippage_pct is None:
                    return 0.0
                return abs(slippage_pct) / 100.0 * entry_price / risk_per_share

            spread_pct    = float(cost_row["spread_at_entry"] or 0)
            spread_r      = (spread_pct / 100.0 * entry_price) / risk_per_share / 2.0  # half-spread
            entry_slip_r  = _to_r(cost_row["entry_slippage_pct"])
            exit_slip_r   = _to_r(cost_row["exit_slippage_pct"])
            total_cost_r  = round(spread_r + entry_slip_r + exit_slip_r, 4)
            net_r         = round(gross_r - total_cost_r, 4)

            with get_connection() as conn:
                conn.execute(
                    """
                    UPDATE cost_metrics
                    SET gross_profit_R = ?, total_cost_R = ?, net_profit_R = ?
                    WHERE trade_id = ?
                    """,
                    (round(gross_r, 4), total_cost_r, net_r, trade_id),
                )

            logger.info(
                "cost_tracker: finalized %s gross=%.3fR cost=%.3fR net=%.3fR",
                trade_id[:12], gross_r, total_cost_r, net_r,
            )
        except Exception as exc:
            logger.warning("cost_tracker.finalize failed for %s: %s", trade_id, exc)

    # ── Summary / reporting ───────────────────────────────────────────────────

    def get_summary(self, days: int = 30) -> dict:
        """
        Return a dict with rolling cost metrics for the last `days` days.
        Used by dashboard and Discord alerts.
        """
        try:
            with get_connection() as conn:
                row = conn.execute(
                    """
                    SELECT
                        COUNT(*)                                                AS n_trades,
                        AVG(ABS(entry_slippage_pct))                            AS avg_entry_slip_pct,
                        AVG(ABS(exit_slippage_pct))                             AS avg_exit_slip_pct,
                        AVG(spread_at_entry)                                    AS avg_spread_pct,
                        AVG(total_cost_R)                                       AS avg_cost_r,
                        AVG(net_profit_R)                                       AS avg_net_r,
                        AVG(gross_profit_R)                                     AS avg_gross_r,
                        SUM(CASE WHEN gross_profit_R > 0 THEN 1 ELSE 0 END)    AS n_winning,
                        AVG(
                            CASE WHEN gross_profit_R > 0
                                 THEN total_cost_R / gross_profit_R * 100
                            END
                        )                                                       AS cost_pct_of_gross
                    FROM cost_metrics
                    WHERE snapshot_at >= datetime('now', ?)
                      AND total_cost_R IS NOT NULL
                    """,
                    (f"-{days} days",),
                ).fetchone()
            if not row:
                return {}
            return {
                "days":              days,
                "n_trades":          row["n_trades"] or 0,
                "avg_entry_slip_pct": round(row["avg_entry_slip_pct"] or 0, 3),
                "avg_exit_slip_pct":  round(row["avg_exit_slip_pct"] or 0, 3),
                "avg_spread_pct":     round(row["avg_spread_pct"] or 0, 3),
                "avg_cost_r":         round(row["avg_cost_r"] or 0, 3),
                "avg_net_r":          round(row["avg_net_r"] or 0, 3),
                "avg_gross_r":        round(row["avg_gross_r"] or 0, 3),
                "cost_pct_of_gross":  round(row["cost_pct_of_gross"] or 0, 1),
                "n_winning":          row["n_winning"] or 0,
            }
        except Exception as exc:
            logger.warning("cost_tracker.get_summary failed: %s", exc)
            return {}

    def check_cost_health(self) -> Optional[str]:
        """
        Sprint-1 read-only health check.
        Returns a warning string if cost_pct > 35%, None if healthy.
        Decision engine integration is Sprint 2.
        """
        summary = self.get_summary(days=30)
        if summary.get("n_trades", 0) < 20:
            return None  # Not enough data yet

        cost_pct = summary.get("cost_pct_of_gross", 0)
        if cost_pct > 50:
            return f"🚨 Costs = {cost_pct:.0f}% of gross profit (>50% threshold)"
        if cost_pct > 35:
            return f"⚠️ Costs = {cost_pct:.0f}% of gross profit (elevated)"
        return None

    # ── Private helpers ───────────────────────────────────────────────────────

    def _insert_empty(self, trade_id: str) -> None:
        try:
            with get_connection() as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO cost_metrics (trade_id, snapshot_at) VALUES (?, ?)",
                    (trade_id, datetime.now(timezone.utc).isoformat()),
                )
        except Exception:
            pass
