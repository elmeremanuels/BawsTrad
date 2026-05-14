from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest


def _init_tmp_db(tmp_db):
    """Helper: init schema in a temp DB file and return the db module patched to use it."""
    import src.storage.db as db_module
    with patch.object(db_module, "DB_PATH", tmp_db):
        # Patch get_connection to use our tmp path
        original_get_conn = db_module.get_connection

        def tmp_conn():
            conn = sqlite3.connect(tmp_db)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            return conn

        db_module.get_connection = tmp_conn
        db_module.init_schema()
        db_module.get_connection = original_get_conn
    return tmp_conn


def test_schema_creates_all_tables(tmp_path):
    """All required tables are created by init_schema."""
    tmp_db = tmp_path / "test.db"
    get_conn = _init_tmp_db(tmp_db)

    conn = get_conn()
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    conn.close()

    expected = {"scan_results", "news_items", "trades", "learnings", "briefings", "bot_events"}
    assert expected.issubset(tables), f"Missing tables: {expected - tables}"


def test_bot_event_insert(tmp_path):
    """Can write and read a bot_event row."""
    tmp_db = tmp_path / "test.db"
    get_conn = _init_tmp_db(tmp_db)

    with get_conn() as conn:
        conn.execute(
            "INSERT INTO bot_events (occurred_at, event_type, message) VALUES (?, ?, ?)",
            ("2024-01-01T00:00:00", "test_event", "hello"),
        )

    with get_conn() as conn:
        row = conn.execute("SELECT * FROM bot_events").fetchone()
        assert row["event_type"] == "test_event"
        assert row["message"] == "hello"
