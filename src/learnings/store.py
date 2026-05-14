from __future__ import annotations

"""SQLite CRUD for the Learning data model."""

import json
from datetime import datetime, timedelta
from typing import List, Optional
from uuid import uuid4

from src.storage.db import get_connection


def save_learning(
    trigger: str,
    scope: str,
    observation: str,
    quantification: str,
    confidence: float,
    related_trade_ids: Optional[List[str]] = None,
    suggested_change: Optional[str] = None,
    config_diff: Optional[dict] = None,
) -> str:
    learning_id = str(uuid4())
    now = datetime.utcnow()
    evaluation_date = (now + timedelta(days=14)).isoformat()

    with get_connection() as conn:
        conn.execute("""
            INSERT INTO learnings
            (id, created_at, trigger, scope, related_trade_ids, observation,
             quantification, confidence, suggested_change, config_diff, evaluation_date)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            learning_id, now.isoformat(), trigger, scope,
            json.dumps(related_trade_ids or []),
            observation, quantification, confidence,
            suggested_change,
            json.dumps(config_diff) if config_diff else None,
            evaluation_date,
        ))
    return learning_id


def get_active_learnings(limit: int = 20) -> List[dict]:
    """Most recent non-invalidated learnings with high confidence."""
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT * FROM learnings
            WHERE (outcome IS NULL OR outcome != 'invalidated')
            AND confidence >= 0.5
            ORDER BY created_at DESC LIMIT ?
        """, (limit,)).fetchall()
    return [dict(r) for r in rows]


def mark_applied(learning_id: str) -> None:
    now = datetime.utcnow().isoformat()
    with get_connection() as conn:
        conn.execute(
            "UPDATE learnings SET applied=1, applied_at=? WHERE id=?",
            (now, learning_id),
        )


def mark_outcome(learning_id: str, outcome: str, notes: str = "") -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE learnings SET outcome=?, outcome_notes=? WHERE id=?",
            (outcome, notes, learning_id),
        )
