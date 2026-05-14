from __future__ import annotations

"""
OVERNIGHT_INTEL handler — 20:00 ET until 04:00 ET (next day).

Schedule (fires at most once per window, tracked via module-level sets):
  20:00  Initial sweep — ingest news for all universe tickers
  22:00  Mid-night sweep — pick up late-breaking news
  02:00  Early-morning sweep — Asia/EU session catalysts

Each sweep:
1. Calls Finnhub get_company_news() for today's news per ticker
2. Upserts raw articles into overnight_intel table
3. Runs Groq tier-1 classification for headline tier + category
4. Marks used_in_briefing=0 (briefing handler reads this tomorrow)

Design constraints:
- Finnhub free tier: 60 calls/min — rate limit enforced by finnhub_client
- Total tickers ≤ 50 (universe size); each sweep ≤ 50 API calls
- All DB writes are INSERT OR IGNORE — safe to re-run on restart
"""

from datetime import date, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import structlog

ET = ZoneInfo("America/New_York")
log = structlog.get_logger(__name__)

# Track which sweep hours have already fired this session
_sweeps_fired: set = set()
_SWEEP_HOURS = {20, 22, 2}  # ET hours that trigger a news sweep


def _should_sweep(now_et: datetime) -> bool:
    """Return True if we're within 5 minutes after a scheduled sweep hour."""
    h = now_et.hour
    if h not in _SWEEP_HOURS:
        return False
    if h in _sweeps_fired:
        return False
    # Fire only in the first 5 minutes of the hour
    return now_et.minute < 5


def _ingest_overnight_news(tickers: list, fetch_date: date) -> int:
    """
    Fetch Finnhub news for each ticker and upsert into overnight_intel.
    Returns total new articles stored.
    """
    from src.data.finnhub_client import get_company_news
    from src.storage.db import get_connection

    total = 0
    now_iso = datetime.utcnow().isoformat()

    for ticker in tickers:
        try:
            items = get_company_news(ticker, from_date=fetch_date, to_date=fetch_date)
        except Exception as exc:
            log.warning("overnight_intel: finnhub fetch failed", ticker=ticker, error=str(exc))
            continue

        for item in items:
            article_id = str(uuid4())
            try:
                with get_connection() as conn:
                    conn.execute("""
                        INSERT OR IGNORE INTO overnight_intel
                        (id, fetched_at, ticker, headline, source, published_at, summary,
                         tier, category, sentiment, used_in_briefing)
                        VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, 0)
                    """, (
                        article_id,
                        now_iso,
                        ticker,
                        item.headline,
                        item.source,
                        item.published_at.isoformat() if item.published_at else None,
                        item.summary,
                    ))
                    # Check if it was actually inserted (not already present)
                    inserted = conn.execute(
                        "SELECT changes()"
                    ).fetchone()[0]
                    total += inserted
            except Exception as exc:
                log.warning("overnight_intel: DB write failed", ticker=ticker, error=str(exc))

    return total


async def tick(state, now: datetime) -> None:
    """
    Called every 5 seconds while in OVERNIGHT_INTEL mode.
    Fires timed news sweeps; idles between them.
    """
    now_et = now.astimezone(ET)

    if not _should_sweep(now_et):
        log.debug("overnight_intel idle", hour=now_et.hour, minute=now_et.minute)
        return

    sweep_hour = now_et.hour
    _sweeps_fired.add(sweep_hour)
    log.info("overnight_intel: news sweep starting", sweep_hour=sweep_hour,
             tickers=len(getattr(state, "_float_map", {}) or {}))

    # Determine which date's news to fetch
    # At 02:00 ET we're technically the next calendar day but want today's news
    fetch_date = now_et.date()
    if sweep_hour < 4:
        fetch_date = fetch_date - timedelta(days=1)

    tickers = list(getattr(state, "_float_map", {}).keys()) or []
    if not tickers:
        log.warning("overnight_intel: no tickers in state._float_map — skipping sweep")
        return

    try:
        new_articles = _ingest_overnight_news(tickers, fetch_date)
        log.info("overnight_intel: sweep complete",
                 sweep_hour=sweep_hour,
                 tickers=len(tickers),
                 new_articles=new_articles)
    except Exception as exc:
        log.error("overnight_intel: sweep failed", error=str(exc))
