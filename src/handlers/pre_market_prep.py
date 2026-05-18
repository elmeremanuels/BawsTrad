from __future__ import annotations

"""
PRE_MARKET_PREP handler — 04:00–09:30 ET on NYSE trading days.

Schedule (ET, fires at most once per session window):
  04:00  Universe refresh — run scripts/refresh_universe.py if not done today
  04:10  News ingest — fetch overnight news for all tickers
  06:00  Watchlist ranking — score candidates by quality_score
  07:30  Learnings review — pull active learnings from SQLite
  08:30  Perplexity briefing — market context + sector watch + avoid list
  08:45  Discord alert — post briefing summary
  09:00  Pre-execution check — validate data feeds, kill switch absent
  09:25  T-5 alert — "Market opens in 5 minutes"

Each task fires within the first 5 minutes of its window and is tracked
by a module-level set so restarts within the window don't double-fire.
"""

from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import structlog

ET = ZoneInfo("America/New_York")
log = structlog.get_logger(__name__)

# Tasks identified by (hour, minute) tuples of their trigger windows
_tasks_fired: set = set()

_TASKS = {
    (4, 0):  "universe_refresh",
    (4, 10): "news_ingest",
    (6, 0):  "watchlist_rank",
    (7, 30): "learnings_review",
    (8, 30): "perplexity_briefing",
    (8, 45): "discord_briefing",
    (9, 0):  "pre_exec_check",
    (9, 25): "t5_alert",
}


def _due_task(now_et: datetime) -> str | None:
    """Return the task name due at this moment, or None."""
    for (h, m), name in _TASKS.items():
        if now_et.hour == h and now_et.minute >= m and now_et.minute < m + 5:
            if name not in _tasks_fired:
                return name
    return None


async def _run_universe_refresh(state) -> None:
    """04:00 — Run refresh_universe.py if universe_today.csv doesn't exist for today."""
    import asyncio
    import os
    import sys

    universe_path = Path("universe_today.csv")

    # Skip if file already exists and was written today
    if universe_path.exists():
        mtime = datetime.fromtimestamp(universe_path.stat().st_mtime).date()
        if mtime >= date.today():
            log.info("pre_market_prep: universe_today.csv already fresh, skipping refresh",
                     mtime=str(mtime))
            return

    log.info("pre_market_prep: running universe refresh script")
    script = Path(__file__).resolve().parent.parent.parent / "scripts" / "refresh_universe.py"

    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(script),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=120)
        output = stdout.decode(errors="replace").strip() if stdout else ""
        if output:
            for line in output.splitlines():
                log.info("refresh_universe", output=line)
        if proc.returncode != 0:
            log.warning("pre_market_prep: refresh_universe exited non-zero",
                        returncode=proc.returncode)
        else:
            log.info("pre_market_prep: universe refresh complete")
    except asyncio.TimeoutError:
        log.warning("pre_market_prep: universe refresh timed out after 120s")
    except Exception as exc:
        log.error("pre_market_prep: universe refresh failed", error=str(exc))


async def _run_news_ingest(state) -> None:
    """04:10 — Ingest overnight news for all tickers."""
    tickers = list(getattr(state, "_float_map", {}).keys())
    if not tickers:
        log.warning("pre_market_prep: no tickers for news ingest")
        return

    from src.news.ingest import ingest_news_for_ticker
    today = date.today()
    total = 0
    for ticker in tickers:
        try:
            count = ingest_news_for_ticker(ticker)
            total += (count or 0)
        except Exception as exc:
            log.warning("pre_market_prep: news ingest failed", ticker=ticker, error=str(exc))

    log.info("pre_market_prep: news ingest complete", tickers=len(tickers), articles=total)


async def _run_watchlist_rank(state) -> None:
    """06:00 — Re-score universe and update watchlist ranking."""
    from src.data.alpaca_client import get_snapshots
    from src.scanner.criteria import NewsCatalyst, TickerSnapshot, passes_stock_selection, quality_score
    from src.news.ingest import get_best_catalyst_today

    tickers = list(getattr(state, "_float_map", {}).keys())
    if not tickers:
        return

    try:
        snaps = get_snapshots(tickers)
    except Exception as exc:
        log.warning("pre_market_prep: snapshot fetch failed", error=str(exc))
        return

    float_map = getattr(state, "_float_map", {})
    candidates = []

    for ticker, snap in snaps.items():
        prev_close = (snap.get("prevDailyBar") or {}).get("c", 0)
        daily = snap.get("dailyBar") or {}
        price = daily.get("c") or daily.get("o") or 0
        vol = daily.get("v") or 0

        if prev_close <= 0 or price <= 0:
            continue

        gap_pct = (price - prev_close) / prev_close * 100.0
        best = get_best_catalyst_today(ticker)
        catalyst = None
        if best:
            catalyst = NewsCatalyst(
                tier=best["tier"],
                category=best["category"],
                headline=best["headline"],
            )

        s = TickerSnapshot(
            ticker=ticker,
            price=price,
            percent_change_today=gap_pct,
            relative_volume=5.0,
            float_shares=float_map.get(ticker, 15_000_000),
            news_catalyst=catalyst,
        )
        if passes_stock_selection(s):
            candidates.append((quality_score(s), s))

    candidates.sort(reverse=True)
    state.watchlist = [s for _, s in candidates[:5]]
    log.info("pre_market_prep: watchlist ranked", candidates=len(candidates),
             watchlist=[s.ticker for s in state.watchlist])


