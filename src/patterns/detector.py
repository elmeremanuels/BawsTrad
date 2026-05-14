from __future__ import annotations

"""
Pattern detector — bull flag, micro pullback, flat top breakout.
Takes a list of Candle objects (1-min or 5-min) and returns detected signals.

Signal format:
  {
    "pattern": "bull_flag" | "micro_pullback" | "flat_top",
    "entry_trigger": float,   # Price at which entry fires (breakout level)
    "stop_price": float,      # Hard stop loss
    "signal_bar_idx": int,    # Index of the signal candle in the list
  }
"""

from dataclasses import dataclass
from typing import List, Optional

from src.patterns.candles import Candle, avg_volume, retracement_pct


@dataclass
class PatternSignal:
    pattern: str          # bull_flag | micro_pullback | flat_top
    entry_trigger: float  # Break above this price = entry
    stop_price: float     # Exit immediately if price drops here
    signal_bar_idx: int   # Which candle index triggered the signal
    confidence: float     # 0–1, informational only


def detect_bull_flag(
    candles: List[Candle],
    idx: int,
    min_pole_candles: int = 3,
    max_flag_candles: int = 5,
    max_retrace_pct: float = 0.50,
    min_pole_body_pct: float = 0.50,
) -> Optional[PatternSignal]:
    """
    Detect a bull flag pattern ending at `candles[idx]`.

    Structure:
    - Flag pole: `min_pole_candles` strong green candles with above-avg volume
    - Flag: 2–`max_flag_candles` consolidating candles, lower volume
    - Retracement: flag low doesn't exceed 50% of the pole
    - Entry trigger: high of the flag (breakout level)
    - Stop: low of the flag
    """
    if idx < min_pole_candles + 1:
        return None

    # Try to find flag: look back for consolidation
    # The current candle (idx) is within or at end of the flag
    for flag_len in range(2, min(max_flag_candles + 1, idx)):
        flag = candles[idx - flag_len + 1: idx + 1]
        pole_end_idx = idx - flag_len

        if pole_end_idx < min_pole_candles - 1:
            continue

        # Try different pole lengths
        for pole_len in range(min_pole_candles, min(7, pole_end_idx + 2)):
            pole_start_idx = pole_end_idx - pole_len + 1
            if pole_start_idx < 0:
                break
            pole = candles[pole_start_idx: pole_end_idx + 1]

            # Pole criteria: mostly green, strong bodies, rising
            green_count = sum(1 for c in pole if c.is_strong_green(min_pole_body_pct))
            if green_count < len(pole) * 0.7:
                continue

            # Pole is net upward
            pole_open = pole[0].open
            pole_top = max(c.high for c in pole)
            if pole_top <= pole_open:
                continue

            # Pole volume: above overall average
            all_avg = avg_volume(candles[max(0, pole_start_idx - 10): pole_start_idx])
            pole_avg = avg_volume(pole)
            if all_avg > 0 and pole_avg < all_avg * 0.8:
                continue

            # Flag criteria: consolidation (lower highs or flat)
            flag_volume_avg = avg_volume(flag)
            if pole_avg > 0 and flag_volume_avg > pole_avg * 1.2:
                continue  # Flag volume too high (not consolidating)

            # Retracement check
            retrace = retracement_pct(pole, flag)
            if retrace > max_retrace_pct:
                continue  # Pulled back too far

            # Signal
            entry_trigger = max(c.high for c in flag)
            stop_price = min(c.low for c in flag)
            if entry_trigger <= stop_price:
                continue

            confidence = min(1.0, green_count / len(pole) * (1.0 - retrace))
            return PatternSignal(
                pattern="bull_flag",
                entry_trigger=entry_trigger,
                stop_price=stop_price,
                signal_bar_idx=idx,
                confidence=confidence,
            )

    return None


