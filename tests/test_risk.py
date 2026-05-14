from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from src.engine.risk import (
    RISK_RULES, RiskState, check_daily_limits,
    minimum_rr_ratio, passes_rr_check
)
from src.engine.position import calculate_position_size

ET = ZoneInfo("America/New_York")


def _et(hour: int, minute: int = 0) -> datetime:
    return datetime(2024, 1, 15, hour, minute, 0, tzinfo=ET)


# ── Position sizing ──────────────────────────────────────────────────────────

def test_position_size_basic():
    """$25k account, 1% risk, $0.50 stop distance → 500 shares."""
    shares = calculate_position_size(
        account_value=25_000,
        entry_price=5.50,
        stop_price=5.00,   # $0.50 risk/share
        risk_per_trade_pct=0.01,
    )
    # $250 at risk / $0.50 per share = 500 shares
    assert shares == 500


def test_position_size_capped_by_value():
    """Position can't exceed 25% of account."""
    shares = calculate_position_size(
        account_value=10_000,
        entry_price=10.00,
        stop_price=9.50,   # $0.50 risk → 20 shares from risk calculation
        risk_per_trade_pct=0.01,
    )
    max_by_value = int(10_000 * 0.25 / 10.00)  # 250 shares
    assert shares <= max_by_value


def test_position_size_zero_when_stop_equals_entry():
    shares = calculate_position_size(25_000, 5.50, 5.50)
    assert shares == 0


# ── R:R ratio ────────────────────────────────────────────────────────────────

def test_rr_ratio_calculation():
    # Entry 5.50, stop 5.00 (risk=0.50), target 6.50 (reward=1.00) → 2.0R
    ratio = minimum_rr_ratio(entry=5.50, stop=5.00, target=6.50)
    assert abs(ratio - 2.0) < 0.001


def test_passes_rr_check_2r():
    assert passes_rr_check(entry=5.50, stop=5.00, target=6.50) is True


def test_fails_rr_check_1r():
    # Only 1:1 — fails the 2:1 minimum
    assert passes_rr_check(entry=5.50, stop=5.00, target=6.00) is False


# ── Daily risk limits ────────────────────────────────────────────────────────

def test_daily_max_loss_stops_trading():
    state = RiskState(account_value=25_000, date="2024-01-15")
    # 3% of 25k = $750 max loss
    state.daily_pnl = -800.0
    ok, reason = check_daily_limits(state, _et(10, 0))
    assert not ok
    assert "daily_max_loss" in reason


def test_consecutive_losses_stops_trading():
    state = RiskState(account_value=25_000, date="2024-01-15")
    state.consecutive_losses = 3
    ok, reason = check_daily_limits(state, _et(10, 0))
    assert not ok
    assert "consecutive_losses" in reason


def test_max_trades_per_day_stops_trading():
    state = RiskState(account_value=25_000, date="2024-01-15")
    state.trades_today = 10
    ok, reason = check_daily_limits(state, _et(10, 0))
    assert not ok
    assert "max_trades_per_day" in reason


def test_post_loss_cooldown():
    state = RiskState(account_value=25_000, date="2024-01-15")
    loss_time = _et(9, 45)
    state.last_loss_time = loss_time
    # 30 seconds later — still in cooldown (60s required)
    ok, reason = check_daily_limits(
        state,
        datetime(2024, 1, 15, 9, 45, 30, tzinfo=ET)
    )
    assert not ok
    assert "cooldown" in reason


def test_clean_state_allows_trading():
    state = RiskState(account_value=25_000, date="2024-01-15")
    ok, reason = check_daily_limits(state, _et(10, 0))
    assert ok
    assert reason == ""


def test_record_trade_updates_state():
    state = RiskState(account_value=25_000, date="2024-01-15")
    state.record_trade(pnl=250.0, closed_at=_et(10, 15))
    assert state.trades_today == 1
    assert state.daily_pnl == 250.0
    assert state.consecutive_losses == 0


def test_record_loss_increments_consecutive():
    state = RiskState(account_value=25_000, date="2024-01-15")
    state.record_trade(pnl=-200.0, closed_at=_et(10, 0))
    state.record_trade(pnl=-150.0, closed_at=_et(10, 15))
    assert state.consecutive_losses == 2


def test_win_resets_consecutive_losses():
    state = RiskState(account_value=25_000, date="2024-01-15")
    state.record_trade(pnl=-200.0, closed_at=_et(10, 0))
    state.record_trade(pnl=-150.0, closed_at=_et(10, 15))
    state.record_trade(pnl=400.0, closed_at=_et(10, 30))
    assert state.consecutive_losses == 0