async def _run_learnings_review(state) -> None:
    """07:30 — Load active learnings so briefing has context."""
    try:
        from src.learnings.feedback import get_briefing_context
        learnings = get_briefing_context()
        state.active_learnings = learnings
        log.info("pre_market_prep: learnings loaded", count=len(learnings))
    except Exception as exc:
        log.warning("pre_market_prep: learnings load failed", error=str(exc))
        state.active_learnings = []


async def _run_perplexity_briefing(state) -> None:
    """08:30 — Fetch Perplexity pre-market briefing."""
    try:
        from src.research.perplexity import get_premarket_briefing
        scanner_tickers = [s.ticker for s in (state.watchlist or [])]
        briefing = get_premarket_briefing(
            briefing_date=date.today(),
            scanner_tickers=scanner_tickers,
            active_learnings=getattr(state, "active_learnings", []),
        )
        if briefing:
            state.briefing = vars(briefing)
            log.info("pre_market_prep: briefing ready", avoid=briefing.avoid_today[:60])
        else:
            log.warning("pre_market_prep: no briefing (Perplexity unavailable)")
    except Exception as exc:
        log.error("pre_market_prep: briefing failed", error=str(exc))


async def _run_discord_briefing(state) -> None:
    """08:45 — Post briefing summary to Discord."""
    from src.config import settings
    from src.alerts.discord import post_message

    briefing = getattr(state, "briefing", None)
    if not briefing:
        log.debug("pre_market_prep: no briefing to post")
        return

    msg_parts = [
        f"🌅 **Pre-market briefing** | {date.today()}",
        briefing.get("market_context", ""),
    ]
    if briefing.get("sector_watch"):
        msg_parts.append(f"📊 Sector: {briefing['sector_watch']}")
    if briefing.get("avoid_today"):
        msg_parts.append(f"⚠️ Avoid: {briefing['avoid_today']}")
    watchlist = [s.ticker for s in (state.watchlist or [])]
    if watchlist:
        msg_parts.append(f"👀 Watchlist: {', '.join(watchlist)}")

    try:
        await post_message(settings.DISCORD_WEBHOOK_URL, "\n".join(msg_parts))
        log.info("pre_market_prep: Discord briefing posted")
    except Exception as exc:
        log.warning("pre_market_prep: Discord post failed", error=str(exc))


async def _run_pre_exec_check(state) -> None:
    """09:00 — Verify data feeds and environment before open."""
    from src.config import settings
    import pathlib

    checks = {}

    # Kill switch must NOT be active
    ks_path = pathlib.Path(settings.KILL_SWITCH_FILE) if settings.KILL_SWITCH_FILE else None
    checks["kill_switch_clear"] = not (ks_path and ks_path.exists())

    # Account value loaded
    checks["account_value_ok"] = getattr(state, "account_value", 0) > 0

    # At least one ticker in universe
    checks["universe_loaded"] = len(getattr(state, "_float_map", {})) > 0

    failed = [k for k, v in checks.items() if not v]
    if failed:
        log.warning("pre_market_prep: pre-exec check FAILED", failed=failed)
    else:
        log.info("pre_market_prep: pre-exec checks passed", checks=list(checks.keys()))


async def _run_t5_alert(state) -> None:
    """09:25 — T-5 minutes alert."""
    from src.config import settings
    from src.alerts.discord import post_message

    watchlist = [s.ticker for s in (state.watchlist or [])]
    msg = (
        f"⏰ **T-5 minutes** | Market opens 09:30 ET | "
        f"Watchlist: {', '.join(watchlist) if watchlist else 'none'}"
    )
    try:
        await post_message(settings.DISCORD_WEBHOOK_URL, msg)
        log.info("pre_market_prep: T-5 alert sent")
    except Exception as exc:
        log.warning("pre_market_prep: T-5 alert failed", error=str(exc))


_TASK_FNS = {
    "universe_refresh":    _run_universe_refresh,
    "news_ingest":         _run_news_ingest,
    "watchlist_rank":      _run_watchlist_rank,
    "learnings_review":    _run_learnings_review,
    "perplexity_briefing": _run_perplexity_briefing,
    "discord_briefing":    _run_discord_briefing,
    "pre_exec_check":      _run_pre_exec_check,
    "t5_alert":            _run_t5_alert,
}


async def tick(state, now: datetime) -> None:
    """
    Called every 5 seconds while in PRE_MARKET_PREP mode.
    Dispatches scheduled tasks when their time window arrives.
    """
    now_et = now.astimezone(ET)
    task = _due_task(now_et)

    if task is None:
        log.debug("pre_market_prep idle", time=now_et.strftime("%H:%M"))
        return

    _tasks_fired.add(task)
    log.info("pre_market_prep: running task", task=task, time=now_et.strftime("%H:%M"))

    fn = _TASK_FNS.get(task)
    if fn:
        try:
            await fn(state)
        except Exception as exc:
            log.error("pre_market_prep: task failed", task=task, error=str(exc))
