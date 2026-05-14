from __future__ import annotations

"""
Candle pattern primitives — pure Python/pandas, no ta-lib.
All functions operate on individual Bar objects or small sequences.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional


@dataclass
class Candle:
    """A single OHLCV candle."""
    bar_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    vwap: Optional[float] = None

    @property
    def body_size(self) -> float:
        return abs(self.close - self.open)

    @property
    def total_range(self) -> float:
        return self.high - self.low

    @property
    def is_green(self) -> bool:
        return self.close >= self.open

    @property
    def is_red(self) -> bool:
        return self.close < self.open

    @property
    def upper_wick(self) -> float:
        return self.high - max(self.open, self.close)

    @property
    def lower_wick(self) -> float:
        return min(self.open, self.close) - self.low

    @property
    def body_pct(self) -> float:
        """Body as % of total range. 0 = doji, 1.0 = marubozu."""
        if self.total_range == 0:
            return 0.0
        return self.body_size / self.total_range

    def is_strong_green(self, min_body_pct: float = 0.5) -> bool:
        return self.is_green and self.body_pct >= min_body_pct

    def is_shooting_star(self) -> bool:
        """Upper wick > 2× body, small lower wick — bearish reversal."""
        if self.body_size == 0:
            return False
        return (
            self.upper_wick >= 2 * self.body_size
            and self.lower_wick <= self.body_size * 0.3
            and self.total_range > 0
        )

    def is_bearish_engulfing(self, prior: "Candle") -> bool:
        """This candle engulfs the prior green candle — bearish reversal."""
        return (
            self.is_red
            and prior.is_green
            and self.open >= prior.close
            and self.close <= prior.open
        )

    def is_doji(self, threshold: float = 0.1) -> bool:
        return self.body_pct <= threshold


def avg_volume(candles: List[Candle]) -> float:
    if not candles:
        return 0.0
    return sum(c.volume for c in candles) / len(candles)


def pole_height(candles: List[Candle]) -> float:
    """Height of a flag pole in dollars."""
    if not candles:
        return 0.0
    return max(c.high for c in candles) - min(c.low for c in candles)


def retracement_pct(pole: List[Candle], flag: List[Candle]) -> float:
    """
    How much did the flag retrace the pole?
    0.0 = no retrace, 1.0 = full retrace, 0.5 = 50% retrace.
    """
    if not pole or not flag:
        return 0.0
    pole_top = max(c.high for c in pole)
    pole_bottom = min(c.low for c in pole)
    flag_low = min(c.low for c in flag)
    height = pole_top - pole_bottom
    if height == 0:
        return 0.0
    return (pole_top - flag_low) / height
