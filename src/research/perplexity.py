from __future__ import annotations

"""
Pre-market briefing via Perplexity Sonar API.
Called once per trading day at 08:30 EST.
Cost: ~$0.05–0.15 per call. Failure is non-fatal — bot continues without briefing.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import date
from typing import List, Optional
from uuid import uuid4

import httpx

from src.config import settings
from src.storage.db import get_connection

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """You are a pre-market briefing analyst for a momentum day trading bot focused on
US small-cap stocks ($1-$20, low float, news catalysts). The bot follows the Warrior Trading methodology.

Your output must be valid JSON matching this exact schema:
{
  "market_context": "1-2 sentences about overall market mood and key events today",
  "watchlist_priorities": [
    {
      "ticker": "TICKER",
      "catalyst_summary": "what is the news in 1 sentence",
      "catalyst_tier": "A|B|C",
      "priority": "high|medium|low|avoid",
      "reasoning": "1 sentence why"
    }
  ],
  "sector_watch": "any sector showing strength or weakness today",
  "scheduled_events": ["list of scheduled market events today"],
  "avoid_today": "any reason to be cautious or sit out (FOMC, holiday, etc.)"
}

Be concise. No fluff. If a ticker has dilution risk or vague catalyst, mark it avoid."""


@dataclass
class Briefing:
    id: str = field(default_factory=lambda: str(uuid4()))
    briefing_date: str = ""
    market_context: str = ""
    watchlist_priorities: List[dict] = field(default_factory=list)
    sector_watch: str = ""
    scheduled_events: List[str] = field(default_factory=list)
    avoid_today: str = ""
    raw_json: str = ""


def get_premarket_briefing(
    briefing_date: date,
    scanner_tickers: List[str],
    active_learnings: Optional[List[dict]] = None,
) -> Optional[Briefing]:
    """
    Call Perplexity Sonar to get pre-market briefing.
    Returns Briefing object, or None if Perplexity is unavailable.
    """
    if not settings.PERPLEXITY_API_KEY:
        logger.warning("PERPLEXITY_API_KEY not set — skipping briefing")
        return None

    tickers_str = ", ".join(scanner_tickers[:20]) if scanner_tickers else "None identified yet"
    learnings_str = ""
    if active_learnings:
        top5 = active_learnings[:5]
        learnings_str = "\n".join(
            f"- {l.get('observation', '')} (confidence={l.get('confidence', 0):.0%})"
            for l in top5
        )

    user_prompt = f"""Date: {briefing_date.isoformat()}

Pre-market watchlist candidates (from scanner, unverified): {tickers_str}

Active strategy learnings from last 30 days:
{learnings_str if learnings_str else "None yet (early in paper trading phase)"}

Please provide your briefing. Focus on which tickers have genuine catalysts and which to avoid."""

    try:
        resp = httpx.post(
            "https://api.perplexity.ai/chat/completions",
            headers={
                "Authorization": f"Bearer {settings.PERPLEXITY_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "sonar-pro",
                "messages": [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                "search_recency_filter": "day",
            },
            timeout=30.0,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]

        # Parse JSON (Perplexity sometimes wraps in markdown)
        if "```" in content:
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]

        data = json.loads(content.strip())
        briefing = Briefing(
            briefing_date=briefing_date.isoformat(),
            market_context=data.get("market_context", ""),
            watchlist_priorities=data.get("watchlist_priorities", []),
            sector_watch=data.get("sector_watch", ""),
            scheduled_events=data.get("scheduled_events", []),
            avoid_today=data.get("avoid_today", ""),
            raw_json=content,
        )
        _store_briefing(briefing)
        logger.info("Pre-market briefing ready: %s", briefing.market_context[:80])
        return briefing

    except Exception as exc:
        logger.error("Perplexity briefing failed: %s — continuing without it", exc)
        return None


def _store_briefing(b: Briefing) -> None:
    from datetime import datetime
    with get_connection() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO briefings
            (id, created_at, briefing_date, market_context, sector_watch,
             scheduled_events, avoid_today, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            b.id, datetime.utcnow().isoformat(), b.briefing_date,
            b.market_context, b.sector_watch,
            json.dumps(b.scheduled_events), b.avoid_today, b.raw_json,
        ))


def get_todays_briefing() -> Optional[dict]:
    today = date.today().isoformat()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM briefings WHERE briefing_date=? ORDER BY created_at DESC LIMIT 1",
            (today,),
        ).fetchone()
    return dict(row) if row else None
