from __future__ import annotations

"""
Data layer for the Streamlit dashboard.
Read-only from bot.db. Write-only to state/ files.
All DB queries are cached to avoid hammering SQLite during page refresh.
"""

import json
import sqlite3
import subprocess
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

ET = ZoneInfo("America/New_York")


def _bot_service_name() -> str:
    """Systemd service name for the trading bot. Configurable via env var."""
    import os
    return os.getenv("BOT_SERVICE_NAME", "trading-bot")


def _db_path() -> Path:
    """Resolve bot.db path: BOT_DB_PATH env > project root auto-detect."""
    import os
    env_path = os.getenv("BOT_DB_PATH", "")
    if env_path:
        return Path(env_path)
    # Walk up from this file to find bot.db
    here = Path(__file__).parent
    for _ in range(5):
        candidate = here / "bot.db"
        if candidate.exists():
            return candidate
        here = here.parent
    # Fallback: assume project root relative to cwd
    return Path("bot.db")


def _state_dir() -> Path:
    import os
    p = Path(os.getenv("STATE_DIR", "./state"))
    p.mkdir(parents=True, exist_ok=True)
    return p


def _kill_switch_path() -> Path:
    import os
    return Path(os.getenv("KILL_SWITCH_FILE", "/tmp/bot_killswitch"))


