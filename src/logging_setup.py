from __future__ import annotations

import logging
import sys
from pathlib import Path

import structlog


def _redact_secrets(logger, method, event_dict):  # noqa: ANN
    """Strip any API keys / secrets from log output."""
    sensitive_patterns = ("api_key", "api_secret", "webhook", "token", "password", "secret")
    for key in list(event_dict.keys()):
        if any(p in key.lower() for p in sensitive_patterns):
            event_dict[key] = "***REDACTED***"
    return event_dict


def configure_logging(log_level: str = "INFO", log_file: str = "logs/bot.log") -> None:
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)

    level = getattr(logging, log_level.upper(), logging.INFO)

    # File handler — JSON structured
    file_handler = logging.FileHandler(log_file)
    file_handler.setLevel(level)

    # Console handler — human readable
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)

    logging.basicConfig(
        level=level,
        handlers=[file_handler, console_handler],
        format="%(message)s",
    )

    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.TimeStamper(fmt="iso"),
            _redact_secrets,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # File: JSON, Console: coloured key=value
    file_formatter = structlog.stdlib.ProcessorFormatter(
        processor=structlog.processors.JSONRenderer(),
    )
    console_formatter = structlog.stdlib.ProcessorFormatter(
        processor=structlog.dev.ConsoleRenderer(colors=True),
    )

    file_handler.setFormatter(file_formatter)
    console_handler.setFormatter(console_formatter)

    # Silence noisy third-party loggers that can leak URLs with embedded API keys
    for noisy in ("httpx", "httpcore", "websockets.client", "websockets.server",
                  "uvicorn.access", "uvicorn.error"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
