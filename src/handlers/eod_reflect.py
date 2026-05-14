from __future__ import annotations

"""
EOD_REFLECT handler — market close until 20:00 ET on NYSE trading days.

Schedule (fires once per session):
  on_enter   EOD trade summary → Discord
  16:15 ET   Learning extraction (EOD scope)
  17:00 ET   Week-in-review extraction (Fridays only)
  17:30 ET   Month-in-review extraction (last trading day of month)

All learning extractions are async/non-blocking — failures are logged
but do not stop the handler.
"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import structlog

ET = ZoneInfo("America/New_York")
log = structlog.get_logger(__name__)

_tasks_fired: set = set()


def _due_task(now_et: datetime) -> str | None:
    tasks = {
        (16, 15): "eod_learning",
        (17, 0):  "eow_learning",
        (17, 30): "eom_learning",
    }
    for (h, m), name in tasks.items():
        if now_et.hour == h and now_et.minute >= m and now_et.minute < m + 5:
            if name not in _tasks_fired:
                return name
    return None


async def on_enter(state) -> None:
    """
    Called once when the bot transitions INTO EOD_REFLECT mode.
    Posts EOD trade summary to Discord.
    """
    if "eod_summary" in _tasks_fired:
        return
    _tasks_fired.add("eod_summary")

    log.info("eod_reflect: building EOD summary")
    try:
        from src.learnings.triggers import _todays_closed_trades
        from src.learnings.extractor import extract_period_learning
        from src.config import settings
        from src.alerts.discord import post_message

        trades = _todays_closed_trades()
        if not trades:
            await post_message(settings.DISCORD_WEBHOOK_URL,
                               f"📊 EOD {date.today()} — No trades today.")
            return

        wins = [t for t in trades if (t.get("pnl_r") or 0) >= 0]
        total_pnl = sum(t.get("pnl_dollars", 0) or 0 for t in trades)
        avg_r = sum(t.get("pnl_r", 0) or 0 for t in trades) / len(trades)
        best = max(trades, key=lambda t: t.get("pnl_r", 0) or 0)

        eod_msg = (
            f"📊 **EOD** {date.today()} | {len(trades)} trades, "
            f"{len(wins)}W/{len(trades) - len(wins)}L, "
            f"${total_pnl:+.2f} ({avg_r:+.2f}R avg)\n"
            f"🏆 Best: {best['ticker']} {best.get('setup_type', '')} "
            f"{(best.get('pnl_r') or 0):+.2f}R"
        )
        await post_message(settings.DISCORD_WEBHOOK_URL, eod_msg)
        log.info("eod_reflect: EOD summary posted", trades=len(trades), pnl=total_pnl)

    except Exception as exc:
        log.error("eod_reflect: EOD summary failed", error=str(exc))


async def _run_eod_learning(state) -> None:
    """16:15 — Extract EOD learning from today's trades."""
    try:
        from src.learnings.triggers import _todays_closed_trades
        from src.learnings.extractor import extract_period_learning
        from src.config import settings
        from src.alerts.discord import post_message

        trades = _todays_closed_trades()
        if not trades:
            return

        discord_msg = extract_period_learning(trades, "eod", "day")
        if discord_msg:
            await post_message(settings.DISCORD_WEBHOOK_URL, f"💡 {discord_msg}")
            log.info("eod_reflect: learning extracted and posted")
        else:
            log.info("eod_reflect: no learning generated")
    except Exception as exc:
        log.error("eod_reflect: learning extraction failed", error=str(exc))


async def _run_eow_learning(state, now_et: datetime) -> None:
    """17:00 Friday — Extract week-in-review learning."""
    if now_et.weekday() != 4:  # 4 = Friday
        return
    try:
        from src.learnings.triggers import _todays_closed_trades
        from src.learnings.extractor import extract_period_learning
        from src.config import settings
        from src.alerts.discord import post_message
        from src.storage.db import get_connection
        from datetime import timedelta

        # Fetch this week's trades
        week_start = (now_et.date() - timedelta(days=now_et.weekday())).isoformat()
        with get_connection() as conn:
            rows = conn.execute("""
                SELECT * FROM trades
                WHERE closed_at >= ? AND closed_at IS NOT NULL
                ORDER BY closed_at
            """, (week_start,)).fetchall()
        trades = [dict(r) for r in rows]

        if not trades:
            return

        discord_msg = extract_period_learning(trades, "eow", "week")
        if discord_msg:
            await post_message(settings.DISCORD_WEBHOOK_URL, f"📅 **Week review**: {discord_msg}")
    except Exception as exc:
        log.error("eod_reflect: EOW learning failed", error=str(exc))


async def _run_eom_learning(state, now_et: datetime) -> None:
    """17:30 last trading day of month — Extract month-in-review learning."""
    from datetime import timedelta
    tomorrow = (now_et + timedelta(days=1)).date()
    if tomorrow.month == now_et.month:
        return  # Not last trading day of month
    try:
        from src.learnings.extractor import extract_period_learning
        from src.config import settings
        from src.alerts.discord import post_message
        from src.storage.db import get_connection

        month_start = now_et.date().replace(day=1).isoformat()
        with get_connection() as conn:
            rows = conn.execute("""
                SELECT * FROM trades
                WHERE closed_at >= ? AND closed_at IS NOT NULL
                ORDER BY closed_at
            """, (month_start,)).fetchall()
        trades = [dict(r) for r in rows]

        if not trades:
            return

        discord_msg = extract_period_learning(trades, "eom", "month")
        if discord_msg:
            await post_message(settings.DISCORD_WEBHOOK_URL, f"📆 **Month review**: {discord_msg}")
    except Exception as exc:
        log.error("eod_reflect: EOM learning failed", error=str(exc))


async def tick(state, now: datetime) -> None:
    """
    Called every 5 seconds while in EOD_REFLECT mode.
    Fires scheduled learning extraction tasks.
    """
    now_et = now.astimezone(ET)
    task = _due_task(now_et)

    if task is None:
        log.debug("eod_reflect idle", time=now_et.strftime("%H:%M"))
        return

    _tasks_fired.add(task)
    log.info("eod_reflect: running task", task=task)

    if task == "eod_learning":
        await _run_eod_learning(state)
    elif task == "eow_learning":
        await _run_eow_learning(state, now_et)
    elif task == "eom_learning":
        await _run_eom_learning(state, now_et)
