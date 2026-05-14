from __future__ import annotations

import os
import pytest

# Ensure tests always run in test mode (no secrets required)
os.environ.setdefault("TRADING_MODE", "test")
os.environ.setdefault("ALPACA_API_KEY", "test_key")
os.environ.setdefault("ALPACA_API_SECRET", "test_secret")
