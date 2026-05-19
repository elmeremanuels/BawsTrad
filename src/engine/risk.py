from __future__ import annotations

"""
Risk management — harde regels uit SampleTradingPlan.pdf.
Tracked via RiskState (one per trading day).
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Tuple

RISK_RULES = {
    "profit_loss_ratio_min": 2.0,
    "max_risk_per_trade_pct": 0.01,
    "daily_max_loss_pct": 0.03,
    "max_consecutive_losses": 3,
    "max_trades_per_day": 10,
    "trading_window": ("09:30", "15:55"),   # full session; window_end read from config.yaml
    "no_new_trades_after": "15:55",
    "force_close_at": "15:55",
    "post_loss_cooldown_sec": 60,
    "post_trade_cooldown_sec": 30,
}


def _load_risk_from_config() -> None:
    """Overwrite RISK_RULES defaults with values from config.yaml (risk: section)."""
    try:
        from pathlib import Path
        import yaml
        cfg_path = Path(__file__).parent.parent.parent / "config.yaml"
        with open(cfg_path) as f:
            cfg = yaml.safe_load(f) or {}
        risk_cfg = cfg.get("risk", {})
        numeric_keys = (
            "profit_loss_ratio_min", "max_risk_per_trade_pct", "daily_max_loss_pct",
            "max_consecutive_losses", "max_trades_per_day",
            "post_loss_cooldown_sec", "post_trade_cooldown_sec", "max_position_pct",
        )
        for key in numeric_keys:
            if key in risk_cfg:
                RISK_RULES[key] = risk_cfg[key]
    except Exception:
        pass  # safe — keep hardcoded defaults


_load_risk_from_config()

TRIFECTA_TARGETS = {
    "novice":   {"consistency_weeks": 1,  "accuracy_min": 0.40, "pnl_ratio_min": 0.5},
    "beginner": {"consistency_weeks": 2,  "accuracy_min": 0.50, "pnl_ratio_min": 1.0},
    "advanced": {"consistency_weeks": 4,  "accuracy_min": 0.60, "pnl_ratio_min": 1.5},
    "pro":      {"consistency_weeks": 5,  "accuracy_min": 0.70, "pnl_ratio_min": 1.0},
}

TIER_UNLOCK_REQUIREMENTS = {
    "novice_to_beginner":   {"min_trades": 50,  "min_accuracy": 0.50, "min_pnl_ratio": 1.0, "min_days": 20},
    "beginner_to_advanced": {"min_trades": 150, "min_accuracy": 0.60, "min_pnl_ratio": 1.5, "min_days": 60},
    "advanced_to_pro":      {"min_trades": 300, "min_accuracy": 0.70, "min_pnl_ratio": 1.0, "min_days": 120},
}


@dataclass
class RiskState:
    """Mutable risk tracker — one instance per trading day."""
    account_value: float
    date: str
    trades_today: int = 0
    consecutive_losses: int = 0
    daily_pnl: float = 0.0
    last_trade_time: Optional[datetime] = None
    last_loss_time: Optional[datetime] = None
    day_stopped: bool = False   # True once daily limits are hit

    @property
    def daily_loss_limit(self) -> float:
        return self.account_value * RISK_RULES["daily_max_loss_pct"]

    @property
    def hit_daily_loss(self) -> bool:
        return self.daily_pnl <= -self.daily_loss_limit

    def record_trade(self, pnl: float, closed_at: datetime) -> None:
        self.trades_today += 1
        self.daily_pnl += pnl
        self.last_trade_time = closed_at
        if pnl < 0:
            self.consecutive_losses += 1
            self.last_loss_time = closed_at
        else:
            self.consecutive_losses = 0

    def check_cooldown(self, now: datetime) -> Tuple[bool, str]:
        """
        Returns (ok_to_trade, reason_if_blocked).
        True = clear to open a new position.
        """
        if self.last_loss_time:
            elapsed = (now - self.last_loss_time).total_seconds()
            if elapsed < RISK_RULES["post_loss_cooldown_sec"]:
                wait = int(RISK_RULES["post_loss_cooldown_sec"] - elapsed)
                return False, f"post_loss_cooldown ({wait}s remaining)"

        if self.last_trade_time:
            elapsed = (now - self.last_trade_time).total_seconds()
            if elapsed < RISK_RULES["post_trade_cooldown_sec"]:
                wait = int(RISK_RULES["post_trade_cooldown_sec"] - elapsed)
                return False, f"post_trade_cooldown ({wait}s remaining)"

        return True, ""


def check_daily_limits(state: RiskState, now: datetime) -> Tuple[bool, str]:
    """
    Returns (can_open_new_trade, reason_if_blocked).
    Checks all daily hard limits.
    """
    if state.day_stopped:
        return False, "day_stopped"

    if state.hit_daily_loss:
        state.day_stopped = True
        return False, f"daily_max_loss (pnl={state.daily_pnl:.2f})"

    if state.consecutive_losses >= RISK_RULES["max_consecutive_losses"]:
        state.day_stopped = True
        return False, f"max_consecutive_losses ({state.consecutive_losses})"

    if state.trades_today >= RISK_RULES["max_trades_per_day"]:
        return False, f"max_trades_per_day ({state.trades_today})"

    ok, reason = state.check_cooldown(now)
    if not ok:
        return False, reason

    return True, ""


def minimum_rr_ratio(entry: float, stop: float, target: float) -> float:
    """Calculate the actual R:R ratio for a proposed trade."""
    risk = abs(entry - stop)
    reward = abs(target - entry)
    if risk == 0:
        return 0.0
    return reward / risk


def passes_rr_check(entry: float, stop: float, target: float) -> bool:
    """Trade is only valid if R:R >= 2:1."""
    return minimum_rr_ratio(entry, stop, target) >= RISK_RULES["profit_loss_ratio_min"]
