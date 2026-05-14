from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_shutdown_callback: Optional[callable] = None


def register_shutdown(callback) -> None:
    global _shutdown_callback
    _shutdown_callback = callback


async def watch_kill_switch(kill_switch_file: str, check_interval_sec: int = 5) -> None:
    """
    Polls for the kill switch file every `check_interval_sec` seconds.
    When found: invokes the registered shutdown callback and removes the file.
    """
    path = Path(kill_switch_file)
    logger.info("Kill switch watching: %s (interval=%ds)", kill_switch_file, check_interval_sec)

    while True:
        await asyncio.sleep(check_interval_sec)
        if path.exists():
            logger.critical("KILL SWITCH ACTIVATED — file found: %s", kill_switch_file)
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass
            if _shutdown_callback:
                await _shutdown_callback(reason="kill_switch")
            return
