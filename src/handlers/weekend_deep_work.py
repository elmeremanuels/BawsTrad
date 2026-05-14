from __future__ import annotations

"""
WEEKEND_DEEP_WORK handler — NYSE holidays and weekends.

Responsibilities:
- Run weekly backtest on recent 5-day window (scripts/backtest_recent.py)
- Extract weekly learnings via Claude
- Update strategy notes in SQLite
- Post Discord weekly summary

This handler is intentionally thin for now (Step 4 skeleton).
Full implementation arrives in Step 9.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import structlog

ET = ZoneInfo("America/New_York")
log = structlog.get_logger(__name__)


async def tick(state, now: datetime) -> None:
    """
    Called every 5 seconds while in WEEKEND_DEEP_WORK mode.
    Skeleton — full implementation in Step 9.
    """
    log.debug("weekend_deep_work idle", time=now.strftime("%H:%M"))
