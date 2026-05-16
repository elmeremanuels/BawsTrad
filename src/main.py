from __future__ import annotations

"""
Trading bot entrypoint.

Usage:
  uv run python -m src.main --mode=test
  uv run python -m src.main --mode=paper [--dashboard]
  uv run python -m src.main --mode=backtest
"""

import argparse
import asyncio
import logging
import sys
from collections import defaultdict, deque
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

import structlog
import yaml

from src.alerts.discord import post_message
from src.config import settings
from src.engine.state import current_mode
from src.kill_switch import register_shutdown, watch_kill_switch
from src.logging_setup import configure_logging
from src.storage.db import get_connection, init_schema

logger = structlog.get_logger(__name__)
ET = ZoneInfo("America/New_York")


# ─── Shared bot state (read by dashboard, written by all components) ──────────

class BotState:
    def __init__(self) -> None:
        self.watchlist: list = []
        self.candle_buffer: Dict[str, deque] = defaultdict(lambda: deque(maxlen=50))
        self.account_value: float = 25_000.0
        self.risk_state: Optional[object] = None
        self.briefing: Optional[dict] = None
        self.ws_stop: asyncio.Event = asyncio.Event()

_state = BotState()


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _log_event(event_type: str, message: str) -> None:
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO bot_events (occurred_at, event_type, message) VALUES (?, ?, ?)",
            (datetime.utcnow().isoformat(), event_type, message),
        )


def _now_et() -> datetime:
    return datetime.now(ET)


def _in_trading_window(now: Optional[datetime] = None) -> bool:
    """True when the state machine is in ACTIVE_TRADING mode.
    Reads window_start/window_end from config.yaml via engine.state — NOT hardcoded.
    Previously hardcoded to 09:30-11:30 which silently dropped all bars after 11:30.
    """
    from src.engine.state import Mode, current_mode
    return current_mode(now=now or _now_et()) is Mode.ACTIVE_TRADING


def _after_force_close(now: Optional[datetime] = None) -> bool:
    t = (now or _now_et()).astimezone(ET)
    return (t.hour, t.minute) >= (15, 55)


def _load_account_value() -> float:
    try:
        from src.data.alpaca_client import get_account
        acct = get_account()
        return float(acct.get("portfolio_value", 25_000))
    except Exception as exc:
        logger.warning("Could not fetch account value: %s", exc)
        return 25_000.0


# ─── Shutdown ─────────────────────────────────────────────────────────────────

async def shutdown(reason: str = "normal") -> None:
    log = structlog.get_logger("shutdown")
    log.warning("Bot shutting down", reason=reason)
    _state.ws_stop.set()

    if reason in ("kill_switch", "paper_mode_exit"):
        try:
            from src.execution.paper import emergency_close_all
            emergency_close_all()
            _log_event("emergency_close", "All orders cancelled and positions closed")
        except Exception as exc:
            log.error("Emergency close failed: %s", exc)

    msg = f"🔴 Bot stopped | reason: `{reason}` | {datetime.now(timezone.utc).isoformat()}"
    if settings.DISCORD_WEBHOOK_URL:
        await post_message(settings.DISCORD_WEBHOOK_URL, msg)

    log.info("Shutdown complete")


# ─── Test mode ───────────────────────────────────────────────────────────────

async def run_test_mode() -> None:
    log = structlog.get_logger("main")
    log.info("Config loaded", trading_mode=settings.TRADING_MODE)

    with get_connection() as conn:
        conn.execute(
            "INSERT INTO bot_events (occurred_at, event_type, message) VALUES (?, ?, ?)",
            (datetime.utcnow().isoformat(), "boot_test", "Phase 0/2 test run"),
        )
    log.info("Test event written to SQLite")

    if settings.DISCORD_WEBHOOK_URL:
        ok = await post_message(settings.DISCORD_WEBHOOK_URL, "🟢 Bot booted — test mode OK")
        log.info("Discord webhook %s", "OK" if ok else "FAILED")
    else:
        log.info("DISCORD_WEBHOOK_URL not set — skipping Discord test")

    log.info("Test mode complete — all checks passed")


