from __future__ import annotations

import pytest
from src.scanner.criteria import (
    NewsCatalyst, TickerSnapshot, is_continuation_setup,
    passes_stock_selection, quality_score
)


def _snap(**kwargs) -> TickerSnapshot:
    defaults = dict(
        ticker="TEST",
        price=5.50,
        percent_change_today=25.0,
        relative_volume=8.0,
        float_shares=5_000_000,
        news_catalyst=NewsCatalyst(tier="A", category="FDA_approval", headline="FDA approves TEST drug"),
        yesterday_percent_change=0.0,
        yesterday_close=5.50,
    )
    defaults.update(kwargs)
    return TickerSnapshot(**defaults)


def test_passes_all_5_criteria():
    assert passes_stock_selection(_snap()) is True


def test_fails_price_too_low():
    assert passes_stock_selection(_snap(price=0.80)) is False


def test_fails_price_too_high():
    assert passes_stock_selection(_snap(price=21.00)) is False


def test_fails_gap_too_small():
    assert passes_stock_selection(_snap(percent_change_today=8.0)) is False


def test_fails_rel_vol_too_low():
    assert passes_stock_selection(_snap(relative_volume=4.9)) is False


def test_fails_no_news():
    assert passes_stock_selection(_snap(news_catalyst=None)) is False


def test_fails_tier_c_news():
    c_news = NewsCatalyst(tier="C", category="dilution", headline="Dilution offering announced")
    assert passes_stock_selection(_snap(news_catalyst=c_news)) is False


def test_fails_float_too_high():
    assert passes_stock_selection(_snap(float_shares=20_000_001)) is False


def test_continuation_setup_overrides_gap_check():
    """A stock that ran 60% yesterday can pass even with <10% gap today."""
    snap = _snap(
        percent_change_today=5.0,
        yesterday_percent_change=60.0,
        yesterday_close=4.00,
        price=3.50,  # 87.5% of yesterday's close = holding up
    )
    assert is_continuation_setup(snap) is True
    assert passes_stock_selection(snap) is True


def test_continuation_fails_if_price_dropped_too_much():
    snap = _snap(
        percent_change_today=5.0,
        yesterday_percent_change=60.0,
        yesterday_close=4.00,
        price=3.00,  # 75% = too much retrace
    )
    assert is_continuation_setup(snap) is False


def test_quality_score_higher_for_lower_float():
    high_float = _snap(float_shares=15_000_000)
    low_float = _snap(float_shares=4_000_000)
    assert quality_score(low_float) > quality_score(high_float)


def test_quality_score_higher_for_tier_a():
    tier_a = _snap(news_catalyst=NewsCatalyst(tier="A", category="FDA_approval", headline="FDA approves"))
    tier_b = _snap(news_catalyst=NewsCatalyst(tier="B", category="upgrade", headline="Analyst upgrade"))
    assert quality_score(tier_a) > quality_score(tier_b)
