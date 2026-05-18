from __future__ import annotations

"""
Finnhub REST API client — historical company news.
Free tier: 60 calls/min.
"""

import logging
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import List

import httpx

from src.config import settings

logger = logging.getLogger(__name__)

BASE_URL = "https://finnhub.io/api/v1"
_RATE_LIMIT_SLEEP = 1.1   # 60/min → 1 call/sec, +0.1 buffer
_last_call_time: float = 0.0


def _rate_limit() -> None:
    global _last_call_time
    elapsed = time.monotonic() - _last_call_time
    if elapsed < _RATE_LIMIT_SLEEP:
        time.sleep(_RATE_LIMIT_SLEEP - elapsed)
    _last_call_time = time.monotonic()


@dataclass
class RawNewsItem:
    ticker: str
    headline: str
    source: str
    published_at: datetime
    url: str
    summary: str


def get_company_news(ticker: str, from_date: date, to_date: date) -> List[RawNewsItem]:
    """Fetch historical company news for `ticker` between `from_date` and `to_date`."""
    if not settings.FINNHUB_API_KEY:
        logger.warning("FINNHUB_API_KEY not set — skipping news fetch")
        return []

    _rate_limit()
    try:
        # Key goes in header (X-Finnhub-Token) not URL param — keeps it out of logs
        r = httpx.get(
            f"{BASE_URL}/company-news",
            params={
                "symbol": ticker,
                "from": from_date.isoformat(),
                "to": to_date.isoformat(),
            },
            headers={"X-Finnhub-Token": settings.FINNHUB_API_KEY},
            timeout=15.0,
        )
        r.raise_for_status()
        items = r.json() or []
    except Exception as exc:
        logger.error("Finnhub news fetch failed for %s: %s", ticker, exc)
        return []

    results: List[RawNewsItem] = []
    for item in items:
        if not item.get("headline"):
            continue
        results.append(RawNewsItem(
            ticker=ticker,
            headline=item["headline"],
            source=item.get("source", ""),
            published_at=datetime.fromtimestamp(item.get("datetime", 0)),
            url=item.get("url", ""),
            summary=item.get("summary", ""),
        ))

    # Log connectivity heartbeat so the dashboard health panel can show last-seen time
    try:
        from src.storage.db import get_connection
        with get_connection() as _c:
            _c.execute(
                "INSERT INTO bot_events (occurred_at, event_type, message) VALUES (?, ?, ?)",
                (datetime.utcnow().isoformat(), "news_ingested",
                 f"{ticker}: {len(results)} articles"),
            )
    except Exception:
        pass  # non-fatal — never let logging break the data path

    return results
