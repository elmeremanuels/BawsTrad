from __future__ import annotations

"""
ACTIVE_TRADING handler — 09:30–11:30 ET on NYSE trading days.

Responsibilities:
- WebSocket bar stream drives on_bar() tick-by-tick
- Pattern detection → evaluate_entry() → open_long()
- Force-close guard at 15:55 (escalated from main loop)

This module owns no state; it reads from BotState and writes via
the paper execution layer only.
"""

import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import structlog

ET = ZoneInfo("America/New_York")
log = structlog.get_logger(__name__)


async def tick(state, now: datetime) -> None:
    """
    Called every 5 seconds from the main monitoring loop while in
    ACTIVE_TRADING mode. Currently a no-op — real work happens in
    on_bar() which is driven by the WebSocket stream callback.
    """
    log.debug("active_trading tick", time=now.strftime("%H:%M:%S"))