def _conn() -> sqlite3.Connection:
    db = _db_path()
    conn = sqlite3.connect(db, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


# ── Read functions (cached) ────────────────────────────────────────────────────

@st.cache_data(ttl=3)
def get_today_stats() -> dict:
    """Returns {trades, wins, losses, win_rate, gross_pnl, avg_r}."""
    today = date.today().isoformat()
    try:
        with _conn() as conn:
            rows = conn.execute("""
                SELECT pnl_dollars, pnl_r FROM trades
                WHERE closed_at >= ? AND closed_at IS NOT NULL
            """, (today,)).fetchall()
    except Exception:
        rows = []

    trades = [dict(r) for r in rows]
    wins   = [t for t in trades if (t["pnl_r"] or 0) >= 0]
    losses = [t for t in trades if (t["pnl_r"] or 0) < 0]
    total_pnl = sum(t["pnl_dollars"] or 0 for t in trades)
    avg_r = sum(t["pnl_r"] or 0 for t in trades) / len(trades) if trades else 0.0

    return {
        "trades": len(trades),
        "wins":   len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(trades) * 100 if trades else 0.0,
        "gross_pnl": total_pnl,
        "avg_r": avg_r,
    }


@st.cache_data(ttl=5)
def get_period_stats(days: int) -> dict:
    """Returns same shape as get_today_stats() for last N days."""
    since = (date.today() - timedelta(days=days)).isoformat()
    try:
        with _conn() as conn:
            rows = conn.execute("""
                SELECT pnl_dollars, pnl_r FROM trades
                WHERE closed_at >= ? AND closed_at IS NOT NULL
            """, (since,)).fetchall()
    except Exception:
        rows = []

    trades = [dict(r) for r in rows]
    wins   = [t for t in trades if (t["pnl_r"] or 0) >= 0]
    total_pnl = sum(t["pnl_dollars"] or 0 for t in trades)
    avg_r = sum(t["pnl_r"] or 0 for t in trades) / len(trades) if trades else 0.0

    return {
        "trades": len(trades),
        "wins":   len(wins),
        "losses": len(trades) - len(wins),
        "win_rate": len(wins) / len(trades) * 100 if trades else 0.0,
        "gross_pnl": total_pnl,
        "avg_r": avg_r,
    }


@st.cache_data(ttl=3)
def get_open_positions() -> list[dict]:
    """Open trades (closed_at IS NULL)."""
    try:
        with _conn() as conn:
            rows = conn.execute("""
                SELECT ticker, shares, entry_price, stop_price, target_price,
                       setup_type, opened_at
                FROM trades
                WHERE closed_at IS NULL
                ORDER BY opened_at DESC
            """).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


@st.cache_data(ttl=5)
def get_watchlist() -> list[dict]:
    """Today's scan results that passed the filter."""
    today = date.today().isoformat()
    try:
        with _conn() as conn:
            rows = conn.execute("""
                SELECT ticker, price, gap_pct, rel_vol, float_shares,
                       news_tier, quality_score
                FROM scan_results
                WHERE scanned_at >= ? AND passed_filter = 1
                ORDER BY quality_score DESC LIMIT 10
            """, (today,)).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


@st.cache_data(ttl=10)
def get_recent_trades(n: int = 20) -> list[dict]:
    """Last N closed trades."""
    try:
        with _conn() as conn:
            rows = conn.execute("""
                SELECT ticker, side, shares, entry_price, exit_price, stop_price,
                       target_price, pnl_dollars, pnl_r, setup_type, exit_reason,
                       opened_at, closed_at
                FROM trades
                WHERE closed_at IS NOT NULL
                ORDER BY closed_at DESC LIMIT ?
            """, (n,)).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


@st.cache_data(ttl=30)
def get_equity_curve(days: int = 30) -> pd.DataFrame:
    """Daily cumulative P&L for charting."""
    since = (date.today() - timedelta(days=days)).isoformat()
    try:
        with _conn() as conn:
            rows = conn.execute("""
                SELECT DATE(closed_at) as day,
                       SUM(pnl_dollars) as daily_pnl
                FROM trades
                WHERE closed_at >= ? AND closed_at IS NOT NULL
                GROUP BY DATE(closed_at)
                ORDER BY day
            """, (since,)).fetchall()
        df = pd.DataFrame([dict(r) for r in rows])
        if df.empty:
            return pd.DataFrame(columns=["day", "daily_pnl", "cumulative_pnl"])
        df["cumulative_pnl"] = df["daily_pnl"].cumsum()
        return df
    except Exception:
        return pd.DataFrame(columns=["day", "daily_pnl", "cumulative_pnl"])


@st.cache_data(ttl=30)
def get_setup_breakdown() -> list[dict]:
    """Per-setup trade stats for charts."""
    try:
        with _conn() as conn:
            rows = conn.execute("""
                SELECT setup_type,
                       COUNT(*) as count,
                       AVG(pnl_r) as avg_r,
                       SUM(CASE WHEN pnl_r >= 0 THEN 1 ELSE 0 END) as wins
                FROM trades
                WHERE closed_at IS NOT NULL AND setup_type IS NOT NULL
                GROUP BY setup_type
                ORDER BY count DESC
            """).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["win_rate"] = d["wins"] / d["count"] * 100 if d["count"] else 0
            result.append(d)
        return result
    except Exception:
        return []


@st.cache_data(ttl=30)
def get_learnings(status: Optional[str] = None) -> list[dict]:
    """Learnings from DB, optionally filtered by status."""
    try:
        with _conn() as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM learnings WHERE applied=? ORDER BY created_at DESC",
                    (1 if status == "active" else 0,)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM learnings ORDER BY created_at DESC LIMIT 50"
                ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


@st.cache_data(ttl=15)
def get_connectivity_health() -> dict:
    """Last API activity per service, inferred from bot_events."""
    services = {
        # scanner_cycle fires every ~25s during pre-market (Alpaca snapshots)
        # entry/force_close fire during trading hours
        "Alpaca":     ["entry", "startup", "force_close", "scanner_cycle"],
        # news_ingested fires per ticker during pre-market scanner + overnight sweeps
        "Finnhub":    ["news_ingested"],
        # startup/entry/force_close are the Discord touch-points
        "Discord":    ["startup", "entry", "force_close"],
        # news_classified fires per headline when Groq key is set
        "Groq":       ["news_classified"],
        # learning_extracted fires post-trade / EOD (rare, high-value)
        "Anthropic":  ["learning_extracted"],
        # briefing_fetched fires once at 08:30 ET per trading day
        "Perplexity": ["briefing_fetched"],
    }
    health = {}
    try:
        with _conn() as conn:
            for service, event_types in services.items():
                placeholders = ",".join("?" * len(event_types))
                row = conn.execute(f"""
                    SELECT MAX(occurred_at) as last_seen
                    FROM bot_events WHERE event_type IN ({placeholders})
                """, event_types).fetchone()
                last_seen = row["last_seen"] if row else None
                health[service] = last_seen
    except Exception:
        pass
    return health


@st.cache_data(ttl=5)
def get_bot_status() -> dict:
    """Bot status: systemd state, current mode, paused flag."""
    from src.engine.state import current_mode, is_paused, get_pause_reason

    # systemd status
    systemd_status = "unknown"
    try:
        result = subprocess.run(
            ["systemctl", "is-active", _bot_service_name()],
            capture_output=True, text=True, timeout=3
        )
        systemd_status = result.stdout.strip()  # active | inactive | failed | unknown
    except Exception:
        systemd_status = "local-dev"  # Not on Linux systemd host

    paused = is_paused()
    pause_reason = get_pause_reason() if paused else None

    try:
        mode = current_mode()
        mode_value = mode.value
    except Exception:
        mode_value = "unknown"

    kill_active = _kill_switch_path().exists()

    # Synthesised state — single field the UI branches on
    if kill_active:
        bot_state = "killed"
    elif systemd_status == "active":
        bot_state = "running"
    elif systemd_status == "failed":
        bot_state = "failed"
    elif systemd_status == "local-dev":
        bot_state = "local-dev"
    else:
        bot_state = "stopped"  # inactive / unknown

    return {
        "systemd_status": systemd_status,
        "mode": mode_value,
        "paused": paused,
        "pause_reason": pause_reason,
        "kill_switch_active": kill_active,
        "bot_state": bot_state,
    }


@st.cache_data(ttl=10)
def get_recent_events(n: int = 50) -> list[dict]:
    """Recent bot_events for the messages panel."""
    try:
        with _conn() as conn:
            rows = conn.execute("""
                SELECT occurred_at, event_type, message
                FROM bot_events
                ORDER BY id DESC LIMIT ?
            """, (n,)).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


@st.cache_data(ttl=30)
def get_account_value() -> float:
    """Last known account value from Alpaca (via bot_events startup log)."""
    try:
        with _conn() as conn:
            row = conn.execute("""
                SELECT message FROM bot_events
                WHERE event_type = 'startup'
                ORDER BY id DESC LIMIT 1
            """).fetchone()
        if row and "account=" in (row["message"] or ""):
            # Parse "Paper trading started, account=$25000.00"
            import re
            m = re.search(r"account=\$?([\d,]+\.?\d*)", row["message"])
            if m:
                return float(m.group(1).replace(",", ""))
    except Exception:
        pass
    return 25_000.0


# ── Pre-flight + start (no caching — live checks) ─────────────────────────────

def preflight_check() -> list[dict]:
    """
    Run pre-flight checks before starting the bot.
    Returns list of {name, ok, detail} — all must be ok to enable start.
    """
    import os
    import httpx  # already a transitive dep via streamlit
    checks: list[dict] = []

    # 1. .env present
    env_path = Path(".env")
    env_exists = env_path.exists()
    checks.append({
        "name": ".env present",
        "ok": env_exists,
        "detail": str(env_path.resolve()) if env_exists else "Not found — create from .env.example",
    })

    # 2. Required Alpaca keys inside .env
    if env_exists:
        env_text = env_path.read_text()
        required = ["ALPACA_API_KEY", "ALPACA_API_SECRET"]
        missing = [k for k in required if f"{k}=" not in env_text]
        keys_ok = not missing
        checks.append({
            "name": "Alpaca API keys",
            "ok": keys_ok,
            "detail": "Both present" if keys_ok else f"Missing: {', '.join(missing)}",
        })
    else:
        checks.append({"name": "Alpaca API keys", "ok": False, "detail": ".env not found"})

    # 3. Alpaca reachable (HTTPS, no auth needed for connectivity check)
    try:
        r = httpx.get("https://paper-api.alpaca.markets", timeout=5, follow_redirects=True)
        ok = r.status_code < 500
        checks.append({"name": "Alpaca reachable", "ok": ok, "detail": f"HTTP {r.status_code}"})
    except Exception as exc:
        checks.append({"name": "Alpaca reachable", "ok": False, "detail": str(exc)[:80]})

    # 4. Discord reachable (optional — only if webhook is configured)
    import os as _os
    discord_url = _os.getenv("DISCORD_WEBHOOK_URL", "")
    if discord_url:
        try:
            r = httpx.get("https://discord.com", timeout=5, follow_redirects=True)
            ok = r.status_code < 500
            checks.append({"name": "Discord reachable", "ok": ok, "detail": f"HTTP {r.status_code}"})
        except Exception as exc:
            checks.append({"name": "Discord reachable", "ok": False, "detail": str(exc)[:80]})
    else:
        checks.append({
            "name": "Discord webhook",
            "ok": True,
            "detail": "Not configured (optional — alerts disabled)",
        })

    return checks


def start_bot() -> dict:
    """
    Clear the kill switch (if present) and start trading-bot via systemctl.
    Returns {ok, kill_removed, mode, error}.
    Audit-logged to state/last_dashboard_action.json.
    """
    kill_removed = False

    # 1. Clear kill switch
    ks_path = _kill_switch_path()
    if ks_path.exists():
        ks_path.unlink(missing_ok=True)
        kill_removed = True

    # 2. Read current TRADING_MODE from .env
    mode = "unknown"
    try:
        for line in Path(".env").read_text().splitlines():
            if line.startswith("TRADING_MODE="):
                mode = line.split("=", 1)[1].strip()
    except Exception:
        pass

    # 3. systemctl start
    try:
        result = subprocess.run(
            ["sudo", "systemctl", "start", _bot_service_name()],
            capture_output=True, text=True, timeout=15,
        )
        ok = result.returncode == 0
        error: Optional[str] = result.stderr.strip() or None if not ok else None
    except Exception as exc:
        ok = False
        error = str(exc)

    log_dashboard_action("manual_start", {
        "mode": mode,
        "killswitch_removed": kill_removed,
        "result": "success" if ok else "failure",
        "error": error or "",
    })
    return {"ok": ok, "kill_removed": kill_removed, "mode": mode, "error": error}


def get_journalctl_tail(n: int = 20) -> str:
    """Return the last N lines from the bot's systemd journal (for crash display)."""
    try:
        result = subprocess.run(
            ["journalctl", "-u", _bot_service_name(), f"-n{n}", "--no-pager", "--output=short"],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip() or "(no log output)"
    except Exception as exc:
        return f"(could not read journal: {exc})"


# ── Write functions (no caching) ──────────────────────────────────────────────

def request_pause(reason: str) -> None:
    """Create STATE_DIR/bot_paused.flag and write reason."""
    sd = _state_dir()
    (sd / "bot_paused.flag").touch()
    (sd / "pause_reason.txt").write_text(reason.strip())
    log_dashboard_action("pause", {"reason": reason})


def request_resume() -> None:
    """Remove STATE_DIR/bot_paused.flag and reason file."""
    sd = _state_dir()
    (sd / "bot_paused.flag").unlink(missing_ok=True)
    (sd / "pause_reason.txt").unlink(missing_ok=True)
    log_dashboard_action("resume", {})


def trigger_kill_switch() -> None:
    """Touch /tmp/bot_killswitch (or configured path)."""
    _kill_switch_path().touch()
    log_dashboard_action("kill_switch", {"path": str(_kill_switch_path())})


def clear_kill_switch() -> None:
    """Remove kill switch file."""
    _kill_switch_path().unlink(missing_ok=True)
    log_dashboard_action("kill_switch_cleared", {})


def switch_trading_mode(new_mode: str) -> bool:
    """
    Write new TRADING_MODE to .env and restart the bot via systemctl.
    Returns True on success. new_mode must be 'paper' or 'live'.
    """
    assert new_mode in ("paper", "live"), "Invalid mode"
    env_path = Path(".env")
    if env_path.exists():
        lines = env_path.read_text().splitlines()
        new_lines = []
        found = False
        for line in lines:
            if line.startswith("TRADING_MODE="):
                new_lines.append(f"TRADING_MODE={new_mode}")
                found = True
            else:
                new_lines.append(line)
        if not found:
            new_lines.append(f"TRADING_MODE={new_mode}")
        env_path.write_text("\n".join(new_lines) + "\n")

    try:
        subprocess.run(
            ["sudo", "systemctl", "restart", _bot_service_name()],
            check=True, timeout=10
        )
        log_dashboard_action("mode_switch", {"new_mode": new_mode})
        return True
    except Exception as e:
        log_dashboard_action("mode_switch_failed", {"new_mode": new_mode, "error": str(e)})
        return False


def log_dashboard_action(action: str, details: dict) -> None:
    """Append to STATE_DIR/last_dashboard_action.json (audit trail)."""
    sd = _state_dir()
    action_file = sd / "last_dashboard_action.json"

    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action": action,
        "details": details,
    }

    history = []
    if action_file.exists():
        try:
            history = json.loads(action_file.read_text())
            if not isinstance(history, list):
                history = []
        except Exception:
            history = []

    history.append(entry)
    history = history[-100:]  # Keep last 100 actions
    action_file.write_text(json.dumps(history, indent=2))


def get_dashboard_actions(n: int = 20) -> list[dict]:
    """Read last N dashboard actions from audit log."""
    action_file = _state_dir() / "last_dashboard_action.json"
    if not action_file.exists():
        return []
    try:
        history = json.loads(action_file.read_text())
        return list(reversed(history[-n:]))
    except Exception:
        return []