# ─── Pre-market scanner ───────────────────────────────────────────────────────

async def run_premarket_scanner(universe_tickers: List[str]) -> None:
    """Poll Alpaca snapshots every 30s to build watchlist until scanner_stop_hour ET."""
    from src.data.alpaca_client import get_snapshots
    from src.scanner.criteria import NewsCatalyst, TickerSnapshot, passes_stock_selection, quality_score
    from src.news.ingest import ingest_news_for_ticker, get_best_catalyst_today
    from src.storage.db import get_connection
    from uuid import uuid4

    log = structlog.get_logger("scanner")

    # Read scanner_stop_hour from config.yaml (default 10 ET)
    _stop_hour = 10
    try:
        with open("config.yaml") as _f:
            _stop_hour = int((yaml.safe_load(_f) or {}).get("scanner", {}).get("scanner_stop_hour", 10))
    except Exception:
        pass
    log.info("Pre-market scanner started", tickers=len(universe_tickers), stop_hour=_stop_hour)

    # Track which tickers have already had news fetched this session
    # to avoid hammering Finnhub on every 30s cycle
    _news_fetched: set = set()
    _cycle = 0

    while not _state.ws_stop.is_set():
        now = _now_et()
        if now.hour >= _stop_hour:
            log.info("scanner: stopping", stop_hour=_stop_hour, cycles_run=_cycle)
            break

        try:
            snaps = get_snapshots(universe_tickers)
            candidates = []
            all_scores = []  # Track all scores for diagnostics

            for ticker, snap in snaps.items():
                prev_close = (snap.get("prevDailyBar") or {}).get("c", 0)
                daily = snap.get("dailyBar") or {}
                price = daily.get("c") or daily.get("o") or 0

                if prev_close <= 0 or price <= 0:
                    continue

                gap_pct = (price - prev_close) / prev_close * 100.0

                # News — fetch once per ticker per session (Finnhub rate limit)
                # If fetch fails, fall back to any catalyst already in DB
                if ticker not in _news_fetched:
                    try:
                        ingest_news_for_ticker(ticker)
                        _news_fetched.add(ticker)
                    except Exception as exc:
                        log.debug("scanner: news fetch skipped", ticker=ticker, error=str(exc))
                        _news_fetched.add(ticker)  # Don't retry failed tickers this cycle

                best = get_best_catalyst_today(ticker)
                catalyst = None
                if best:
                    catalyst = NewsCatalyst(
                        tier=best["tier"],
                        category=best["category"],
                        headline=best["headline"],
                    )

                float_shares = _state._float_map.get(ticker, 15_000_000)

                s = TickerSnapshot(
                    ticker=ticker,
                    price=price,
                    percent_change_today=gap_pct,
                    relative_volume=5.0,  # Approximation pre-market
                    float_shares=float_shares,
                    news_catalyst=catalyst,
                )

                # Log why tickers are rejected (debug only)
                price_ok = 1.00 <= price <= 20.00
                gap_ok   = gap_pct >= 10.0
                cat_ok   = catalyst is not None and catalyst.tier in ("A", "B")
                float_ok = float_shares < 20_000_000

                # Log why promising tickers (gap ≥10%, right price) are rejected
                if gap_ok and price_ok:
                    if not float_ok:
                        log.debug("scanner: rejected float_too_large",
                                  ticker=ticker, float_shares=float_shares)
                    elif not cat_ok:
                        log.info("scanner: gap mover without catalyst",
                                 ticker=ticker, gap_pct=round(gap_pct, 1),
                                 price=round(price, 2), catalyst=str(catalyst))

                if passes_stock_selection(s):
                    score = quality_score(s)
                    candidates.append((score, s))
                    all_scores.append((ticker, score))

                    with get_connection() as conn:
                        conn.execute("""
                            INSERT OR IGNORE INTO scan_results
                            (id, scanned_at, ticker, price, gap_pct, rel_vol, float_shares,
                             news_tier, quality_score, passed_filter)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                        """, (str(uuid4()), datetime.utcnow().isoformat(), ticker,
                              price, gap_pct, 5.0, float_shares,
                              catalyst.tier if catalyst else None, score))

            candidates.sort(reverse=True)
            _state.watchlist = [s for _, s in candidates[:5]]
            _cycle += 1

            gappers = [
                t for t, snap in snaps.items()
                if (snap.get("prevDailyBar") or {}).get("c", 0) > 0
                and (((snap.get("dailyBar") or {}).get("c", 0)
                      / (snap.get("prevDailyBar") or {}).get("c", 1)) - 1) * 100 >= 5.0
            ]
            log.info("scanner: cycle complete",
                     cycle=_cycle,
                     universe=len(snaps),
                     gappers_5pct=len(gappers),
                     passed_filter=len(candidates),
                     watchlist=[s.ticker for s in _state.watchlist])

            # Write heartbeat to DB every 5 cycles (~2.5 min) so the dashboard
            # and SQLite show the scanner is alive even when 0 tickers pass.
            if _cycle % 5 == 1:
                _log_event("scanner_cycle",
                           f"cycle={_cycle} universe={len(snaps)} "
                           f"gappers={len(gappers)} passed={len(candidates)} "
                           f"watchlist={[s.ticker for s in _state.watchlist]}")

        except Exception as exc:
            log.error("Scanner error: %s", exc)
            _log_event("scanner_error", str(exc))

        await asyncio.sleep(30)


