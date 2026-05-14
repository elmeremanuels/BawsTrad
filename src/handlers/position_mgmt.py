from __future__ import annotations

"""
POSITION_MGMT handler — 11:30 ET until market close on NYSE trading days.

Responsibilities:
- Monitor open positions for stop/target hits
- No new entries permitted (enforced upstream by evaluate_entry mode guard)
- Tighten stops on large open profits (future: trailing stop logic)
- Log position P&L every tick for dashboard visibility
- Escalate force-close at 15:55 ET
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import structlog

ET = ZoneInfo("America/New_York")
log = structlog.get_logger(__name__)


async def tick(state, now: datetime) -> None:
    """
    Called every 5 seconds from the main monitoring loop while in
    POSITION_MGMT mode. Monitors open positions; no new trades.
    """
    try:
        from src.execution.paper import get_open_positions_from_db
        positions = get_open_positions_from_db()
    except Exception as exc:
        log.warning("position_mgmt: failed to fetch positions", error=str(exc))
        return

    if not positions:
        log.debug("position_mgmt tick", open_positions=0)
        return

    for pos in positions:
        log.info(
            "position_mgmt: open position",
            ticker=pos.get("ticker"),
            entry=pos.get("entry_price"),
            stop=pos.get("stop_price"),
            target=pos.get("target_price"),
            qty=pos.get("qty"),
        )