def detect_micro_pullback(
    candles: List[Candle],
    idx: int,
    min_prior_green: int = 3,
    max_red_candles: int = 1,
) -> Optional[PatternSignal]:
    """
    Detect a micro pullback pattern ending at `candles[idx]`.

    Structure:
    - Prior strong upward move: `min_prior_green` green candles
    - Single red candle (or wick down) — the pullback
    - Entry trigger: high of the red candle (new high after pullback)
    - Stop: low of the red candle
    """
    if idx < min_prior_green + max_red_candles:
        return None

    current = candles[idx]

    # The pullback is the current candle (must be red or small wick)
    is_pullback = current.is_red or (current.is_green and current.lower_wick > current.body_size)
    if not is_pullback:
        return None

    # Prior candles must show a strong upward move
    lookback = min_prior_green + 2
    prior = candles[max(0, idx - lookback): idx]
    if len(prior) < min_prior_green:
        return None

    green_candles = [c for c in prior if c.is_green]
    if len(green_candles) < min_prior_green:
        return None

    # The move must be net upward
    move_start = prior[0].open
    move_peak = max(c.high for c in prior)
    if move_peak <= move_start * 1.02:  # Need at least 2% move before pullback
        return None

    # Volume during pullback should stay reasonably high (not collapse)
    prior_avg = avg_volume(prior)
    if prior_avg > 0 and current.volume < prior_avg * 0.20:
        return None  # Volume collapsed — not a healthy pullback

    entry_trigger = current.high
    stop_price = current.low
    if entry_trigger <= stop_price:
        stop_price = current.low * 0.999  # Tiny buffer

    confidence = min(1.0, len(green_candles) / lookback)
    return PatternSignal(
        pattern="micro_pullback",
        entry_trigger=entry_trigger,
        stop_price=stop_price,
        signal_bar_idx=idx,
        confidence=confidence,
    )


def detect_flat_top(
    candles: List[Candle],
    idx: int,
    min_tests: int = 3,
    tolerance_pct: float = 0.003,  # 0.3% price tolerance for "flat"
) -> Optional[PatternSignal]:
    """
    Detect a flat top breakout pattern.

    Structure:
    - Multiple tests (>= min_tests) of the same resistance level
    - Each test on lower volume than the previous (or at least not increasing)
    - Entry: break above the flat top with volume
    - Stop: lowest low of the consolidation
    """
    if idx < min_tests + 1:
        return None

    lookback_window = min(20, idx)
    window = candles[idx - lookback_window: idx + 1]

    if len(window) < min_tests + 1:
        return None

    # Find the resistance level: the most-tested high
    highs = [c.high for c in window[:-1]]  # Exclude current candle
    if not highs:
        return None

    # Cluster highs within tolerance
    candidate_resistance = max(highs)
    tol = candidate_resistance * tolerance_pct

    tests = [c for c in window[:-1] if abs(c.high - candidate_resistance) <= tol]
    if len(tests) < min_tests:
        return None

    # Volume should not be increasing on each test
    test_volumes = [c.volume for c in tests]
    # Simple check: last test volume < first test volume
    if len(test_volumes) >= 2 and test_volumes[-1] > test_volumes[0] * 1.5:
        return None  # Volume increasing — not a flat top

    # Current candle must be near the resistance (about to break out)
    current = candles[idx]
    if current.high < candidate_resistance * (1 - tolerance_pct * 2):
        return None

    entry_trigger = candidate_resistance * (1 + tolerance_pct)  # Just above resistance
    stop_price = min(c.low for c in tests)

    return PatternSignal(
        pattern="flat_top",
        entry_trigger=entry_trigger,
        stop_price=stop_price,
        signal_bar_idx=idx,
        confidence=min(1.0, len(tests) / (min_tests + 2)),
    )


def scan_for_patterns(candles: List[Candle]) -> List[PatternSignal]:
    """
    Scan all candles for any of the three patterns.
    Returns a list of signals (one per detected pattern per bar).
    Useful for backtesting: scan the full day's bars at once.
    """
    signals: List[PatternSignal] = []
    for i in range(5, len(candles)):
        sig = detect_bull_flag(candles, i)
        if sig:
            signals.append(sig)
            continue  # One pattern per bar

        sig = detect_micro_pullback(candles, i)
        if sig:
            signals.append(sig)
            continue

        sig = detect_flat_top(candles, i)
        if sig:
            signals.append(sig)

    return signals
