from __future__ import annotations

"""
Stock selection criteria — Warrior Trading methodology.
All criteria must pass (AND logic).
Pure Python — no AI, no external calls.

Config flag scanner.require_news_catalyst (default: true) controls whether
a Tier A/B news catalyst is mandatory. Set to false as a fallback when
Finnhub is unreliable, to avoid blocking all trades on a data feed outage.
"""

from dataclasses import dataclass
from typing import Optional

import yaml
from pathlib import Path


def _require_catalyst() -> bool:
    """Read require_news_catalyst from config.yaml. Defaults to True."""
    try:
        cfg_path = Path(__file__).parent.parent.parent / "config.yaml"
        with open(cfg_path) as f:
            cfg = yaml.safe_load(f) or {}
        return bool(cfg.get("scanner", {}).get("require_news_catalyst", True))
    except Exception:
        return True  # Safe default — always require news if config unreadable


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
    """
    Return True only if ALL criteria are met.

    Criteria 4 (news catalyst) can be relaxed via config:
      scanner.require_news_catalyst: false
    Use this as a fallback when Finnhub is down to avoid blocking all trades
    on a data feed outage. Still logs the absence of a catalyst as a warning.
    """
    # 1. Price range $1–$20
    if not (1.00 <= snap.price <= 20.00):
        return False
    # 2. Up at least 10% on the day (or continuation setup)
    if not (snap.percent_change_today >= 10.0 or is_continuation_setup(snap)):
        return False
    # 3. Relative volume >= 5x
    if snap.relative_volume < 5.0:
        return False
    # 4. News catalyst (optional via config)
    if _require_catalyst():
        if snap.news_catalyst is None or snap.news_catalyst.tier not in ("A", "B"):
            return False
    # 5. Float < 20M shares
    if snap.float_shares >= 20_000_000:
        return False
    return True


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
