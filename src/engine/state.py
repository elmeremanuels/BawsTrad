from __future__ import annotations

"""
State machine for bot operating modes.
Pure logic — no side effects, no DB, no logging. Easy to test and mock.

Single source of truth: current_mode(). Everything else reads this.
"""

from datetime import date, datetime, time, timedelta
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Optional, Tuple
from zoneinfo import ZoneInfo

import exchange_calendars as ec

ET = ZoneInfo("America/New_York")
_nyse = ec.get_calendar("XNYS")


@lru_cache(maxsize=1)
def _trading_window() -> tuple[time, time]:
    """
    Load window_start / window_end from config.yaml.
    Cached at first call (module stays pure — no repeated YAML reads).
    Falls back to full-session defaults (09:30 – 15:55) if config is missing.
    """
    try:
        import yaml
        cfg = Path(__file__).parent.parent.parent / "config.yaml"
        with open(cfg) as f:
            data = yaml.safe_load(f) or {}
        tr = data.get("trading", {})
        def _t(s: str) -> time:
            h, m = map(int, s.split(":"))
            return time(h, m)
        return _t(tr.get("window_start", "09:30")), _t(tr.get("window_end", "15:55"))
    except Exception:
        return time(9, 30), time(15, 55)


class Mode(str, Enum):
    ACTIVE_TRADING    = "active_trading"
    POSITION_MGMT     = "position_mgmt"
    EOD_REFLECT       = "eod_reflect"
    OVERNIGHT_INTEL   = "overnight_intel"
    PRE_MARKET_PREP   = "pre_market_prep"
    WEEKEND_DEEP_WORK = "weekend_deep_work"
    MAINTENANCE       = "maintenance"


# Emoji per mode — used in logs and Discord
MODE_EMOJI: dict[Mode, str] = {
    Mode.ACTIVE_TRADING:    "🟢",
    Mode.POSITION_MGMT:     "🔵",
    Mode.EOD_REFLECT:       "🟣",
    Mode.OVERNIGHT_INTEL:   "🌙",
    Mode.PRE_MARKET_PREP:   "🌅",
    Mode.WEEKEND_DEEP_WORK: "🏖️",
    Mode.MAINTENANCE:       "🔴",
}


def _is_trading_session(day: date) -> bool:
    """True if NYSE is open on `day`."""
    try:
        return bool(_nyse.is_session(day))
    except Exception:
        # Fallback: weekdays only (conservative — better than crashing)
        return day.weekday() < 5


def _session_close(day: date) -> time:
    """Market close time (ET) for `day`. Handles early-close days correctly."""
    try:
        close_utc = _nyse.session_close(day)
        return close_utc.astimezone(ET).time().replace(second=0, microsecond=0)
    except Exception:
        return time(16, 0)  # Default to normal close


def current_mode(
    now: Optional[datetime] = None,
    maintenance_active: bool = False,
) -> Mode:
    """
    Single source of truth for the bot's operating mode.

    Args:
        now: timezone-aware datetime (defaults to datetime.now(ET)).
        maintenance_active: True if kill switch is active or a critical error
                            has been flagged. Always returns MAINTENANCE when True.
    Returns:
        The current operating Mode.
    """
    if maintenance_active:
        return Mode.MAINTENANCE

    now_et = (now or datetime.now(ET)).astimezone(ET)
    today = now_et.date()

    # Weekend or US market holiday → deep work
    if not _is_trading_session(today):
        return Mode.WEEKEND_DEEP_WORK

    close = _session_close(today)
    t = now_et.time()
    win_start, win_end = _trading_window()  # e.g. (09:30, 15:55) from config.yaml

    if time(4, 0) <= t < win_start:
        return Mode.PRE_MARKET_PREP
    elif win_start <= t < win_end:
        return Mode.ACTIVE_TRADING
    elif win_end <= t < close:
        return Mode.POSITION_MGMT   # Short force-close window (e.g. 15:55 – 16:00)
    elif close <= t < time(20, 0):
        return Mode.EOD_REFLECT
    else:  # 20:00 – 04:00
        return Mode.OVERNIGHT_INTEL


def next_mode_change(now: Optional[datetime] = None) -> Tuple[Mode, datetime]:
    """
    Returns (next_mode, transition_time_et) — when the mode will next change and to what.
    Useful for the dashboard countdown.
    """
    now_et = (now or datetime.now(ET)).astimezone(ET)
    today = now_et.date()
    t = now_et.time()

    if not _is_trading_session(today):
        # Find next Monday (or next trading day at 04:00)
        candidate = today + timedelta(days=1)
        while not _is_trading_session(candidate):
            candidate += timedelta(days=1)
        transition = datetime(candidate.year, candidate.month, candidate.day, 4, 0, tzinfo=ET)
        return Mode.PRE_MARKET_PREP, transition

    close = _session_close(today)
    win_start, win_end = _trading_window()

    # Ordered list of (boundary_time, mode_that_starts_at_boundary)
    boundaries = [
        (time(4, 0), Mode.PRE_MARKET_PREP),
        (win_start,  Mode.ACTIVE_TRADING),
        (win_end,    Mode.POSITION_MGMT),
        (close,      Mode.EOD_REFLECT),
        (time(20, 0), Mode.OVERNIGHT_INTEL),
    ]

    for boundary, next_mode in boundaries:
        if t < boundary:
            transition = datetime(today.year, today.month, today.day,
                                  boundary.hour, boundary.minute, tzinfo=ET)
            return next_mode, transition

    # After 20:00 — next boundary is 04:00 next trading day
    candidate = today + timedelta(days=1)
    while not _is_trading_session(candidate):
        candidate += timedelta(days=1)
    transition = datetime(candidate.year, candidate.month, candidate.day, 4, 0, tzinfo=ET)
    return Mode.PRE_MARKET_PREP, transition


def is_trading_allowed(mode: Mode) -> bool:
    """May the bot open a new position in this mode?"""
    return mode == Mode.ACTIVE_TRADING


def is_exit_allowed(mode: Mode) -> bool:
    """May the bot close an existing position in this mode?"""
    return mode in (Mode.ACTIVE_TRADING, Mode.POSITION_MGMT)


def minutes_until_next_change(now: Optional[datetime] = None) -> int:
    """Convenience: seconds until the next mode boundary."""
    now_et = (now or datetime.now(ET)).astimezone(ET)
    _, transition = next_mode_change(now_et)
    delta = (transition - now_et).total_seconds()
    return max(0, int(delta // 60))


# ── Dashboard pause mechanism ──────────────────────────────────────────────────

def is_paused() -> bool:
    """
    Return True if the dashboard has requested a bot pause.
    Checks for existence of STATE_DIR/bot_paused.flag file.
    Designed to be called on every tick — Path.exists() is fast.
    """
    try:
        from src.config import settings
        from pathlib import Path
        return (Path(settings.STATE_DIR) / "bot_paused.flag").exists()
    except Exception:
        return False  # Never block the bot on config errors


def get_pause_reason() -> Optional[str]:
    """Return the reason stored in STATE_DIR/pause_reason.txt, or None."""
    try:
        from src.config import settings
        from pathlib import Path
        reason_file = Path(settings.STATE_DIR) / "pause_reason.txt"
        return reason_file.read_text().strip() if reason_file.exists() else None
    except Exception:
        return None
