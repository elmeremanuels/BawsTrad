from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from src.engine.state import Mode, current_mode, is_exit_allowed, is_trading_allowed, next_mode_change

ET = ZoneInfo("America/New_York")


# ── Core mode detection ───────────────────────────────────────────────────────

def test_pre_market():
    assert current_mode(datetime(2026, 5, 14, 7, 0, tzinfo=ET)) == Mode.PRE_MARKET_PREP

def test_active_trading_window():
    assert current_mode(datetime(2026, 5, 14, 10, 0, tzinfo=ET)) == Mode.ACTIVE_TRADING

def test_active_trading_at_open():
    assert current_mode(datetime(2026, 5, 14, 9, 30, tzinfo=ET)) == Mode.ACTIVE_TRADING

def test_position_mgmt():
    assert current_mode(datetime(2026, 5, 14, 12, 0, tzinfo=ET)) == Mode.POSITION_MGMT

def test_position_mgmt_at_boundary():
    assert current_mode(datetime(2026, 5, 14, 11, 30, tzinfo=ET)) == Mode.POSITION_MGMT

def test_eod_reflect():
    assert current_mode(datetime(2026, 5, 14, 17, 0, tzinfo=ET)) == Mode.EOD_REFLECT

def test_overnight_intel():
    assert current_mode(datetime(2026, 5, 14, 22, 0, tzinfo=ET)) == Mode.OVERNIGHT_INTEL

def test_overnight_early_morning():
    # 01:00 ET on a weekday is still in overnight_intel
    assert current_mode(datetime(2026, 5, 14, 1, 0, tzinfo=ET)) == Mode.OVERNIGHT_INTEL

# ── Weekend & holidays ────────────────────────────────────────────────────────

def test_weekend_saturday():
    # 2026-05-16 is a Saturday
    assert current_mode(datetime(2026, 5, 16, 10, 0, tzinfo=ET)) == Mode.WEEKEND_DEEP_WORK

def test_weekend_sunday():
    # 2026-05-17 is a Sunday
    assert current_mode(datetime(2026, 5, 17, 14, 0, tzinfo=ET)) == Mode.WEEKEND_DEEP_WORK

def test_thanksgiving_2026():
    # 2026-11-26 = Thanksgiving (4th Thursday of November)
    assert current_mode(datetime(2026, 11, 26, 10, 0, tzinfo=ET)) == Mode.WEEKEND_DEEP_WORK

def test_july_4_2026():
    # 2026-07-04 is a Saturday, observed on Friday 2026-07-03
    assert current_mode(datetime(2026, 7, 3, 10, 0, tzinfo=ET)) == Mode.WEEKEND_DEEP_WORK

def test_christmas_2026():
    # 2026-12-25 is a Friday — NYSE closed
    assert current_mode(datetime(2026, 12, 25, 10, 0, tzinfo=ET)) == Mode.WEEKEND_DEEP_WORK

# ── Maintenance override ──────────────────────────────────────────────────────

def test_maintenance_override_during_trading():
    assert current_mode(datetime(2026, 5, 14, 10, 0, tzinfo=ET),
                        maintenance_active=True) == Mode.MAINTENANCE

def test_maintenance_override_during_weekend():
    assert current_mode(datetime(2026, 5, 16, 10, 0, tzinfo=ET),
                        maintenance_active=True) == Mode.MAINTENANCE

# ── Permission helpers ────────────────────────────────────────────────────────

def test_trading_allowed_only_in_active():
    assert is_trading_allowed(Mode.ACTIVE_TRADING) is True
    for m in Mode:
        if m != Mode.ACTIVE_TRADING:
            assert is_trading_allowed(m) is False

def test_exit_allowed_in_active_and_mgmt():
    assert is_exit_allowed(Mode.ACTIVE_TRADING) is True
    assert is_exit_allowed(Mode.POSITION_MGMT) is True
    assert is_exit_allowed(Mode.EOD_REFLECT) is False
    assert is_exit_allowed(Mode.MAINTENANCE) is False

# ── Next mode change ──────────────────────────────────────────────────────────

def test_next_change_from_pre_market():
    next_mode, t = next_mode_change(datetime(2026, 5, 14, 7, 0, tzinfo=ET))
    assert next_mode == Mode.ACTIVE_TRADING
    assert t.hour == 9 and t.minute == 30

def test_next_change_from_active_trading():
    next_mode, t = next_mode_change(datetime(2026, 5, 14, 10, 0, tzinfo=ET))
    assert next_mode == Mode.POSITION_MGMT
    assert t.hour == 11 and t.minute == 30

def test_next_change_from_weekend():
    # Saturday → next trading day Monday at 04:00
    next_mode, t = next_mode_change(datetime(2026, 5, 16, 10, 0, tzinfo=ET))
    assert next_mode == Mode.PRE_MARKET_PREP
    assert t.weekday() == 0  # Monday
    assert t.hour == 4
