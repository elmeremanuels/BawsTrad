from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from src.engine.decision import evaluate_entry
from src.engine.risk import RiskState
from src.patterns.detector import PatternSignal
from src.scanner.criteria import NewsCatalyst, TickerSnapshot

ET = ZoneInfo("America/New_York")


def _snap(**kwargs) -> TickerSnapshot:
    defaults = dict(
        ticker="TEST",
        price=5.50,
        percent_change_today=25.0,
        relative_volume=8.0,
        float_shares=5_000_000,
        news_catalyst=NewsCatalyst(tier="A", category="FDA_approval", headline="FDA approves TEST"),
    )
    defaults.update(kwargs)
    return TickerSnapshot(**defaults)


def _signal(entry_trigger=5.60, stop_price=5.30) -> PatternSignal:
    return PatternSignal(
        pattern="bull_flag",
        entry_trigger=entry_trigger,
        stop_price=stop_price,
        signal_bar_idx=5,
        confidence=0.8,
    )


def _state() -> RiskState:
    return RiskState(account_value=25_000, date="2024-01-15")


def _et(hour: int, minute: int = 0) -> datetime:
    return datetime(2024, 1, 15, hour, minute, 0, tzinfo=ET)


def test_entry_approved_when_all_checks_pass():
    dec = evaluate_entry(
        ticker=_snap(),
        signal=_signal(),
        account_value=25_000,
        risk_state=_state(),
        now=_et(10, 0),
    )
    assert dec.approved is True
    assert dec.shares > 0
    assert dec.target_price > dec.entry_price
    assert dec.stop_price < dec.entry_price


def test_entry_blocked_before_trading_window():
    dec = evaluate_entry(
        ticker=_snap(),
        signal=_signal(),
        account_value=25_000,
        risk_state=_state(),
        now=_et(9, 0),  # Before 9:30
    )
    assert dec.approved is False
    assert "before_trading_window" in dec.reason


def test_entry_blocked_after_trading_window():
    dec = evaluate_entry(
        ticker=_snap(),
        signal=_signal(),
        account_value=25_000,
        risk_state=_state(),
        now=_et(11, 30),  # At 11:30 = closed
    )
    assert dec.approved is False
    assert "after_trading_window" in dec.reason


def test_entry_blocked_when_daily_limit_hit():
    state = _state()
    state.daily_pnl = -800.0  # Exceeds 3% daily max
    dec = evaluate_entry(
        ticker=_snap(),
        signal=_signal(),
        account_value=25_000,
        risk_state=state,
        now=_et(10, 0),
    )
    assert dec.approved is False
    assert "daily_limit" in dec.reason


def test_target_is_at_least_2r():
    dec = evaluate_entry(
        ticker=_snap(),
        signal=_signal(entry_trigger=5.60, stop_price=5.30),
        account_value=25_000,
        risk_state=_state(),
        now=_et(10, 0),
    )
    assert dec.approved is True
    risk = dec.entry_price - dec.stop_price
    reward = dec.target_price - dec.entry_price
    assert reward / risk >= 2.0 - 0.001
