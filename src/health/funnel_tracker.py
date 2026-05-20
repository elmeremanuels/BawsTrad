from __future__ import annotations

"""
Funnel Tracker — System 5 (PROBLEEM 5).

Logs how many tickers survive each stage of the universe-to-trade pipeline.
A "dead funnel" (stage = 0 for 5+ consecutive days) triggers a Discord alert
and kicks off auto-investigation so blind spots never hide in silence.

Stages (in order):
    base_universe   — all active US equities on NYSE/NASDAQ/AMEX/ARCA
    wave1_price_gap — price $1-$20, gap ≥ 3%
    wave2_volume    — premarket volume above minimum
    wave3_float     — float < 30M shares (Finnhub cache)
    wave4_news      — Tier A/B news catalyst present
    watchlist_active — made it onto the intraday watchlist
    setup_detected  — bull-flag / micro-pullback pattern fired
    rr_acceptable   — reward:risk ≥ 2R
    mode_allows     — state machine in ACTIVE_TRADING
    cost_acceptable — edge > 1.5× predicted cost  [post-Sprint-2]
    entries_placed  — order actually submitted

Usage:
    from src.health.funnel_tracker import log_funnel, get_today_funnel
    log_funnel('wave1_price_gap', 42)
    log_funnel('watchlist_active', 3)
"""

import logging
from datetime import date, datetime
from typing import Dict, List, Optional

from src.storage.db import get_connection

logger = logging.getLogger(__name__)

STAGES: List[str] = [
    "base_universe",
    "wave1_price_gap",
    "wave2_volume",
    "wave3_float",
    "wave4_news",
    "watchlist_active",
    "setup_detected",
    "rr_acceptable",
    "mode_allows",
    "cost_acceptable",
    "entries_placed",
]

# Alert if this many consecutive days of count=0 at a given stage
DEAD_FUNNEL_DAYS = 5


def log_funnel(stage: str, count: int, for_date: Optional[date] = None) -> None:
    """
    Upsert a funnel count for today (or `for_date`).
    Silently swallows errors — funnel tracking must never break the main pipeline.
    """
    day_str = (for_date or date.today()).isoformat()
    recorded_at = datetime.utcnow().isoformat()
    try:
        with get_connection() as conn:
            conn.execute(
                """
                INSERT INTO funnel_daily (date, stage, count, recorded_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(date, stage) DO UPDATE SET
                    count       = excluded.count,
                    recorded_at = excluded.recorded_at
                """,
                (day_str, stage, count, recorded_at),
            )
    except Exception as exc:
        logger.warning("funnel_tracker.log_funnel failed: %s", exc)


def get_today_funnel() -> Dict[str, int]:
    """Return {stage: count} for today. Missing stages return 0."""
    day_str = date.today().isoformat()
    result = {s: 0 for s in STAGES}
    try:
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT stage, count FROM funnel_daily WHERE date = ?",
                (day_str,),
            ).fetchall()
        for row in rows:
            result[row["stage"]] = row["count"]
    except Exception as exc:
        logger.warning("funnel_tracker.get_today_funnel failed: %s", exc)
    return result


def get_funnel_history(days: int = 30) -> List[Dict]:
    """Return last N days of funnel data as list of {date, stage, count}."""
    try:
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT date, stage, count FROM funnel_daily
                WHERE date >= date('now', ?)
                ORDER BY date DESC, stage
                """,
                (f"-{days} days",),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception as exc:
        logger.warning("funnel_tracker.get_funnel_history failed: %s", exc)
        return []


def detect_dead_funnels(post_to_discord: bool = True) -> List[str]:
    """
    Check every stage. If count=0 for DEAD_FUNNEL_DAYS consecutive days,
    it's a dead funnel — alert to Discord and return the list of dead stages.

    Safe to call daily (e.g., from pre_market_prep or a cron).
    """
    dead: List[str] = []

    try:
        with get_connection() as conn:
            for stage in STAGES:
                rows = conn.execute(
                    """
                    SELECT count FROM funnel_daily
                    WHERE stage = ? AND date >= date('now', ?)
                    ORDER BY date DESC
                    """,
                    (stage, f"-{DEAD_FUNNEL_DAYS} days"),
                ).fetchall()

                if len(rows) >= DEAD_FUNNEL_DAYS and all(r["count"] == 0 for r in rows):
                    dead.append(stage)
                    logger.warning("funnel_tracker: DEAD FUNNEL detected at stage=%s", stage)
    except Exception as exc:
        logger.warning("funnel_tracker.detect_dead_funnels query failed: %s", exc)
        return []

    if dead and post_to_discord:
        _alert_dead_funnels(dead)

    return dead


def _alert_dead_funnels(dead_stages: List[str]) -> None:
    try:
        import asyncio
        from src.alerts.discord import post_message
        from src.config import settings

        lines = [
            f"🚨 **Dead Funnel Alert** — {len(dead_stages)} stage(s) at 0 for {DEAD_FUNNEL_DAYS}+ days:",
        ]
        for stage in dead_stages:
            causes = _DEAD_FUNNEL_LIKELY_CAUSES.get(stage, "upstream feeds or filter mis-config")
            lines.append(f"  • `{stage}` — likely: {causes}")
        lines.append("Run `scripts/diagnose_scanner.py` for details.")

        msg = "\n".join(lines)
        asyncio.run(post_message(settings.DISCORD_WEBHOOK_URL, msg))
    except Exception as exc:
        logger.warning("funnel_tracker: Discord alert failed: %s", exc)


def format_funnel_report(for_date: Optional[date] = None) -> str:
    """
    Return a human-readable funnel report for today (or `for_date`).
    Used by pre-market Discord briefing.
    """
    day_str = (for_date or date.today()).isoformat()
    try:
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT stage, count FROM funnel_daily WHERE date = ? ORDER BY rowid",
                (day_str,),
            ).fetchall()
        if not rows:
            return f"📊 Funnel ({day_str}): no data yet"

        lines = [f"📊 **Funnel** {day_str}"]
        prev_count: Optional[int] = None
        for row in rows:
            stage = row["stage"]
            count = row["count"]
            drop = ""
            if prev_count is not None and prev_count > 0:
                pct = (1 - count / prev_count) * 100
                drop = f"  (↓{pct:.0f}%)" if pct > 0 else ""
            lines.append(f"  `{stage:<22}` {count:>5}{drop}")
            prev_count = count

        return "\n".join(lines)
    except Exception as exc:
        logger.warning("funnel_tracker.format_funnel_report failed: %s", exc)
        return "📊 Funnel: error generating report"


_DEAD_FUNNEL_LIKELY_CAUSES: Dict[str, str] = {
    "base_universe":    "Alpaca assets API down or base_universe.csv missing",
    "wave1_price_gap":  "No stocks in $1-$20 range gapping ≥3% — low-vol market?",
    "wave2_volume":     "Pre-market volume feed broken or threshold too high",
    "wave3_float":      "Finnhub API down or float filter too tight",
    "wave4_news":       "Finnhub news feed down, Groq classifier failing, or real quiet day",
    "watchlist_active": "Scanner not running, or all 5 criteria never align",
    "setup_detected":   "No bull-flag or micro-pullback patterns in bar data",
    "rr_acceptable":    "Stops too tight or volatility too low for 2R targets",
    "mode_allows":      "State machine stuck outside ACTIVE_TRADING",
    "cost_acceptable":  "Spread filter blocking all trades (post-Sprint-2 feature)",
    "entries_placed":   "Risk limits hit, daily max trades reached, or order errors",
}
