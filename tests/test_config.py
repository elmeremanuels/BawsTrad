from __future__ import annotations

import os
import pytest
from pydantic import ValidationError


def test_settings_loads_in_test_mode(monkeypatch):
    """Config loads without requiring API keys when TRADING_MODE=test."""
    monkeypatch.setenv("TRADING_MODE", "test")
    # Re-import to pick up monkeypatched env
    import importlib
    import src.config as cfg_module
    importlib.reload(cfg_module)
    assert cfg_module.settings.TRADING_MODE == "test"


def test_settings_crashes_without_alpaca_keys_in_paper_mode(monkeypatch):
    """Config must crash on boot if required secrets are missing in paper mode."""
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET", raising=False)
    monkeypatch.setenv("TRADING_MODE", "paper")

    from pydantic_settings import BaseSettings
    from pydantic import model_validator
    from typing import Literal

    # Instantiate directly to test validation
    with pytest.raises((ValueError, ValidationError)):
        from pydantic_settings import SettingsConfigDict
        from src.config import Settings
        s = Settings(TRADING_MODE="paper", ALPACA_API_KEY="", ALPACA_API_SECRET="")
        # model_validator should raise


def test_kill_switch_file_default():
    from src.config import settings
    assert settings.KILL_SWITCH_FILE  # Must be set
