from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest


@pytest.mark.asyncio
async def test_kill_switch_triggers_callback(tmp_path):
    """Placing the kill switch file causes the watcher to fire the callback."""
    ks_file = tmp_path / "killswitch"
    called_with = []

    async def mock_shutdown(reason: str):
        called_with.append(reason)

    from src.kill_switch import register_shutdown, watch_kill_switch
    register_shutdown(mock_shutdown)

    async def trigger_after_delay():
        await asyncio.sleep(0.05)
        ks_file.touch()

    await asyncio.gather(
        watch_kill_switch(str(ks_file), check_interval_sec=0),
        trigger_after_delay(),
    )

    assert called_with == ["kill_switch"]
    assert not ks_file.exists()  # File was removed after trigger
