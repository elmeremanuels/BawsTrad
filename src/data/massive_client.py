from __future__ import annotations

"""
Polygon (Massive) REST API client — historical bars + ticker reference.

Rate limit: 5 calls/min on free tier.
All results are cached in SQLite to avoid re-downloading.
"""

import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Iterator, List, Optional
from zoneinfo import ZoneInfo

import httpx

from src.config import settings
from src.storage.db import get_connection

logger = logging.getLogger(__name__)

BASE_URL = "https://api.polygon.io"
ET = ZoneInfo("America/New_York")

# Free tier: 5 calls/minute = 1 call per 12 seconds (conservative: 13s)
_RATE_LIMIT_SLEEP = 13.0
_last_call_time: float = 0.0


def _rate_limit() -> None:
    global _last_call_time
    elapsed = time.monotonic() - _last_call_time
    if elapsed < _RATE_LIMIT_SLEEP:
        wait = _RATE_LIMIT_SLEEP - elapsed
        logger.debug("Rate limiting: sleeping %.1fs", wait)
        time.sleep(wait)
    _last_call_time = time.monotonic()


@dataclass
class Bar:
    ticker: str
    bar_time: datetime   # UTC
    timeframe: str       # 'day' or '1min'
    open: float
    high: float
    low: float
    close: float
    volume: int
    vwap: Optional[float]

    @property
    def bar_time_et(self) -> datetime:
        return self.bar_time.astimezone(ET)


def _polygon_get(path: str, params: Optional[dict] = None) -> dict:
    """Single Polygon REST call with rate limiting and basic retry."""
    _rate_limit()
    p = params or {}
    p["apiKey"] = settings.MASSIVE_API_KEY
    url = f"{BASE_URL}{path}"

    for attempt in range(3):
        try:
            r = httpx.get(url, params=p, timeout=30.0)
            if r.status_code == 429:
                wait = 60.0 * (attempt + 1)
                logger.warning("Polygon 429: waiting %.0fs", wait)
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except httpx.HTTPError as exc:
            logger.error("Polygon HTTP error (attempt %d): %s", attempt + 1, exc)
            if attempt == 2:
                raise
            time.sleep(5.0)
    return {}


# ─── Cache helpers ────────────────────────────────────────────────────────────

def _bars_cached(ticker: str, timeframe: str, start: date, end: date) -> List[Bar]:
    """Return bars from cache if fully covered, else empty list."""
    start_iso = datetime(start.year, start.month, start.day, tzinfo=timezone.utc).isoformat()
    end_iso = datetime(end.year, end.month, end.day, 23, 59, 59, tzinfo=timezone.utc).isoformat()
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM price_bars WHERE ticker=? AND timeframe=? "
            "AND bar_time >= ? AND bar_time <= ? ORDER BY bar_time",
            (ticker, timeframe, start_iso, end_iso),
        ).fetchall()
    return [_row_to_bar(r) for r in rows]


def _row_to_bar(r) -> Bar:
    return Bar(
        ticker=r["ticker"],
        bar_time=datetime.fromisoformat(r["bar_time"]),
        timeframe=r["timeframe"],
        open=r["open"],
        high=r["high"],
        low=r["low"],
        close=r["close"],
        volume=r["volume"],
        vwap=r["vwap"],
    )


def _store_bars(bars: List[Bar]) -> None:
    with get_connection() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO price_bars "
            "(ticker, bar_time, timeframe, open, high, low, close, volume, vwap) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (b.ticker, b.bar_time.isoformat(), b.timeframe,
                 b.open, b.high, b.low, b.close, b.volume, b.vwap)
                for b in bars
            ],
        )


# ─── Public API ───────────────────────────────────────────────────────────────

