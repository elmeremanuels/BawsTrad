from __future__ import annotations

from datetime import datetime, timezone

import pytest
from src.patterns.candles import Candle
from src.patterns.detector import detect_bull_flag, detect_micro_pullback, detect_flat_top


def _candle(open_, close, high=None, low=None, volume=100_000, t: int = 0) -> Candle:
    h = high if high is not None else max(open_, close) * 1.005
    l = low if low is not None else min(open_, close) * 0.995
    dt = datetime(2024, 1, 2, 9, 30 + t, tzinfo=timezone.utc)
    return Candle(bar_time=dt, open=open_, high=h, low=l, close=close, volume=volume)


def _green(price_start, price_end, vol=150_000, t=0, high=None, low=None):
    return _candle(price_start, price_end, high=high, low=low, volume=vol, t=t)


def _red(price_start, price_end, vol=80_000, t=0, high=None, low=None):
    return _candle(price_start, price_end, high=high, low=low, volume=vol, t=t)


# ── Bull flag ────────────────────────────────────────────────────────────────

def test_bull_flag_detected():
    """Classic bull flag: 3 strong green candles then 2 consolidating."""
    candles = [
        # Pole: 3 strong green candles
        _green(5.00, 5.50, vol=200_000, t=0),
        _green(5.50, 6.00, vol=180_000, t=1),
        _green(6.00, 6.60, vol=170_000, t=2),
        # Flag: 2 small candles, lower volume
        _green(6.55, 6.62, vol=50_000, t=3),
        _red(6.62, 6.55, vol=40_000, t=4),
    ]
    sig = detect_bull_flag(candles, idx=4)
    assert sig is not None
    assert sig.pattern == "bull_flag"
    assert sig.entry_trigger > sig.stop_price


def test_bull_flag_not_detected_on_deep_retrace():
    """Flag retraces >50% of pole — not a valid bull flag."""
    candles = [
        _green(5.00, 5.50, t=0),
        _green(5.50, 6.00, t=1),
        _green(6.00, 6.60, t=2),
        # Deep retrace
        _red(6.60, 5.80, t=3),  # >50% retrace of 5.00→6.60 pole
        _red(5.80, 5.70, t=4),
    ]
    sig = detect_bull_flag(candles, idx=4)
    assert sig is None


def test_bull_flag_needs_enough_candles():
    """Not enough candles — returns None."""
    candles = [_green(5.0, 5.5, t=i) for i in range(3)]
    sig = detect_bull_flag(candles, idx=2)
    assert sig is None


# ── Micro pullback ──────────────────────────────────────────────────────────

def test_micro_pullback_detected():
    """Strong upward move, single red candle pullback."""
    candles = [
        _green(5.00, 5.30, t=0),
        _green(5.30, 5.65, t=1),
        _green(5.65, 6.00, t=2),
        _green(6.00, 6.20, t=3),
        _red(6.20, 6.05, t=4),  # Single red pullback
    ]
    sig = detect_micro_pullback(candles, idx=4)
    assert sig is not None
    assert sig.pattern == "micro_pullback"
    assert sig.stop_price == candles[4].low


def test_micro_pullback_not_on_green_candle():
    """Current candle is green (not a pullback)."""
    candles = [
        _green(5.00, 5.30, t=0),
        _green(5.30, 5.65, t=1),
        _green(5.65, 6.00, t=2),
        _green(6.00, 6.20, t=3),
        _green(6.20, 6.40, t=4),  # Green — not a pullback
    ]
    sig = detect_micro_pullback(candles, idx=4)
    assert sig is None


# ── Flat top ────────────────────────────────────────────────────────────────

def test_flat_top_detected():
    """Three tests of the same resistance level."""
    resistance = 6.50
    candles = [
        _green(5.50, 6.48, high=6.50, t=0),   # Test 1
        _red(6.45, 6.30, t=1),
        _green(6.30, 6.47, high=6.50, t=2),   # Test 2
        _red(6.45, 6.35, t=3),
        _green(6.35, 6.48, high=6.50, t=4),   # Test 3 — current
    ]
    sig = detect_flat_top(candles, idx=4)
    assert sig is not None
    assert sig.pattern == "flat_top"
    assert sig.entry_trigger > resistance * 0.99


def test_flat_top_not_enough_tests():
    """Only 2 tests of resistance — not a flat top."""
    candles = [
        _green(5.50, 6.48, high=6.50, t=0),
        _red(6.45, 6.30, t=1),
        _green(6.30, 6.47, high=6.50, t=2),
    ]
    sig = detect_flat_top(candles, idx=2)
    assert sig is None


# ── Candle properties ───────────────────────────────────────────────────────

def test_candle_is_green():
    c = _candle(5.0, 5.5)
    assert c.is_green
    assert not c.is_red


def test_candle_is_red():
    c = _candle(5.5, 5.0)
    assert c.is_red
    assert not c.is_green


def test_shooting_star():
    # Long upper wick (0.75), tiny body (0.05), nearly no lower wick (0.001)
    c = Candle(
        bar_time=datetime(2024, 1, 2, 9, 30, tzinfo=timezone.utc),
        open=6.00, close=6.05, high=6.80, low=5.999, volume=100_000
    )
    # upper_wick=0.75, body=0.05 → 0.75 >= 2*0.05=0.10 ✓
    # lower_wick=0.001 <= 0.05*0.3=0.015 ✓
    assert c.is_shooting_star()
