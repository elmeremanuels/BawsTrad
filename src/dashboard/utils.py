from __future__ import annotations

"""Shared helpers for the Streamlit dashboard."""

from datetime import datetime, timezone


def fmt_currency(val: float, sign: bool = False) -> str:
    if val is None:
        return "—"
    prefix = "+" if sign and val > 0 else ""
    return f"{prefix}${val:,.2f}"


def fmt_pct(val: float, sign: bool = False) -> str:
    if val is None:
        return "—"
    prefix = "+" if sign and val > 0 else ""
    return f"{prefix}{val:.1f}%"


def fmt_r(val: float) -> str:
    if val is None:
        return "—"
    prefix = "+" if val > 0 else ""
    return f"{prefix}{val:.2f}R"


def time_ago(iso_str: str) -> str:
    """Convert ISO timestamp to '2m ago', '1h ago', etc."""
    if not iso_str:
        return "—"
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = (datetime.now(timezone.utc) - dt).total_seconds()
        if delta < 60:
            return f"{int(delta)}s ago"
        elif delta < 3600:
            return f"{int(delta // 60)}m ago"
        elif delta < 86400:
            return f"{int(delta // 3600)}h ago"
        else:
            return f"{int(delta // 86400)}d ago"
    except Exception:
        return iso_str[:16] if len(iso_str) >= 16 else iso_str


def pnl_color(val: float) -> str:
    """Return CSS color string for a P&L value."""
    if val is None:
        return "gray"
    return "#22c55e" if val >= 0 else "#ef4444"


def mode_badge(mode: str) -> str:
    """Return colored emoji badge for a mode string."""
    badges = {
        "active_trading":    "🟢 ACTIVE TRADING",
        "position_mgmt":     "🟡 POSITION MGMT",
        "eod_reflect":       "🔵 EOD REFLECT",
        "overnight_intel":   "🌙 OVERNIGHT INTEL",
        "pre_market_prep":   "🌅 PRE-MARKET PREP",
        "weekend_deep_work": "🏖️ WEEKEND",
        "maintenance":       "🔴 MAINTENANCE",
        "unknown":           "⚪ UNKNOWN",
    }
    return badges.get(mode, f"⚪ {mode.upper()}")


def health_icon(last_seen_iso: str | None, warn_seconds: int = 300, error_seconds: int = 900) -> str:
    """Return 🟢/🟡/🔴 based on how long ago a service was last seen."""
    if not last_seen_iso:
        return "⚪"
    try:
        dt = datetime.fromisoformat(last_seen_iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - dt).total_seconds()
        if age < warn_seconds:
            return "🟢"
        elif age < error_seconds:
            return "🟡"
        else:
            return "🔴"
    except Exception:
        return "⚪"
