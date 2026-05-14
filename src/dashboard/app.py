from __future__ import annotations

"""
Read-only dashboard — FastAPI + Jinja2 + HTMX.
Runs on localhost:8080. Protected by HTTP Basic Auth.
HARD RULE: no buttons that place orders or affect bot state.
"""

import secrets
import threading
from datetime import date
from pathlib import Path

import uvicorn
from fastapi import Depends, FastAPI, Request, status
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.exceptions import HTTPException

from src.config import settings
from src.storage.db import get_connection

BASE = Path(__file__).parent
app = FastAPI(docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE / "templates"))

_security = HTTPBasic()


def _require_auth(credentials: HTTPBasicCredentials = Depends(_security)) -> None:
    """
    Constant-time comparison to prevent timing attacks.
    Raises 401 if username or password is wrong.
    """
    correct_user = secrets.compare_digest(
        credentials.username.encode(), settings.DASHBOARD_USERNAME.encode()
    )
    correct_pass = secrets.compare_digest(
        credentials.password.encode(), settings.DASHBOARD_PASSWORD.encode()
    )
    if not (correct_user and correct_pass):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect credentials",
            headers={"WWW-Authenticate": "Basic realm='BawsTrad Dashboard'"},
        )


# ── DB helpers ────────────────────────────────────────────────────────────────

def _trades_today() -> list:
    today = date.today().isoformat()
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT ticker, side, shares, entry_price, exit_price, stop_price,
                   target_price, pnl_dollars, pnl_r, setup_type, exit_reason,
                   opened_at, closed_at
            FROM trades
            WHERE mode='paper' AND opened_at >= ?
            ORDER BY opened_at DESC
        """, (today,)).fetchall()
    return [dict(r) for r in rows]


def _open_positions() -> list:
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT ticker, shares, entry_price, stop_price, target_price,
                   setup_type, opened_at
            FROM trades
            WHERE mode='paper' AND closed_at IS NULL
            ORDER BY opened_at DESC
        """).fetchall()
    return [dict(r) for r in rows]


def _recent_logs(n: int = 20) -> list:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT occurred_at, event_type, message FROM bot_events ORDER BY id DESC LIMIT ?",
            (n,),
        ).fetchall()
    return [dict(r) for r in rows]


def _watchlist() -> list:
    today = date.today().isoformat()
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT ticker, price, gap_pct, rel_vol, float_shares, news_tier, quality_score
            FROM scan_results
            WHERE scanned_at >= ? AND passed_filter=1
            ORDER BY quality_score DESC LIMIT 10
        """, (today,)).fetchall()
    return [dict(r) for r in rows]


# ── Pages (all require auth) ──────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request, _: None = Depends(_require_auth)):
    trades = _trades_today()
    total_pnl = sum(t["pnl_dollars"] or 0 for t in trades if t["pnl_dollars"])
    wins = sum(1 for t in trades if (t["pnl_dollars"] or 0) > 0)
    return templates.TemplateResponse("index.html", {
        "request": request,
        "watchlist": _watchlist(),
        "positions": _open_positions(),
        "trades": trades,
        "logs": _recent_logs(),
        "total_pnl": total_pnl,
        "wins": wins,
        "losses": len([t for t in trades if (t["pnl_dollars"] or 0) < 0]),
    })


@app.get("/trades", response_class=HTMLResponse)
async def trades_page(request: Request, _: None = Depends(_require_auth)):
    trades = _trades_today()
    return templates.TemplateResponse("trades.html", {"request": request, "trades": trades})


@app.get("/watchlist", response_class=HTMLResponse)
async def watchlist_page(request: Request, _: None = Depends(_require_auth)):
    return templates.TemplateResponse("watchlist.html", {
        "request": request, "watchlist": _watchlist()
    })


# ── HTMX partials (all require auth) ─────────────────────────────────────────

@app.get("/partials/watchlist", response_class=HTMLResponse)
async def partial_watchlist(request: Request, _: None = Depends(_require_auth)):
    return templates.TemplateResponse("partials/watchlist.html",
                                      {"request": request, "watchlist": _watchlist()})


@app.get("/partials/positions", response_class=HTMLResponse)
async def partial_positions(request: Request, _: None = Depends(_require_auth)):
    return templates.TemplateResponse("partials/positions.html",
                                      {"request": request, "positions": _open_positions()})


@app.get("/partials/trades", response_class=HTMLResponse)
async def partial_trades(request: Request, _: None = Depends(_require_auth)):
    trades = _trades_today()
    total_pnl = sum(t["pnl_dollars"] or 0 for t in trades if t["pnl_dollars"])
    return templates.TemplateResponse("partials/trades.html",
                                      {"request": request, "trades": trades, "total_pnl": total_pnl})


@app.get("/partials/logs", response_class=HTMLResponse)
async def partial_logs(request: Request, _: None = Depends(_require_auth)):
    return templates.TemplateResponse("partials/logs.html",
                                      {"request": request, "logs": _recent_logs()})


# ── Server ────────────────────────────────────────────────────────────────────

def start_dashboard(host: str = "127.0.0.1", port: int = 8080) -> None:
    """Launch dashboard in a background daemon thread."""
    def _run():
        uvicorn.run(app, host=host, port=port, log_level="warning")
    t = threading.Thread(target=_run, daemon=True, name="dashboard")
    t.start()
