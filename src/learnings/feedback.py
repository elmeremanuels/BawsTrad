from __future__ import annotations

"""
Learning feedback — read-only context injection.
Learnings are fed back into the pre-market briefing and surfaced to the human via Discord.
The decision engine NEVER reads learnings directly. Changes require human confirmation.
"""

from src.learnings.store import get_active_learnings


def get_briefing_context(limit: int = 5) -> list:
    """Top learnings for pre-market briefing context."""
    return get_active_learnings(limit=limit)


def get_suggested_changes() -> list:
    """Learnings with a suggested_change that haven't been applied yet."""
    from src.storage.db import get_connection
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT * FROM learnings
            WHERE suggested_change IS NOT NULL AND applied=0
            ORDER BY confidence DESC, created_at DESC LIMIT 10
        """).fetchall()
    return [dict(r) for r in rows]
