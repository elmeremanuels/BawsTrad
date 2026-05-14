from __future__ import annotations

"""
EOD_REFLECT handler — market close until 20:00 ET on NYSE trading days.

Responsibilities:
- Run EOD summary (already in main.py, will be migrated here in Step 8)
- Trigger learning extraction via Claude
- Post Discord summary
- Log daily stats to bot_events

This handler is intentionally thin for now (Step 4 skeleton).
Full implementation arrives in Step 8.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import structlog

ET = ZoneInfo("America/New_York")
log = structlog.get_logger(__name__)

_summary_fired: bool = False  # Module-level guard — fire once per session


async def tick(state, now: datetime) -> None:
    """
    Called every 5 seconds while in EOD_REFLECT mode.
    Fires the EOD summary once per session then idles.
    """
    global _summary_fired
    if _summary_fired:
        log.debug("eod_reflect idle")
        return

    log.info("eod_reflect: EOD summary pending (Step 8 migration)")
    _summary_fired = True
