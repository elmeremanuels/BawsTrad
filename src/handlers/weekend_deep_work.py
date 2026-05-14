from __future__ import annotations

"""
WEEKEND_DEEP_WORK handler — NYSE holidays and weekends.

Schedule (fires once per Saturday session):
  Sat 09:00 ET  Run recent backtest (last 5 trading days)
  Sat 10:00 ET  Extract weekly strategy learnings via Claude
  Sat 11:00 ET  Post Discord weekend summary

On non-Saturday weekends/holidays: idles, logs briefly.
"""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import structlog

ET = ZoneInfo("America/New_York")
log = structlog.get_logger(__name__)

_tasks_fired: set = set()


def _due_task(now_et: datetime) -> str | None:
    if now_et.weekday() != 5:  # Only Saturday
        return None
    tasks = {
        (9,  0): "backtest_recent",
        (10, 0): "weekly_learning",
        (11, 0): "discord_summary",
    }
    for (h, m), name in tasks.items():
        if now_et.hour == h and now_et.minute >= m and now_et.minute < m + 5:
            # Reset on each Saturday (date-keyed to avoid cross-week locks)
            task_key = f"{now_et.date().isoformat()}:{name}"
            if task_key not in _tasks_fired:
                return name
    return None


def _task_key(now_et: datetime, name: str) -> str:
    return f"{now_et.date().isoformat()}:{name}"


async def _run_backtest_recent(state, now_et: datetime) -> None:
    """
    Run backtest_recent.py covering the last 5 trading days.
    Runs in a subprocess to avoid blocking the event loop.
    """
    import asyncio
    import sys

    log.info("weekend_deep_work: starting recent backtest (last 5 days)")
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "scripts/backtest_recent.py",
            "--days", "5",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
        if proc.returncode == 0:
            log.info("weekend_deep_work: backtest_recent complete",
                     output=stdout.decode()[-500:] if stdout else "")
        else:
            log.warning("weekend_deep_work: backtest_recent non-zero exit",
                        rc=proc.returncode,
                        stderr=stderr.decode()[-300:] if stderr else "")
    except asyncio.TimeoutError:
        log.error("weekend_deep_work: backtest_recent timed out after 5 min")
    except FileNotFoundError:
        log.warning("weekend_deep_work: scripts/backtest_recent.py not found — skipping")
    except Exception as exc:
        log.error("weekend_deep_work: backtest_recent failed", error=str(exc))


async def _run_weekly_learning(state) -> None:
    """Extract weekly learning summary via Claude."""
    try:
        from src.learnings.extractor import extract_period_learning
        from src.config import settings
        from src.alerts.discord import post_message
        from src.storage.db import get_connection

        # Last 7 calendar days of trades
        week_ago = (date.today() - timedelta(days=7)).isoformat()
        with get_connection() as conn:
            rows = conn.execute("""
                SELECT * FROM trades
                WHERE closed_at >= ? AND closed_at IS NOT NULL
                ORDER BY closed_at
            """, (week_ago,)).fetchall()
        trades = [dict(r) for r in rows]

        if not trades:
            log.info("weekend_deep_work: no trades this week — skipping learning")
            return

        discord_msg = extract_period_learning(trades, "eow", "week")
        if discord_msg:
            await post_message(settings.DISCORD_WEBHOOK_URL,
                               f"📊 **Weekly learning** (weekend review):\n{discord_msg}")
            log.info("weekend_deep_work: weekly learning posted", trades=len(trades))
    except Exception as exc:
        log.error("weekend_deep_work: weekly learning failed", error=str(exc))


async def _run_discord_summary(state) -> None:
    """Post a brief weekend status to Discord."""
    try:
        from src.config import settings
        from src.alerts.discord import post_message
        from src.storage.db import get_connection

        week_ago = (date.today() - timedelta(days=7)).isoformat()
        with get_connection() as conn:
            row = conn.execute("""
                SELECT COUNT(*) as n,
                       SUM(pnl_dollars) as total_pnl,
                       AVG(pnl_r) as avg_r
                FROM trades
                WHERE closed_at >= ? AND closed_at IS NOT NULL
            """, (week_ago,)).fetchone()

        n = row["n"] or 0
        total_pnl = row["total_pnl"] or 0.0
        avg_r = row["avg_r"] or 0.0

        msg = (
            f"🏖️ **Weekend review** | {date.today()}\n"
            f"Last 7 days: {n} trades, ${total_pnl:+.2f} ({avg_r:+.2f}R avg)\n"
            f"Bot resting until Monday pre-market."
        )
        await post_message(settings.DISCORD_WEBHOOK_URL, msg)
        log.info("weekend_deep_work: Discord summary posted")
    except Exception as exc:
        log.error("weekend_deep_work: Discord summary failed", error=str(exc))


async def tick(state, now: datetime) -> None:
    """
    Called every 5 seconds while in WEEKEND_DEEP_WORK mode.
    On Saturdays fires scheduled tasks; otherwise idles.
    """
    now_et = now.astimezone(ET)
    task = _due_task(now_et)

    if task is None:
        log.debug("weekend_deep_work idle", weekday=now_et.strftime("%A"),
                  time=now_et.strftime("%H:%M"))
        return

    task_key = _task_key(now_et, task)
    _tasks_fired.add(task_key)
    log.info("weekend_deep_work: running task", task=task)

    if task == "backtest_recent":
        await _run_backtest_recent(state, now_et)
    elif task == "weekly_learning":
        await _run_weekly_learning(state)
    elif task == "discord_summary":
        await _run_discord_summary(state)
