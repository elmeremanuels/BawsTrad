from __future__ import annotations

"""
Float share cache — Finnhub /stock/profile2 → ticker_reference SQLite cache.
Used by the scanner to evaluate float criterion (C5: float < 20M shares).

Finnhub returns shareOutstanding in millions — multiply by 1,000,000 for our purposes.
Cache TTL: 7 days (float rarely changes significantly week-to-week).
Falls back to DEFAULT_FLOAT on error — conservative enough to not block trades.
"""

import logging
import time
from datetime import datetime, timedelta
from typing import Dict, List

import httpx

from src.config import settings
from src.storage.db import get_connection

logger = logging.getLogger(__name__)

BASE_URL = "https://finnhub.io/api/v1"
CACHE_TTL_DAYS = 7
DEFAULT_FLOAT = 15_000_000
_RATE_LIMIT_SLEEP = 1.1  # 60/min → 1 call/sec, +0.1 buffer

_last_call_time: float = 0.0


def _rate_limit() -> None:
    global _last_call_time
    elapsed = time.monotonic() - _last_call_time
    if elapsed < _RATE_LIMIT_SLEEP:
        time.sleep(_RATE_LIMIT_SLEEP - elapsed)
    _last_call_time = time.monotonic()


def _cache_cutoff() -> str:
    """Return ISO timestamp for (now - TTL). Rows older than this are stale."""
    cutoff = datetime.utcnow() - timedelta(days=CACHE_TTL_DAYS)
    return cutoff.isoformat()


def _fetch_from_finnhub(ticker: str) -> Dict:
    """
    Call Finnhub /stock/profile2 for the given ticker.
    Returns the parsed JSON dict on success, empty dict on failure.
    Rate-limited to 1 call/sec.
    """
    if not settings.FINNHUB_API_KEY:
        logger.warning("FINNHUB_API_KEY not set — cannot fetch float for %s", ticker)
        return {}

    _rate_limit()
    try:
        r = httpx.get(
            f"{BASE_URL}/stock/profile2",
            params={"symbol": ticker},
            headers={"X-Finnhub-Token": settings.FINNHUB_API_KEY},
            timeout=15.0,
        )
        r.raise_for_status()
        return r.json() or {}
    except Exception as exc:
        logger.warning("Finnhub profile2 fetch failed for %s: %s", ticker, exc)
        return {}


def _upsert_ticker_reference(
    ticker: str,
    shares_outstanding: int,
    company_name: str,
    sector: str,
) -> None:
    """Upsert a row into ticker_reference with the current timestamp."""
    fetched_at = datetime.utcnow().isoformat()
    try:
        with get_connection() as conn:
            conn.execute(
                """
                INSERT INTO ticker_reference (ticker, fetched_at, shares_outstanding, company_name, sector)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(ticker) DO UPDATE SET
                    fetched_at         = excluded.fetched_at,
                    shares_outstanding = excluded.shares_outstanding,
                    company_name       = excluded.company_name,
                    sector             = excluded.sector
                """,
                (ticker, fetched_at, shares_outstanding, company_name, sector),
            )
    except Exception as exc:
        logger.warning("Failed to upsert ticker_reference for %s: %s", ticker, exc)


def get_float_shares(ticker: str) -> int:
    """
    Return float shares for `ticker`.

    1. Checks ticker_reference for a fresh (< 7-day-old) row.
    2. On cache miss or stale row, calls Finnhub /stock/profile2.
    3. On any error, returns DEFAULT_FLOAT (15M — conservative, won't block trades).
    """
    ticker = ticker.upper().strip()
    cutoff = _cache_cutoff()

    # ── 1. Cache lookup ────────────────────────────────────────────────────────
    try:
        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT shares_outstanding FROM ticker_reference
                WHERE ticker = ? AND fetched_at >= ?
                """,
                (ticker, cutoff),
            ).fetchone()
        if row and row["shares_outstanding"] and row["shares_outstanding"] > 0:
            return int(row["shares_outstanding"])
    except Exception as exc:
        logger.warning("ticker_reference cache lookup failed for %s: %s", ticker, exc)

    # ── 2. Finnhub fetch ───────────────────────────────────────────────────────
    profile = _fetch_from_finnhub(ticker)
    if not profile:
        logger.warning("No Finnhub profile for %s — using DEFAULT_FLOAT", ticker)
        return DEFAULT_FLOAT

    raw_shares = profile.get("shareOutstanding")  # in millions from Finnhub
    if not raw_shares or float(raw_shares) <= 0:
        logger.warning("shareOutstanding missing/zero for %s — using DEFAULT_FLOAT", ticker)
        return DEFAULT_FLOAT

    shares_outstanding = int(float(raw_shares) * 1_000_000)
    company_name = profile.get("name", "")
    sector = profile.get("finnhubIndustry", "")

    _upsert_ticker_reference(ticker, shares_outstanding, company_name, sector)

    return shares_outstanding


def prefetch_floats(tickers: List[str]) -> Dict[str, int]:
    """
    Batch-fetch float shares for a list of tickers.
    Returns {ticker: float_shares}.
    Rate-limited: calls get_float_shares() sequentially (~1/sec for cache misses).
    """
    result: Dict[str, int] = {}
    for ticker in tickers:
        result[ticker] = get_float_shares(ticker)
    return result
