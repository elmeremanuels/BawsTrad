#!/usr/bin/env python3
"""
Initialize (or re-initialize) the SQLite schema.
Idempotent: safe to run multiple times.

Usage: uv run python scripts/seed_db.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.storage.db import init_schema

if __name__ == "__main__":
    init_schema()
    print("Database ready.")