# ─── Real-time bar handler ────────────────────────────────────────────────────

def on_bar(candle) -> None:
    """Called by WebSocket stream for each incoming 1-min bar."""
    ticker = getattr(candle, "_ticker", None)
    if not ticker:
        return

    _state.candle_buffer[ticker].append(candle)

    # Pattern detection
    now = candle.bar_time.astimezone(ET)
    if not _in_trading_window(now):
        return

    from src.patterns.candles import Candle
    from src.patterns.detector import detect_bull_flag, detect_micro_pullback
    from src.engine.decision import evaluate_entry
    from src.engine.risk import check_daily_limits
    from src.execution.paper import open_long, get_open_positions_from_db
    from src.scanner.criteria import passes_stock_selection

    buf = list(_state.candle_buffer[ticker])
    idx = len(buf) - 1

    # Skip if already in a position for this ticker
    open_tickers = {p["ticker"] for p in get_open_positions_from_db()}
    if ticker in open_tickers:
        return

    # Find matching watchlist snap
    snap = next((s for s in _state.watchlist if s.ticker == ticker), None)
    if snap is None:
        return

    for detect_fn in (detect_bull_flag, detect_micro_pullback):
        signal = detect_fn(buf, idx)
        if signal is None:
            continue

        if _state.risk_state is None:
            return

        can_trade, reason = check_daily_limits(_state.risk_state, now)
        if not can_trade:
            logger.info("Signal on %s blocked: %s", ticker, reason)
            return

        decision = evaluate_entry(
            ticker=snap,
            signal=signal,
            account_value=_state.account_value,
            risk_state=_state.risk_state,
            now=now,
        )

        if decision.approved:
            trade = open_long(
                symbol=ticker,
                qty=decision.shares,
                entry_price=decision.entry_price,
                stop_price=decision.stop_price,
                target_price=decision.target_price,
                setup_type=decision.setup_type,
            )
            if trade:
                _state.risk_state.record_trade(0, now)  # Placeholder until fill
                _log_event("entry", f"{ticker} {decision.setup_type} entry={decision.entry_price:.2f} "
                           f"stop={decision.stop_price:.2f} target={decision.target_price:.2f}")
                asyncio.create_task(post_message(
                    settings.DISCORD_WEBHOOK_URL,
                    f"📥 ENTRY: **{ticker}** {decision.setup_type} | "
                    f"entry=${decision.entry_price:.2f} stop=${decision.stop_price:.2f} "
                    f"target=${decision.target_price:.2f} | {decision.shares} shares",
                ))
        break  # One signal per bar


