from __future__ import annotations

"""
OVERNIGHT_INTEL handler — 20:00 ET until 04:00 ET (next day).

Responsibilities:
- Ingest overnight news into SQLite via Finnhub
- Store articles in overnight_intel table (added in Step 6)
- Run sentiment pre-scoring so pre-market briefing starts with warm data
- Idle most of the time (fires at 20:00, 22:00, 02:00 ET)

This handler is intentionally thin for now (Step 4 skeleton).
Full implementation arrives in Step 6.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import structlog

ET = ZoneInfo("America/New_York")
log = structlog.get_logger(__name__)


async def tick(state, now: datetime) -> None:
    """
    Called every 5 seconds while in OVERNIGHT_INTEL mode.
    Skeleton — full implementation in Step 6.
    """
    log.debug("overnight_intel idle", time=now.strftime("%H:%M"))
