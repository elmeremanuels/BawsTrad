from __future__ import annotations

"""
News ingestion — polls Finnhub REST API, deduplicates, stores in SQLite.
Used in Phase 2 (paper trading). Phase 1 used the same finnhub_client directly.
"""

import hashlib
import logging
from datetime import date, datetime, timedelta
from typing import List, Set
from uuid import uuid4

from src.data.finnhub_client import get_company_news
from src.llm.cheap import classify_with_llm  # Groq LLM with keyword fallback
from src.storage.db import get_connection

logger = logging.getLogger(__name__)


def _headline_hash(headline: str, ticker: str) -> str:
    return hashlib.sha256(f"{ticker}:{headline.lower().strip()}".encode()).hexdigest()[:16]


def _already_stored(headline_hash: str) -> bool:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM news_items WHERE id LIKE ? LIMIT 1",
            (f"%{headline_hash}%",),
        ).fetchone()
    return row is not None


def ingest_news_for_ticker(ticker: str) -> List[dict]:
    """
    Poll Finnhub for today's news, classify, deduplicate, store in DB.
    Returns list of newly stored news items.
    """
    today = date.today()
    raw_items = get_company_news(ticker, today - timedelta(days=1), today)
    new_items: List[dict] = []

    for item in raw_items:
        h = _headline_hash(item.headline, ticker)
        if _already_stored(h):
            continue

        tier, category = classify_with_llm(item.headline, ticker)
        item_id = f"{h}-{uuid4().hex[:8]}"
        record = {
            "id":         item_id,
            "fetched_at": datetime.utcnow().isoformat(),
            "ticker":     ticker,
            "headline":   item.headline,
            "source":     item.source,
            "tier":       tier,
            "category":   category,
            "sentiment":  0.0,
            "reasoning":  "",
        }
        with get_connection() as conn:
            conn.execute("""
                INSERT OR IGNORE INTO news_items
                (id, fetched_at, ticker, headline, source, tier, category, sentiment, reasoning)
                VALUES (:id, :fetched_at, :ticker, :headline, :source, :tier, :category,
                        :sentiment, :reasoning)
            """, record)
        new_items.append(record)
        logger.info("News [%s] %s: %s — %s", tier, ticker, item.headline[:60], category)

    return new_items


def get_best_catalyst_today(ticker: str) -> dict | None:
    """Return the highest-tier news item for ticker today."""
    today = date.today().isoformat()
    with get_connection() as conn:
        for tier in ("A", "B"):
            row = conn.execute("""
                SELECT * FROM news_items
                WHERE ticker=? AND tier=? AND fetched_at >= ?
                ORDER BY fetched_at DESC LIMIT 1
            """, (ticker, tier, today)).fetchone()
            if row:
                return dict(row)
    return None