# ─── EOD summary ─────────────────────────────────────────────────────────────

async def run_eod_summary() -> None:
    from src.learnings.triggers import _todays_closed_trades
    from src.learnings.extractor import extract_period_learning

    trades = _todays_closed_trades()
    if not trades:
        await post_message(settings.DISCORD_WEBHOOK_URL, "📊 EOD: No trades today.")
        return

    wins = [t for t in trades if (t.get("pnl_r") or 0) >= 0]
    total_pnl = sum(t.get("pnl_dollars", 0) or 0 for t in trades)
    avg_r = sum(t.get("pnl_r", 0) or 0 for t in trades) / len(trades)
    best = max(trades, key=lambda t: t.get("pnl_r", 0) or 0)

    eod_msg = (
        f"📊 **EOD** {date.today()} | {len(trades)} trades, "
        f"{len(wins)}W/{len(trades)-len(wins)}L, ${total_pnl:+.2f} ({avg_r:+.2f}R avg)\n"
        f"🏆 Best: {best['ticker']} {best.get('setup_type','')} {(best.get('pnl_r') or 0):+.2f}R"
    )
    await post_message(settings.DISCORD_WEBHOOK_URL, eod_msg)

    # Trigger learning extraction (async, non-blocking)
    discord_msg = extract_period_learning(trades, "eod", "day")
    if discord_msg:
        await post_message(settings.DISCORD_WEBHOOK_URL, f"💡 {discord_msg}")


# ─── Paper trading main loop ─────────────────────────────────────────────────

