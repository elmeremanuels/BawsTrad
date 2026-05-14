from __future__ import annotations

"""
Entry decision engine — all checks must be green before a trade is opened.
100% rule-based. No LLM in this path. Ever.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Tuple
from zoneinfo import ZoneInfo

from src.engine.risk import RISK_RULES, RiskState, check_daily_limits, passes_rr_check
from src.engine.position import calculate_position_size
from src.patterns.detector import PatternSignal
from src.scanner.criteria import TickerSnapshot

ET = ZoneInfo("America/New_York")


@dataclass
class EntryDecision:
    approved: bool
    reason: str
    shares: int = 0
    entry_price: float = 0.0
    stop_price: float = 0.0
    target_price: float = 0.0
    setup_type: str = ""


def _parse_time(t_str: str, date: datetime) -> datetime:
    """Convert 'HH:MM' + date into a timezone-aware ET datetime."""
    h, m = map(int, t_str.split(":"))
    return date.replace(hour=h, minute=m, second=0, microsecond=0, tzinfo=ET)


def evaluate_entry(
    ticker: TickerSnapshot,
    signal: PatternSignal,
    account_value: float,
    risk_state: RiskState,
    now: datetime,
    fill_price: Optional[float] = None,
) -> EntryDecision:
    """
    Run every entry check. Returns EntryDecision with approved=True only if
    ALL checks pass. The first failed check is reported as `reason`.

    `fill_price`: simulated fill (e.g. open of next bar). If None, uses signal.entry_trigger.
    """
    entry = fill_price if fill_price is not None else signal.entry_trigger
    stop = signal.stop_price
    now_et = now.astimezone(ET)

    # ── 1. Trading window ───────────────────────────────────────────────────
    day_base = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    window_open = _parse_time(RISK_RULES["trading_window"][0], now_et)
    window_close = _parse_time(RISK_RULES["no_new_trades_after"], now_et)

    if now_et < window_open:
        return EntryDecision(False, "before_trading_window")
    if now_et >= window_close:
        return EntryDecision(False, "after_trading_window")

    # ── 2. Daily limits ─────────────────────────────────────────────────────
    ok, reason = check_daily_limits(risk_state, now)
    if not ok:
        return EntryDecision(False, f"daily_limit:{reason}")

    # ── 3. Stock selection still valid ─────────────────────────────────────
    from src.scanner.criteria import passes_stock_selection
    if not passes_stock_selection(ticker):
        return EntryDecision(False, "stock_selection_failed")

    # ── 4. Entry price must be above stop ──────────────────────────────────
    if entry <= stop:
        return EntryDecision(False, "entry_below_stop")

    # ── 5. R:R ratio >= 2:1 ────────────────────────────────────────────────
    # Target = entry + 2× risk
    risk = abs(entry - stop)
    target = entry + (risk * RISK_RULES["profit_loss_ratio_min"])

    if not passes_rr_check(entry, stop, target):
        return EntryDecision(False, "rr_ratio_too_low")

    # ── 6. Position sizing ─────────────────────────────────────────────────
    shares = calculate_position_size(
        account_value=account_value,
        entry_price=entry,
        stop_price=stop,
        risk_per_trade_pct=RISK_RULES["max_risk_per_trade_pct"],
    )
    if shares == 0:
        return EntryDecision(False, "position_size_zero")

    return EntryDecision(
        approved=True,
        reason="all_checks_passed",
        shares=shares,
        entry_price=entry,
        stop_price=stop,
        target_price=round(target, 4),
        setup_type=signal.pattern,
    )
