from __future__ import annotations

"""
Position sizing — exact Warrior Trading formula.
Pure math, no external calls.
"""


def calculate_position_size(
    account_value: float,
    entry_price: float,
    stop_price: float,
    risk_per_trade_pct: float = 0.01,
    max_position_pct: float = 0.25,
) -> int:
    """
    Risk-based position sizing.

    risk_per_share = entry - stop
    max_dollars_at_risk = account * 1%
    shares = max_dollars / risk_per_share

    Capped at 25% of account value.
    Returns 0 if the setup is invalid (stop >= entry).
    """
    risk_per_share = abs(entry_price - stop_price)
    if risk_per_share == 0:
        return 0

    max_dollars_at_risk = account_value * risk_per_trade_pct
    shares = int(max_dollars_at_risk / risk_per_share)

    # Cap at max_position_pct of account
    max_shares_by_value = int((account_value * max_position_pct) / entry_price)
    shares = min(shares, max_shares_by_value)

    return max(0, shares)
