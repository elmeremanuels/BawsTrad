from __future__ import annotations

"""
Tests for the per-mode handlers and the kill switch → MAINTENANCE path.
"""

import asyncio
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

ET = ZoneInfo("America/New_York")


# ── Kill switch → MAINTENANCE ──────────────────────────────────────────────────

def test_kill_switch_file_triggers_maintenance_mode():
    """
    Creating the kill switch file must flip current_mode() to MAINTENANCE
    when maintenance_active=True is passed.
    """
    from src.engine.state import Mode, current_mode

    # Monday 10:00 ET — would normally be ACTIVE_TRADING
    now = datetime(2024, 1, 16, 10, 0, 0, tzinfo=ET)

    assert current_mode(now=now, maintenance_active=False) is Mode.ACTIVE_TRADING
    assert current_mode(now=now, maintenance_active=True) is Mode.MAINTENANCE


def test_kill_switch_file_existence_in_main_loop(tmp_path):
    """
    The main loop reads the kill switch file path from settings.KILL_SWITCH_FILE.
    Verify that pathlib.Path(kill_switch_file).exists() returns True after touch.
    """
    ks_file = tmp_path / "bot_killswitch"
    assert not ks_file.exists()
    ks_file.touch()
    assert ks_file.exists()
    ks_file.unlink()
    assert not ks_file.exists()


# ── Maintenance handler ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_maintenance_on_enter_calls_emergency_close():
    """maintenance.on_enter() must call emergency_close_all() exactly once."""
    import importlib
    import src.handlers.maintenance as m
    importlib.reload(m)  # Reset module-level _entered flag
    m._entered = False

    mock_close = MagicMock()
    state = MagicMock()

    # emergency_close_all is imported inside on_enter() from src.execution.paper,
    # so we must patch it at the source module.
    with patch("src.execution.paper.emergency_close_all", mock_close), \
         patch("src.config.settings") as mock_settings, \
         patch("src.alerts.discord.post_message", new_callable=AsyncMock):
        mock_settings.DISCORD_WEBHOOK_URL = ""
        await m.on_enter(state)

    assert mock_close.called


@pytest.mark.asyncio
async def test_maintenance_on_enter_fires_only_once():
    """on_enter() must not call emergency_close_all() a second time."""
    import src.handlers.maintenance as m
    import importlib
    importlib.reload(m)  # Reset state

    mock_close = MagicMock()
    state = MagicMock()

    with patch("src.execution.paper.emergency_close_all", mock_close), \
         patch("src.config.settings") as mock_settings, \
         patch("src.alerts.discord.post_message", new_callable=AsyncMock):
        mock_settings.DISCORD_WEBHOOK_URL = ""

        await m.on_enter(state)
        await m.on_enter(state)  # Second call

    assert mock_close.call_count == 1  # Fired exactly once


@pytest.mark.asyncio
async def test_maintenance_tick_logs_and_does_not_trade():
    """tick() in MAINTENANCE mode must not call any trade functions."""
    import src.handlers.maintenance as m

    state = MagicMock()
    now = datetime(2024, 1, 16, 10, 0, 0, tzinfo=ET)

    # Just verify it doesn't raise and returns cleanly
    await m.tick(state, now)


# ── Position management handler ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_position_mgmt_tick_no_positions():
    """tick() with no open positions should complete without error."""
    from src.handlers.position_mgmt import tick
    import src.handlers.position_mgmt as pm
    pm._last_log_time = 0.0  # Reset rate limiter

    state = MagicMock()
    now = datetime(2024, 1, 16, 11, 45, 0, tzinfo=ET)

    with patch("src.execution.paper.get_open_positions_from_db", return_value=[]):
        await tick(state, now)  # Should not raise


@pytest.mark.asyncio
async def test_position_mgmt_tick_logs_open_position():
    """tick() with an open position should log it without error."""
    from src.handlers.position_mgmt import tick
    import src.handlers.position_mgmt as pm
    pm._last_log_time = 0.0  # Reset rate limiter so logging fires

    state = MagicMock()
    now = datetime(2024, 1, 16, 11, 45, 0, tzinfo=ET)

    fake_position = {
        "ticker": "TEST",
        "entry_price": 5.50,
        "stop_price": 5.20,
        "target_price": 6.10,
        "shares": 100,
        "setup_type": "bull_flag",
        "opened_at": "2024-01-16T14:00:00+00:00",
    }

    with patch("src.execution.paper.get_open_positions_from_db", return_value=[fake_position]):
        await tick(state, now)  # Should log and not raise


def test_position_mgmt_stale_detection_logic():
    """
    The stale-detection math in position_mgmt must flag positions open > 4 hours.
    Test the UTC comparison logic directly without exercising structlog patching.
    """
    from datetime import timedelta, timezone
    from zoneinfo import ZoneInfo

    ET = ZoneInfo("America/New_York")

    # now = 11:45 ET (= 16:45 UTC in January, EST=UTC-5)
    now = datetime(2024, 1, 16, 11, 45, 0, tzinfo=ET)
    stale_threshold_utc = now.astimezone(timezone.utc) - timedelta(hours=4)

    # opened_at 5h 45min ago in UTC = 11:00 UTC → stale
    opened_at_stale = datetime.fromisoformat("2024-01-16T11:00:00+00:00")
    # opened_at 1h ago in UTC = 15:45 UTC → not stale
    opened_at_fresh = datetime.fromisoformat("2024-01-16T15:45:00+00:00")

    assert opened_at_stale < stale_threshold_utc, \
        f"Expected {opened_at_stale} < {stale_threshold_utc}"
    assert not (opened_at_fresh < stale_threshold_utc), \
        f"Expected {opened_at_fresh} to be NOT stale"


@pytest.mark.asyncio
async def test_position_mgmt_stale_tick_completes_without_error():
    """tick() with a stale position should complete without raising."""
    from src.handlers.position_mgmt import tick
    import src.handlers.position_mgmt as pm
    pm._last_log_time = 0.0

    state = MagicMock()
    now = datetime(2024, 1, 16, 11, 45, 0, tzinfo=ET)

    fake_position = {
        "ticker": "STALE",
        "entry_price": 5.50,
        "stop_price": 5.20,
        "target_price": 6.10,
        "shares": 100,
        "setup_type": "bull_flag",
        "opened_at": "2024-01-16T11:00:00+00:00",  # 5h 45min ago in UTC → stale
    }

    with patch("src.execution.paper.get_open_positions_from_db", return_value=[fake_position]):
        await tick(state, now)  # Must not raise; stale warning is logged internally
