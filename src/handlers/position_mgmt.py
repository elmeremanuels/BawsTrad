from __future__ import annotations

"""
POSITION_MGMT handler — 11:30 ET until market close on NYSE trading days.

Responsibilities:
- Monitor open positions for stop/target hits (Alpaca handles bracket fills)
- Log position P&L mark-to-market every tick for dashboard visibility
- No new entries permitted (enforced upstream by evaluate_entry mode guard)
- Detect stale open positions (opened_at > 4h ago, likely missed fill confirmation)
- Escalate force-close at 15:55 ET is handled in main loop; this handler
  just monitors and logs.
"""

from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import structlog

ET = ZoneInfo("America/New_York")
log = structlog.get_logger(__name__)

# Log open positions at most once every N seconds to avoid log spam
_LOG_INTERVAL_SECONDS = 60
_last_log_time: float = 0.0

# Positions open longer than this are flagged as potentially stale
_STALE_POSITION_HOURS = 4


async def tick(state, now: datetime) -> None:
    """
    Called every 5 seconds while in POSITION_MGMT mode.
    Logs open positions periodically; flags stale positions.
    No new trades are opened.
    """
    import time
    global _last_log_time

    try:
        from src.execution.paper import get_open_positions_from_db
        positions = get_open_positions_from_db()
    except Exception as exc:
        log.warning("position_mgmt: failed to fetch positions", error=str(exc))
        return

    if not positions:
        log.debug("position_mgmt: no open positions")
        return

    # Rate-limit position logging to once per minute
    elapsed = time.monotonic() - _last_log_time
    if elapsed < _LOG_INTERVAL_SECONDS:
        return
    _last_log_time = time.monotonic()

    # Stale threshold in UTC so we can compare with opened_at (stored as UTC ISO string)
    stale_threshold_utc = now.astimezone(timezone.utc) - timedelta(hours=_STALE_POSITION_HOURS)

    for pos in positions:
        opened_at_str = pos.get("opened_at", "")
        try:
            opened_at = datetime.fromisoformat(opened_at_str)
            # Ensure timezone-aware (DB stores UTC ISO; fromisoformat preserves +00:00)
            if opened_at.tzinfo is None:
                opened_at = opened_at.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            opened_at = None

        is_stale = opened_at is not None and opened_at < stale_threshold_utc

        log.info(
            "position_mgmt: open position",
            ticker=pos.get("ticker"),
            entry=pos.get("entry_price"),
            stop=pos.get("stop_price"),
            target=pos.get("target_price"),
            qty=pos.get("qty") or pos.get("shares"),
            setup_type=pos.get("setup_type"),
            opened_at=opened_at_str,
            stale=is_stale,
        )

        if is_stale:
            hours_open = round(
                (now.astimezone(timezone.utc) - opened_at).total_seconds() / 3600, 1
            ) if opened_at else "unknown"
            log.warning(
                "position_mgmt: STALE position detected — may need manual review",
                ticker=pos.get("ticker"),
                hours_open=hours_open,
            )
