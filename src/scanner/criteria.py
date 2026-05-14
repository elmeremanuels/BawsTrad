from __future__ import annotations

"""
Stock selection criteria — Warrior Trading methodology.
All 5 criteria must pass (AND logic).
Pure Python — no AI, no external calls.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class NewsCatalyst:
    tier: str        # A / B / C
    category: str
    headline: str


@dataclass
class TickerSnapshot:
    ticker: str
    price: float
    percent_change_today: float     # Gap % at time of scan (vs prev close)
    relative_volume: float          # today_vol / avg_30d_vol
    float_shares: int               # Shares available for trading
    news_catalyst: Optional[NewsCatalyst]
    yesterday_percent_change: float = 0.0
    yesterday_close: float = 0.0


# ─── 5-criteria filter ────────────────────────────────────────────────────────

def passes_stock_selection(snap: TickerSnapshot) -> bool:
    """Return True only if ALL 5 criteria are met."""
    return all([
        # 1. Price range $1–$20
        1.00 <= snap.price <= 20.00,
        # 2. Up at least 10% on the day (or continuation setup)
        snap.percent_change_today >= 10.0 or is_continuation_setup(snap),
        # 3. Relative volume >= 5x
        snap.relative_volume >= 5.0,
        # 4. News catalyst present and tier A or B
        snap.news_catalyst is not None
        and snap.news_catalyst.tier in ("A", "B"),
        # 5. Float < 20M shares
        snap.float_shares < 20_000_000,
    ])


def is_continuation_setup(snap: TickerSnapshot) -> bool:
    """
    Stock ran hard yesterday (>50%) and is holding those levels today.
    Valid even if today's gap % is under 10%.
    """
    return (
        snap.yesterday_percent_change >= 50.0
        and snap.price >= snap.yesterday_close * 0.85
    )


def quality_score(snap: TickerSnapshot) -> float:
    """
    Rank candidates within the watchlist.
    Higher = better. Top 3-5 tickers are actively monitored.
    """
    score = snap.percent_change_today * snap.relative_volume

    if snap.float_shares < 5_000_000:
        score *= 1.5  # Lower float → bigger imbalance → preferred

    if snap.news_catalyst and snap.news_catalyst.tier == "A":
        score *= 1.3

    return score


def passes_gap_and_go(snap: TickerSnapshot) -> bool:
    """Gap and Go strategy — slightly different (wider float, bigger gap pref)."""
    return all([
        1.50 <= snap.price <= 20.00,
        snap.percent_change_today >= 5.0,
        snap.float_shares < 50_000_000,
        snap.news_catalyst is not None,
    ])
