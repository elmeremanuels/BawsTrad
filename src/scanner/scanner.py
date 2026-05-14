from __future__ import annotations

"""
Historical scanner — identifies watchlist candidates from daily bars.
Used by the backtest script (Phase 1).
In Phase 2, a real-time version will use WebSocket data instead.
"""

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple

from src.data.massive_client import Bar, compute_avg_volume
from src.news.classifier import classify_headline
from src.scanner.criteria import NewsCatalyst, TickerSnapshot, passes_stock_selection, quality_score

logger = logging.getLogger(__name__)


@dataclass
class UniverseTicker:
    ticker: str
    float_shares: int        # From CSV (or Polygon reference)
    sector: str = ""


@dataclass
class DayResult:
    trading_day: date
    watchlist: List[TickerSnapshot]  # Passed all 5 criteria, sorted by quality


def build_snapshot_from_daily_bars(
    ticker: str,
    float_shares: int,
    today_bar: Bar,
    prior_bars: List[Bar],  # At least 30 days before today
    news_items: Optional[list] = None,
) -> Optional[TickerSnapshot]:
    """
    Build a TickerSnapshot from daily bars.

    NOTE: In backtest mode, 'price' = today's open and 'gap_pct' is
    computed vs previous close. Relative volume uses the full day's
    volume (slightly forward-looking — acceptable for phase 1).
    """
    if not prior_bars:
        return None

    prev_close = prior_bars[-1].close
    if prev_close <= 0:
        return None

    price = today_bar.open
    gap_pct = (today_bar.open - prev_close) / prev_close * 100.0
    # Percent change today uses end-of-day close vs prev close for backtest
    pct_change = (today_bar.close - prev_close) / prev_close * 100.0

    # Relative volume: today's total vs 30-day avg
    avg_vol = compute_avg_volume(prior_bars)
    rel_vol = today_bar.volume / avg_vol if avg_vol > 0 else 0.0

    # Yesterday's change (for continuation setup check)
    if len(prior_bars) >= 2:
        yday_close = prior_bars[-2].close
        yday_change = (prior_bars[-1].close - yday_close) / yday_close * 100.0
    else:
        yday_change = 0.0

    # Best news catalyst for this ticker/day
    catalyst: Optional[NewsCatalyst] = None
    if news_items:
        best_tier = "C"
        for item in news_items:
            tier, category = classify_headline(item.headline, ticker)
            if tier == "A" and best_tier != "A":
                catalyst = NewsCatalyst(tier="A", category=category, headline=item.headline)
                best_tier = "A"
                break
            elif tier == "B" and best_tier == "C":
                catalyst = NewsCatalyst(tier="B", category=category, headline=item.headline)
                best_tier = "B"

    return TickerSnapshot(
        ticker=ticker,
        price=price,
        percent_change_today=gap_pct,   # Use gap vs prev close as "change today" for morning scan
        relative_volume=rel_vol,
        float_shares=float_shares,
        news_catalyst=catalyst,
        yesterday_percent_change=yday_change,
        yesterday_close=prior_bars[-1].close,
    )


def scan_day(
    trading_day: date,
    universe: List[UniverseTicker],
    all_daily_bars: Dict[str, List[Bar]],       # ticker -> sorted list of daily bars
    news_by_ticker: Dict[str, list],             # ticker -> list of RawNewsItem
    top_n: int = 5,
) -> DayResult:
    """
    Apply the 5-criteria filter to all universe tickers for `trading_day`.
    Returns watchlist sorted by quality score (best first), capped at top_n.
    """
    candidates: List[Tuple[float, TickerSnapshot]] = []

    for u in universe:
        bars = all_daily_bars.get(u.ticker, [])
        # Find today's bar and all prior bars
        prior: List[Bar] = []
        today_bar: Optional[Bar] = None

        for bar in bars:
            bar_date = bar.bar_time.date()
            if bar_date < trading_day:
                prior.append(bar)
            elif bar_date == trading_day:
                today_bar = bar

        if today_bar is None or len(prior) < 5:
            continue   # Not enough data

        news = news_by_ticker.get(u.ticker, [])
        snap = build_snapshot_from_daily_bars(
            ticker=u.ticker,
            float_shares=u.float_shares,
            today_bar=today_bar,
            prior_bars=prior,
            news_items=news,
        )

        if snap is None:
            continue

        if passes_stock_selection(snap):
            score = quality_score(snap)
            candidates.append((score, snap))
            logger.debug("%s passed scan on %s (score=%.1f)", u.ticker, trading_day, score)
        else:
            logger.debug("%s failed scan on %s", u.ticker, trading_day)

    candidates.sort(key=lambda x: x[0], reverse=True)
    watchlist = [snap for _, snap in candidates[:top_n]]

    logger.info("Day %s: %d/%d passed, watchlist=%s",
                trading_day, len(candidates), len(universe),
                [s.ticker for s in watchlist])

    return DayResult(trading_day=trading_day, watchlist=watchlist)
