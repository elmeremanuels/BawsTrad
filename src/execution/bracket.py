from __future__ import annotations

"""
Bracket order builder — assembles the correct prices for an Alpaca bracket order.
Entry (market) → take_profit (limit) → stop_loss (stop-limit).
"""

from src.data.alpaca_client import BracketOrder
from src.engine.risk import RISK_RULES


def build_bracket(
    symbol: str,
    qty: int,
    entry_price: float,
    stop_price: float,
    rr_ratio: float = RISK_RULES["profit_loss_ratio_min"],
) -> BracketOrder:
    """
    Build a bracket order. Target = entry + (risk × rr_ratio).
    Stop limit = stop - 1 cent (prevent partial fills at a worse price).
    """
    risk = abs(entry_price - stop_price)
    target_price = entry_price + risk * rr_ratio
    stop_limit = max(0.01, stop_price - 0.01)

    return BracketOrder(
        symbol=symbol,
        qty=qty,
        entry_price=entry_price,
        take_profit_price=round(target_price, 2),
        stop_loss_price=round(stop_price, 2),
        stop_limit_price=round(stop_limit, 2),
    )
