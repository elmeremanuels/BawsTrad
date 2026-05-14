from __future__ import annotations

import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


async def post_message(webhook_url: str, message: str, embed: Optional[dict] = None) -> bool:
    """Post a message to a Discord webhook. Returns True on success."""
    if not webhook_url:
        logger.warning("DISCORD_WEBHOOK_URL not set — skipping alert")
        return False

    payload: dict = {"content": message}
    if embed:
        payload["embeds"] = [embed]

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(webhook_url, json=payload)
            r.raise_for_status()
            return True
    except Exception as exc:
        logger.error("Discord webhook failed: %s", exc)
        return False
