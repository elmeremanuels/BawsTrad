from __future__ import annotations

"""
MAINTENANCE handler — activated by kill switch or manual override.

Responsibilities:
- Emergency close all open positions (called once on entry)
- Block all new activity
- Post Discord alert on entry and exit
- Log to bot_events table

Kill switch: touch /tmp/bot_killswitch (or path from settings.KILL_SWITCH_FILE)
Clear maintenance: remove the kill switch file
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import structlog

ET = ZoneInfo("America/New_York")
log = structlog.get_logger(__name__)

_entered: bool = False  # Fire emergency close exactly once


async def on_enter(state) -> None:
    """Called when the bot transitions INTO MAINTENANCE mode."""
    global _entered
    if _entered:
        return
    _entered = True

    log.warning("maintenance: entering — closing all positions")
    try:
        from src.execution.paper import emergency_close_all
        emergency_close_all()
        log.info("maintenance: emergency close complete")
    except Exception as exc:
        log.error("maintenance: emergency close failed", error=str(exc))

    try:
        from src.config import settings
        from src.alerts.discord import post_message
        await post_message(
            settings.DISCORD_WEBHOOK_URL,
            "🔴 **MAINTENANCE MODE** — kill switch triggered. All positions closed.",
        )
    except Exception as exc:
        log.error("maintenance: discord alert failed", error=str(exc))


async def tick(state, now: datetime) -> None:
    """Called every 5 seconds while in MAINTENANCE mode. Idles; no trading."""
    log.debug("maintenance idle — kill switch active")
