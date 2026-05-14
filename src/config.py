from __future__ import annotations

import os
import stat
import warnings
from pathlib import Path
from typing import Literal

import yaml
from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _load_yaml() -> dict:
    yaml_path = Path(__file__).parent.parent / "config.yaml"
    if yaml_path.exists():
        with open(yaml_path) as f:
            return yaml.safe_load(f) or {}
    return {}


_yaml = _load_yaml()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # === Secrets (required at boot — crash if missing in non-test mode) ===
    ALPACA_API_KEY: str = ""
    ALPACA_API_SECRET: str = ""
    ALPACA_BASE_URL: str = "https://paper-api.alpaca.markets"
    ALPACA_DATA_URL: str = "https://data.alpaca.markets"
    MASSIVE_API_KEY: str = ""
    FINNHUB_API_KEY: str = ""
    GROQ_API_KEY: str = ""
    ANTHROPIC_API_KEY: str = ""
    PERPLEXITY_API_KEY: str = ""
    DISCORD_WEBHOOK_URL: str = ""

    # === Dashboard (legacy FastAPI — kept for reference) ===
    DASHBOARD_USERNAME: str = "bawstrad"
    DASHBOARD_PASSWORD: str = "Tr4d-B@ws-K9#mX"

    # === Runtime ===
    TRADING_MODE: Literal["paper", "live", "backtest", "test"] = "paper"
    LOG_LEVEL: str = "INFO"
    KILL_SWITCH_FILE: str = "/tmp/bot_killswitch"

    # === State files (shared between bot and Streamlit dashboard) ===
    # On prod Hetzner: /home/trader/BawsTrad/state
    # On dev (local):  ./state
    STATE_DIR: str = "./state"
    BOT_DB_PATH: str = ""  # Empty = auto-detect (project root bot.db)

    @model_validator(mode="after")
    def check_required_secrets(self) -> "Settings":
        if self.TRADING_MODE == "test":
            return self  # Skip checks in test mode

        required: dict[str, str] = {
            "ALPACA_API_KEY": self.ALPACA_API_KEY,
            "ALPACA_API_SECRET": self.ALPACA_API_SECRET,
        }
        missing = [k for k, v in required.items() if not v]
        if missing:
            raise ValueError(
                f"Missing required secrets: {', '.join(missing)}. "
                "Set them in your .env file."
            )
        return self

    @field_validator("LOG_LEVEL")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        valid = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = v.upper()
        if upper not in valid:
            raise ValueError(f"LOG_LEVEL must be one of {valid}")
        return upper


def _check_env_file_permissions() -> None:
    env_path = Path(".env")
    if not env_path.exists():
        return
    mode = stat.S_IMODE(os.stat(env_path).st_mode)
    if mode & 0o077:  # Group or other has any permission
        warnings.warn(
            f".env file permissions are {oct(mode)} — should be 600 (owner read/write only). "
            "Run: chmod 600 .env",
            stacklevel=2,
        )


def load_settings() -> Settings:
    _check_env_file_permissions()
    return Settings()


# Loaded once, used everywhere
settings = load_settings()