def get_daily_bars(ticker: str, start: date, end: date, use_cache: bool = True) -> List[Bar]:
    """
    Fetch daily OHLCV bars for `ticker` between `start` and `end` (inclusive).
    Checks SQLite cache first; only hits Polygon if needed.
    """
    if use_cache:
        cached = _bars_cached(ticker, "day", start, end)
        if cached:
            logger.debug("Cache hit: %s daily bars %s→%s (%d rows)", ticker, start, end, len(cached))
            return cached

    logger.info("Fetching daily bars: %s %s→%s", ticker, start, end)
    bars: List[Bar] = []
    path = f"/v2/aggs/ticker/{ticker}/range/1/day/{start}/{end}"
    params = {"adjusted": "true", "sort": "asc", "limit": 50000}

    while path:
        data = _polygon_get(path, params)
        results = data.get("results") or []
        for r in results:
            ts = datetime.fromtimestamp(r["t"] / 1000, tz=timezone.utc)
            bars.append(Bar(
                ticker=ticker, bar_time=ts, timeframe="day",
                open=r["o"], high=r["h"], low=r["l"], close=r["c"],
                volume=int(r["v"]), vwap=r.get("vw"),
            ))
        # Pagination
        path = None
        if "next_url" in data:
            # next_url is a full URL; extract path+params
            next_url = data["next_url"]
            logger.debug("Paginating: %s", next_url)
            # Make the next call directly
            _rate_limit()
            r2 = httpx.get(next_url + f"&apiKey={settings.MASSIVE_API_KEY}", timeout=30.0)
            r2.raise_for_status()
            data = r2.json()
            results = data.get("results") or []
            for r in results:
                ts = datetime.fromtimestamp(r["t"] / 1000, tz=timezone.utc)
                bars.append(Bar(
                    ticker=ticker, bar_time=ts, timeframe="day",
                    open=r["o"], high=r["h"], low=r["l"], close=r["c"],
                    volume=int(r["v"]), vwap=r.get("vw"),
                ))

    if bars:
        _store_bars(bars)
    logger.info("Fetched %d daily bars for %s", len(bars), ticker)
    return bars


def get_intraday_bars(ticker: str, day: date, use_cache: bool = True) -> List[Bar]:
    """
    Fetch 1-minute bars for `ticker` on `day`.
    Returns bars from 4:00 AM to 8:00 PM ET (full extended hours session).
    """
    if use_cache:
        cached = _bars_cached(ticker, "1min", day, day)
        if cached:
            logger.debug("Cache hit: %s 1min bars %s (%d rows)", ticker, day, len(cached))
            return cached

    logger.info("Fetching 1min bars: %s %s", ticker, day)
    path = f"/v2/aggs/ticker/{ticker}/range/1/minute/{day}/{day}"
    params = {"adjusted": "true", "sort": "asc", "limit": 50000, "extended_hours": "true"}

    data = _polygon_get(path, params)
    results = data.get("results") or []
    bars: List[Bar] = []
    for r in results:
        ts = datetime.fromtimestamp(r["t"] / 1000, tz=timezone.utc)
        bars.append(Bar(
            ticker=ticker, bar_time=ts, timeframe="1min",
            open=r["o"], high=r["h"], low=r["l"], close=r["c"],
            volume=int(r["v"]), vwap=r.get("vw"),
        ))

    if bars:
        _store_bars(bars)
    logger.info("Fetched %d 1min bars for %s on %s", len(bars), ticker, day)
    return bars


def get_ticker_reference(ticker: str, use_cache: bool = True) -> Optional[dict]:
    """Fetch company info (shares outstanding, name, sector)."""
    if use_cache:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM ticker_reference WHERE ticker=?", (ticker,)
            ).fetchone()
            if row:
                return dict(row)

    logger.info("Fetching reference data for %s", ticker)
    data = _polygon_get(f"/v3/reference/tickers/{ticker}")
    result = data.get("results", {})
    if not result:
        return None

    ref = {
        "ticker": ticker,
        "fetched_at": datetime.utcnow().isoformat(),
        "shares_outstanding": result.get("share_class_shares_outstanding"),
        "company_name": result.get("name"),
        "sector": result.get("sic_description"),
    }
    with get_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO ticker_reference "
            "(ticker, fetched_at, shares_outstanding, company_name, sector) "
            "VALUES (:ticker, :fetched_at, :shares_outstanding, :company_name, :sector)",
            ref,
        )
    return ref


def compute_avg_volume(daily_bars: List[Bar], window: int = 30) -> float:
    """30-day average volume from daily bars (excluding the most recent day)."""
    if len(daily_bars) < 2:
        return 0.0
    # Exclude the last bar (today), use up to `window` prior days
    prior = daily_bars[:-1][-window:]
    volumes = [b.volume for b in prior]
    return sum(volumes) / len(volumes) if volumes else 0.0
