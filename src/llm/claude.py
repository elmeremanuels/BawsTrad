from __future__ import annotations

"""
Claude API (Anthropic) — premium tier for learning extraction and analysis.
Used for: big-win/loss post-mortems, EOD/EOW/EOM journals, backtest interpretation.

Cost: ~$3-15/1M tokens. Called max ~10x/day, always with structured pre-processed input.
"""

import logging
from typing import Optional

from src.config import settings

logger = logging.getLogger(__name__)

_MODEL = "claude-opus-4-5"


def analyze(system_prompt: str, user_content: str, max_tokens: int = 1024) -> Optional[str]:
    """
    Single Claude API call. Returns the text response or None on failure.
    """
    if not settings.ANTHROPIC_API_KEY:
        logger.warning("ANTHROPIC_API_KEY not set — skipping Claude call")
        return None

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
        msg = client.messages.create(
            model=_MODEL,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": user_content}],
            system=system_prompt,
        )
        return msg.content[0].text
    except Exception as exc:
        logger.error("Claude API call failed: %s", exc)
        return None
