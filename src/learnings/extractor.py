from __future__ import annotations

"""
Learning extractor — uses Claude to generate structured learnings from trade data.
Pre-processes trade aggregates via keyword stats first (Tier 1), then sends
compact structured input to Claude (Tier 3). Not called in real-time.
"""

import json
import logging
from datetime import date
from typing import List, Optional

from src.learnings.store import save_learning
from src.llm.claude import analyze
from src.storage.db import get_connection

logger = logging.getLogger(__name__)

_SYSTEM = """You are a trading performance analyst for a momentum day-trading bot.
You receive pre-aggregated trade statistics and must produce a structured learning.

Rules:
- Observations must be SPECIFIC and QUANTITATIVE (include n=, win_rate, dates)
- BAD: "biotech trades performed well"
- GOOD: "5/6 FDA-tier-A trades hit 2R when entered <8min after news (n=6)"
- suggested_change must be an actionable config YAML diff or None
- confidence: 0.0 (guessing) to 1.0 (strong statistical evidence, n>20)

Respond ONLY with valid JSON:
{
  "observation": "specific quantitative observation",
  "quantification": "n=X, metric=Y, comparison context",
  "confidence": 0.0-1.0,
  "suggested_change": "YAML snippet or null",
  "discord_message": "1-4 lines for Discord"
}"""


def _aggregate_trades(trades: List[dict]) -> dict:
    """Pre-process trade list into statistics for Claude input."""
    if not trades:
        return {}

    wins = [t for t in trades if (t.get("pnl_r") or 0) >= 0]
    losses = [t for t in trades if (t.get("pnl_r") or 0) < 0]

    by_setup: dict = {}
    for t in trades:
        st = t.get("setup_type", "unknown")
        if st not in by_setup:
            by_setup[st] = {"n": 0, "wins": 0, "r_sum": 0.0}
        by_setup[st]["n"] += 1
        if (t.get("pnl_r") or 0) >= 0:
            by_setup[st]["wins"] += 1
        by_setup[st]["r_sum"] += (t.get("pnl_r") or 0)

    return {
        "total": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(trades) if trades else 0,
        "avg_win_r": sum(t.get("pnl_r", 0) or 0 for t in wins) / len(wins) if wins else 0,
        "avg_loss_r": sum(t.get("pnl_r", 0) or 0 for t in losses) / len(losses) if losses else 0,
        "total_pnl": sum(t.get("pnl_dollars", 0) or 0 for t in trades),
        "by_setup": {
            st: {
                "n": v["n"],
                "win_rate": v["wins"] / v["n"] if v["n"] else 0,
                "avg_r": v["r_sum"] / v["n"] if v["n"] else 0,
            }
            for st, v in by_setup.items()
        },
        "exit_reasons": _count_field(trades, "exit_reason"),
        "tickers": list({t.get("ticker", "") for t in trades}),
    }


def _count_field(trades: List[dict], field: str) -> dict:
    counts: dict = {}
    for t in trades:
        v = t.get(field, "unknown") or "unknown"
        counts[v] = counts.get(v, 0) + 1
    return counts


def extract_trade_learning(trade: dict, trigger: str) -> Optional[str]:
    """Extract a learning from a single big-win or big-loss trade."""
    agg = _aggregate_trades([trade])
    prompt = f"""Trigger: {trigger}
Trade: {json.dumps(trade, indent=2)}
Stats: {json.dumps(agg, indent=2)}

Generate a learning from this single trade."""

    result = analyze(_SYSTEM, prompt, max_tokens=400)
    if not result:
        return None

    try:
        data = json.loads(result)
        learning_id = save_learning(
            trigger=trigger,
            scope="trade",
            observation=data["observation"],
            quantification=data["quantification"],
            confidence=data.get("confidence", 0.5),
            related_trade_ids=[trade.get("id", "")],
            suggested_change=data.get("suggested_change"),
        )
        return data.get("discord_message", "")
    except Exception as exc:
        logger.error("Failed to parse Claude learning: %s", exc)
        return None


def extract_period_learning(trades: List[dict], trigger: str, scope: str) -> Optional[str]:
    """Extract a learning from EOD/EOW/EOM aggregated trades."""
    if not trades:
        return None

    agg = _aggregate_trades(trades)
    prompt = f"""Trigger: {trigger} ({scope})
Aggregated stats: {json.dumps(agg, indent=2)}
Trade IDs: {[t.get('id','') for t in trades[:20]]}

Generate a learning from this period's trading."""

    result = analyze(_SYSTEM, prompt, max_tokens=600)
    if not result:
        return None

    try:
        data = json.loads(result)
        save_learning(
            trigger=trigger,
            scope=scope,
            observation=data["observation"],
            quantification=data["quantification"],
            confidence=data.get("confidence", 0.5),
            related_trade_ids=[t.get("id", "") for t in trades],
            suggested_change=data.get("suggested_change"),
        )
        return data.get("discord_message", "")
    except Exception as exc:
        logger.error("Failed to parse Claude period learning: %s", exc)
        return None