async def run_paper_mode(show_dashboard: bool = False) -> None:
    import csv
    from pathlib import Path

    log = structlog.get_logger("main")

    # Load universe
    universe_path = Path("small_cap_runners.csv")
    universe_tickers: List[str] = []
    float_map: Dict[str, int] = {}
    if universe_path.exists():
        with open(universe_path) as f:
            for row in csv.DictReader(f):
                t = row.get("ticker", "").strip().upper()
                if t:
                    universe_tickers.append(t)
                    try:
                        float_map[t] = int(row.get("float_shares", 15_000_000))
                    except ValueError:
                        float_map[t] = 15_000_000
    _state._float_map = float_map  # type: ignore[attr-defined]

    # Init
    _state.account_value = _load_account_value()
    _state.ws_stop = asyncio.Event()
    register_shutdown(shutdown)

    from src.engine.risk import RiskState
    _state.risk_state = RiskState(
        account_value=_state.account_value,
        date=str(date.today()),
    )

    log.info("Paper trading started", account=f"${_state.account_value:,.0f}",
             universe=len(universe_tickers))
    _log_event("startup", f"Paper trading started, account=${_state.account_value:,.2f}")

    await post_message(
        settings.DISCORD_WEBHOOK_URL,
        f"🟢 **Bot started** | mode: paper | account: ${_state.account_value:,.0f} | "
        f"{datetime.now(ET).strftime('%Y-%m-%d %H:%M ET')}",
    )

    # Dashboard runs as a separate Streamlit process (trading-dashboard systemd service)
    # Start it with: uv run streamlit run src/dashboard/app.py --server.port 8501
    if show_dashboard:
        log.info("Dashboard: run 'uv run streamlit run src/dashboard/app.py' separately")

    # Pre-market briefing at 08:30 ET (if time is right)
    now = _now_et()
    if (8, 25) <= (now.hour, now.minute) <= (9, 30):
        from src.research.perplexity import get_premarket_briefing
        from src.learnings.feedback import get_briefing_context
        briefing = get_premarket_briefing(
            briefing_date=date.today(),
            scanner_tickers=universe_tickers[:20],
            active_learnings=get_briefing_context(),
        )
        if briefing:
            _state.briefing = vars(briefing)
            await post_message(
                settings.DISCORD_WEBHOOK_URL,
                f"🌅 **Pre-market briefing**\n{briefing.market_context}\n"
                f"Sector: {briefing.sector_watch}\n"
                + ("⚠️ " + briefing.avoid_today if briefing.avoid_today else ""),
            )

    # Launch tasks
    tasks = [
        asyncio.create_task(watch_kill_switch(settings.KILL_SWITCH_FILE), name="kill_switch"),
        asyncio.create_task(run_premarket_scanner(universe_tickers), name="scanner"),
    ]

    # Start WebSocket stream once market is near open
    from src.data.alpaca_client import stream_bars
    ws_task = asyncio.create_task(
        stream_bars(universe_tickers, on_bar, _state.ws_stop),
        name="ws_stream",
    )
    tasks.append(ws_task)

    # Import mode handlers (lazy — avoids circular imports at module load time)
    from src.handlers import (
        active_trading as h_active,
        position_mgmt as h_position,
        eod_reflect as h_eod,
        overnight_intel as h_overnight,
        pre_market_prep as h_premarket,
        weekend_deep_work as h_weekend,
        maintenance as h_maintenance,
    )
    from src.engine.state import Mode

    _HANDLER_MAP = {
        Mode.ACTIVE_TRADING:    h_active,
        Mode.POSITION_MGMT:     h_position,
        Mode.EOD_REFLECT:       h_eod,
        Mode.OVERNIGHT_INTEL:   h_overnight,
        Mode.PRE_MARKET_PREP:   h_premarket,
        Mode.WEEKEND_DEEP_WORK: h_weekend,
        Mode.MAINTENANCE:       h_maintenance,
    }

    _prev_mode: Optional[Mode] = None
    eod_fired = False
    try:
        while not _state.ws_stop.is_set():
            await asyncio.sleep(5)
            now = _now_et()

            # Determine current operating mode
            maintenance_active = settings.KILL_SWITCH_FILE and \
                __import__("pathlib").Path(settings.KILL_SWITCH_FILE).exists()
            mode = current_mode(now=now, maintenance_active=bool(maintenance_active))
            log.info("current_mode", mode=mode.value)

            # On mode transition, call on_enter() if the handler defines it
            if mode is not _prev_mode:
                log.info("mode_transition", previous=(_prev_mode.value if _prev_mode else None),
                         current=mode.value)
                handler = _HANDLER_MAP.get(mode)
                if handler and hasattr(handler, "on_enter"):
                    await handler.on_enter(_state)
                _prev_mode = mode

            # Dispatch to current-mode handler tick()
            handler = _HANDLER_MAP.get(mode)
            if handler and hasattr(handler, "tick"):
                await handler.tick(_state, now)

            # Force close at 15:55 (handled here regardless of mode)
            if _after_force_close(now) and not eod_fired:
                log.warning("Force-close time reached (15:55 ET)")
                from src.execution.paper import emergency_close_all
                emergency_close_all()
                _log_event("force_close", "15:55 ET force-close fired")

                await asyncio.sleep(60 * 20)  # Wait for fills to settle
                await run_eod_summary()
                eod_fired = True
                _state.ws_stop.set()
                break

    except asyncio.CancelledError:
        pass
    finally:
        for t in tasks:
            t.cancel()
        await shutdown(reason="paper_mode_exit")


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Trading bot")
    parser.add_argument("--mode", choices=["test", "paper", "live", "backtest"],
                        default=settings.TRADING_MODE)
    parser.add_argument("--dashboard", action="store_true", help="Start read-only web dashboard")
    args = parser.parse_args()
    mode = args.mode

    configure_logging(log_level=settings.LOG_LEVEL, log_file="logs/bot.log")

    log = structlog.get_logger("main")
    log.info("Trading bot initialising", mode=mode, version="0.2.0")
    init_schema()

    if mode == "test":
        asyncio.run(run_test_mode())
    elif mode == "paper":
        asyncio.run(run_paper_mode(show_dashboard=args.dashboard))
    elif mode == "live":
        log.error("Live trading not enabled. Use paper mode.")
        sys.exit(1)
    elif mode == "backtest":
        log.info("Run: uv run python scripts/backtest.py")
        sys.exit(0)


if __name__ == "__main__":
    main()
