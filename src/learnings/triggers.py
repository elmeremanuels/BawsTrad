from __future__ import annotations

"""
Learning trigger detection.
Monitors trade results and time-based events, fires the appropriate extractor.
"""

import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

from src.storage.db import get_connection

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")


def is_big_win(trade: dict, top_pct_threshold: float = 0.90) -> bool:
    pnl_r = trade.get("pnl_r", 0) or 0
    if pnl_r >= 3.0:
        return True
    # Top 10% of today's trades
    trades_today = _todays_closed_trades()
    if len(trades_today) < 3:
        return False
    sorted_r = sorted(t.get("pnl_r", 0) or 0 for t in trades_today)
    threshold = sorted_r[int(len(sorted_r) * top_pct_threshold)]
    return pnl_r >= threshold


def is_big_loss(trade: dict) -> bool:
    pnl_r = trade.get("pnl_r", 0) or 0
    return pnl_r <= -1.0


def _todays_closed_trades() -> list:
    today = date.today().isoformat()
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT * FROM trades
            WHERE closed_at >= ? AND closed_at IS NOT NULL
            ORDER BY closed_at
        """, (today,)).fetchall()
    return [dict(r) for r in rows]


def should_run_eod(now: Optional[datetime] = None) -> bool:
    t = (now or datetime.now(ET)).astimezone(ET)
    return t.hour == 16 and t.minute == 15


def should_run_eow(now: Optional[datetime] = None) -> bool:
    t = (now or datetime.now(ET)).astimezone(ET)
    return t.weekday() == 4 and t.hour == 17 and t.minute == 0


def should_run_eom(now: Optional[datetime] = None) -> bool:
    t = (now or datetime.now(ET)).astimezone(ET)
    # Last trading day of month = today's month != tomorrow's month
    from datetime import timedelta
    tomorrow = (t + timedelta(days=1)).date()
    return (tomorrow.month != t.month and t.hour == 17 and t.minute == 30)


# Fix the Optional import
from typing import Optional
