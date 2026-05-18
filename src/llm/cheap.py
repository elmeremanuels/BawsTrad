from __future__ import annotations

"""
Groq (Llama 3.3 70B) — real-time news classification for Phase 2.
Falls back to keyword classifier if Groq is unavailable.

Cost: ~$0.00001 per classification. Zero overhead in the hot path.
"""

import json
import logging
from typing import Tuple

from src.config import settings
from src.news.classifier import classify_headline as keyword_classify

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """You are a financial news classifier for a momentum day-trading bot.
Classify each headline into exactly one tier and category. Respond ONLY with valid JSON.

Tiers:
- A: FDA approval/breakthrough, major acquisition/merger target, major government contract,
     major tech partnership (Microsoft/Apple/Google/Amazon/Nvidia)
- B: analyst upgrade, partnership, earnings beat, revenue beat, guidance raise,
     insider buying, short squeeze setup, clinical trial results
- C: dilution/secondary offering, going concern, delisting, reverse split,
     earnings miss, downgrade, vague PR, bankruptcy

JSON schema: {"tier": "A"|"B"|"C", "category": "string", "sentiment": -1.0..1.0, "reasoning": "max 20 words"}"""


def classify_with_llm(headline: str, ticker: str) -> Tuple[str, str]:
    """
    Classify a news headline using Groq Llama.
    Returns (tier, category). Falls back to keyword classifier on error.
    """
    if not settings.GROQ_API_KEY:
        return keyword_classify(headline, ticker)

    try:
        from groq import Groq
        client = Groq(api_key=settings.GROQ_API_KEY)
        resp = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": f"Ticker: {ticker}\nHeadline: {headline}"},
            ],
            response_format={"type": "json_object"},
            max_tokens=100,
            temperature=0,
        )
        data = json.loads(resp.choices[0].message.content)
        tier = data.get("tier", "B")
        category = data.get("category", "general")
        if tier not in ("A", "B", "C"):
            tier = "B"

        # Log connectivity heartbeat for dashboard health panel
        try:
            from datetime import datetime
            from src.storage.db import get_connection
            with get_connection() as _c:
                _c.execute(
                    "INSERT INTO bot_events (occurred_at, event_type, message) VALUES (?, ?, ?)",
                    (datetime.utcnow().isoformat(), "news_classified",
                     f"{ticker}: [{tier}] {category}"),
                )
        except Exception:
            pass  # non-fatal

        return tier, category
    except Exception as exc:
        logger.warning("Groq classification failed (%s) — falling back to keyword", exc)
        return keyword_classify(headline, ticker)
