#!/usr/bin/env python3
"""
Backtest script — Phase 1.

Downloads historical data, applies scanner + pattern detection,
simulates trades, and prints a full report.

Usage:
  uv run python scripts/backtest.py --start=2024-01-01 --end=2024-12-31 --universe=small_cap_runners.csv

The universe CSV must have columns: ticker, float_shares (and optionally: sector)
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from uuid import uuid4
from zoneinfo import ZoneInfo

# Ensure src/ is importable
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import settings
from src.data.finnhub_client import get_company_news
from src.data.massive_client import get_daily_bars, get_intraday_bars
from src.engine.decision import evaluate_entry
from src.engine.position import calculate_position_size
from src.engine.risk import RISK_RULES, RiskState, check_daily_limits
from src.logging_setup import configure_logging
from src.patterns.candles import Candle
from src.patterns.detector import PatternSignal, scan_for_patterns
from src.scanner.scanner import DayResult, UniverseTicker, scan_day
from src.storage.db import get_connection, init_schema

ET = ZoneInfo("America/New_York")
logger = logging.getLogger("backtest")


# ─── CLI ──────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Trading bot backtest")
    p.add_argument("--start", required=True, help="Start date YYYY-MM-DD")
    p.add_argument("--end", required=True, help="End date YYYY-MM-DD")
    p.add_argument("--universe", required=True, help="Path to universe CSV")
    p.add_argument("--account", type=float, default=25_000.0, help="Starting account value ($)")
    p.add_argument("--no-news", action="store_true", help="Skip news fetch (faster, no news filter)")
    p.add_argument("--no-cache", action="store_true", help="Force re-download all data")
    return p.parse_args()


# ─── Universe CSV ─────────────────────────────────────────────────────────────

def load_universe(csv_path: str) -> List[UniverseTicker]:
    path = Path(csv_path)
    if not path.exists():
        logger.error("Universe file not found: %s", csv_path)
        sys.exit(1)

    tickers: List[UniverseTicker] = []
    with open(path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            ticker = row.get("ticker", "").strip().upper()
            if not ticker:
                continue
            try:
                float_shares = int(row.get("float_shares", 0))
            except ValueError:
                float_shares = 15_000_000   # Default if missing
            sector = row.get("sector", "").strip()
            tickers.append(UniverseTicker(ticker=ticker, float_shares=float_shares, sector=sector))

    logger.info("Universe: %d tickers loaded from %s", len(tickers), csv_path)
    return tickers


# ─── Data download phase ──────────────────────────────────────────────────────

def download_daily_bars(
    universe: List[UniverseTicker],
    start: date,
    end: date,
    use_cache: bool,
) -> Dict[str, list]:
    """Download (or load from cache) daily bars for all universe tickers."""
    # Fetch 30 extra days before start for relative-volume calculation
    fetch_start = start - timedelta(days=45)
    all_bars: Dict[str, list] = {}

    logger.info("Downloading daily bars for %d tickers (%s→%s)...", len(universe), fetch_start, end)
    for i, u in enumerate(universe):
        logger.info("[%d/%d] Daily bars: %s", i + 1, len(universe), u.ticker)
        bars = get_daily_bars(u.ticker, fetch_start, end, use_cache=use_cache)
        all_bars[u.ticker] = bars

    return all_bars


def download_news(
    watchlist_by_day: Dict[date, List[str]],   # date → list of tickers
    use_cache: bool,
) -> Dict[Tuple[str, date], list]:
    """Download news for each (ticker, day) pair that made the watchlist."""
    news: Dict[Tuple[str, date], list] = {}
    pairs = [(ticker, day) for day, tickers in watchlist_by_day.items() for ticker in tickers]
    logger.info("Fetching news for %d ticker-day pairs...", len(pairs))

    for i, (ticker, day) in enumerate(pairs):
        key = (ticker, day)
        if key in news:
            continue
        logger.info("[%d/%d] News: %s %s", i + 1, len(pairs), ticker, day)
        items = get_company_news(ticker, day - timedelta(days=1), day)
        news[key] = items

    return news


def download_intraday_bars(
    watchlist_by_day: Dict[date, List[str]],
    use_cache: bool,
) -> Dict[Tuple[str, date], List[Candle]]:
    """Download 1-min bars for each (ticker, day) pair and convert to Candle objects."""
    candles_map: Dict[Tuple[str, date], List[Candle]] = {}
    pairs = [(ticker, day) for day, tickers in watchlist_by_day.items() for ticker in tickers]
    logger.info("Downloading 1min bars for %d ticker-day pairs...", len(pairs))

    for i, (ticker, day) in enumerate(pairs):
        key = (ticker, day)
        logger.info("[%d/%d] 1min bars: %s %s", i + 1, len(pairs), ticker, day)
        bars = get_intraday_bars(ticker, day, use_cache=use_cache)
        # Convert to Candle, filter to regular hours only (9:30-16:00 ET)
        candles: List[Candle] = []
        for b in bars:
            t_et = b.bar_time.astimezone(ET)
            if t_et.hour == 9 and t_et.minute < 30:
                continue
            if t_et.hour >= 16:
                continue
            candles.append(Candle(
                bar_time=b.bar_time,
                open=b.open,
                high=b.high,
                low=b.low,
                close=b.close,
                volume=b.volume,
                vwap=b.vwap,
            ))
        candles_map[key] = candles

    return candles_map


# ─── Trade simulation ─────────────────────────────────────────────────────────

def _force_close_time(day: date) -> datetime:
    t = datetime(day.year, day.month, day.day, 15, 55, 0, tzinfo=ET)
    return t


def simulate_trade(
    ticker: str,
    signal: PatternSignal,
    candles: List[Candle],
    account_value: float,
    risk_state: RiskState,
    backtest_run_id: str,
) -> Optional[dict]:
    """
    Simulate a single trade given a pattern signal and subsequent candles.
    Entry = open of next candle after signal.
    Returns trade dict or None if entry couldn't be filled.
    """
    signal_idx = signal.signal_bar_idx
    if signal_idx + 1 >= len(candles):
        return None  # No next candle to enter on

    entry_candle = candles[signal_idx + 1]
    entry_time = entry_candle.bar_time.astimezone(ET)

    # Check if entry candle is within trading window
    if entry_time.hour > 11 or (entry_time.hour == 11 and entry_time.minute >= 30):
        return None

    fill_price = entry_candle.open

    # Must break above entry trigger on entry candle
    if fill_price < signal.entry_trigger and entry_candle.high < signal.entry_trigger:
        return None   # Never triggered

    # Use open as fill if it's already above trigger, else use trigger price
    if fill_price < signal.entry_trigger:
        fill_price = signal.entry_trigger

    stop_price = signal.stop_price
    risk = abs(fill_price - stop_price)
    if risk == 0:
        return None

    target_price = fill_price + risk * RISK_RULES["profit_loss_ratio_min"]
    shares = calculate_position_size(account_value, fill_price, stop_price)
    if shares == 0:
        return None

    # Simulate bar by bar until target, stop, or force-close
    exit_price: Optional[float] = None
    exit_reason: str = ""
    closed_at: Optional[datetime] = None
    day = entry_candle.bar_time.date()
    force_close = _force_close_time(day)

    for candle in candles[signal_idx + 1:]:
        t_et = candle.bar_time.astimezone(ET)

        # Force close
        if t_et >= force_close:
            exit_price = candle.open
            exit_reason = "force_close_eod"
            closed_at = t_et
            break

        # Stop hit (bar's low touches or crosses stop)
        if candle.low <= stop_price:
            exit_price = stop_price
            exit_reason = "stop_loss"
            closed_at = t_et
            break

        # Target hit (bar's high reaches target)
        if candle.high >= target_price:
            exit_price = target_price
            exit_reason = "target_hit"
            closed_at = t_et
            break

        # Early exit: first red 1min candle below entry (simplified Signal 1 from handoff)
        if candle.is_red and candle.close < fill_price:
            exit_price = candle.close
            exit_reason = "first_red_candle_below_entry"
            closed_at = t_et
            break

    if exit_price is None:
        # End of day without hitting target or stop
        if candles:
            last = candles[-1]
            exit_price = last.close
            exit_reason = "eod_close"
            closed_at = last.bar_time.astimezone(ET)
        else:
            return None

    pnl_dollars = (exit_price - fill_price) * shares
    pnl_r = (exit_price - fill_price) / risk if risk > 0 else 0.0

    trade_id = str(uuid4())
    opened_at = entry_candle.bar_time.astimezone(ET)

    return {
        "id": trade_id,
        "backtest_run_id": backtest_run_id,
        "ticker": ticker,
        "opened_at": opened_at.isoformat(),
        "closed_at": closed_at.isoformat() if closed_at else None,
        "side": "long",
        "shares": shares,
        "entry_price": round(fill_price, 4),
        "exit_price": round(exit_price, 4),
        "stop_price": round(stop_price, 4),
        "target_price": round(target_price, 4),
        "pnl_dollars": round(pnl_dollars, 2),
        "pnl_r": round(pnl_r, 4),
        "setup_type": signal.pattern,
        "exit_reason": exit_reason,
        "mode": "backtest",
    }


def store_trade(trade: dict) -> None:
    with get_connection() as conn:
        conn.execute("""
            INSERT OR IGNORE INTO trades
            (id, opened_at, closed_at, ticker, side, shares, entry_price, exit_price,
             stop_price, target_price, pnl_dollars, pnl_r, setup_type, exit_reason, mode)
            VALUES
            (:id, :opened_at, :closed_at, :ticker, :side, :shares, :entry_price, :exit_price,
             :stop_price, :target_price, :pnl_dollars, :pnl_r, :setup_type, :exit_reason, :mode)
        """, trade)


# ─── Report ────────────────────────────────────────────────────────────────────

def generate_report(
    trades: List[dict],
    start: date,
    end: date,
    universe_size: int,
    account_start: float,
) -> str:
    if not trades:
        return "# Backtest Report\n\n**No trades generated.** Check scanner criteria and data.\n"

    wins = [t for t in trades if t["pnl_r"] >= 0]
    losses = [t for t in trades if t["pnl_r"] < 0]
    total = len(trades)
    win_rate = len(wins) / total if total else 0.0

    avg_win_r = sum(t["pnl_r"] for t in wins) / len(wins) if wins else 0.0
    avg_loss_r = sum(t["pnl_r"] for t in losses) / len(losses) if losses else 0.0
    avg_r = sum(t["pnl_r"] for t in trades) / total

    gross_profit = sum(t["pnl_dollars"] for t in wins)
    gross_loss = abs(sum(t["pnl_dollars"] for t in losses))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    # Max drawdown (running account PnL)
    running_pnl = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in sorted(trades, key=lambda x: x["opened_at"]):
        running_pnl += t["pnl_dollars"]
        if running_pnl > peak:
            peak = running_pnl
        dd = peak - running_pnl
        if dd > max_dd:
            max_dd = dd

    max_dd_pct = max_dd / account_start * 100

    total_pnl = sum(t["pnl_dollars"] for t in trades)
    total_pnl_pct = total_pnl / account_start * 100

    # By setup type
    setup_stats: Dict[str, dict] = {}
    for t in trades:
        st = t.get("setup_type", "unknown")
        if st not in setup_stats:
            setup_stats[st] = {"trades": 0, "wins": 0, "r_sum": 0.0}
        setup_stats[st]["trades"] += 1
        if t["pnl_r"] >= 0:
            setup_stats[st]["wins"] += 1
        setup_stats[st]["r_sum"] += t["pnl_r"]

    # By hour
    hour_stats: Dict[int, dict] = {}
    for t in trades:
        h = datetime.fromisoformat(t["opened_at"]).hour
        if h not in hour_stats:
            hour_stats[h] = {"trades": 0, "wins": 0}
        hour_stats[h]["trades"] += 1
        if t["pnl_r"] >= 0:
            hour_stats[h]["wins"] += 1

    # Exit reasons
    exit_counts: Dict[str, int] = {}
    for t in trades:
        r = t.get("exit_reason", "unknown")
        exit_counts[r] = exit_counts.get(r, 0) + 1

    report_lines = [
        f"# Backtest Report",
        f"",
        f"**Period:** {start} → {end}  ",
        f"**Universe:** {universe_size} tickers  ",
        f"**Starting account:** ${account_start:,.0f}",
        f"",
        f"## Summary",
        f"",
        f"| Metric | Value |",
        f"|---|---|",
        f"| Total trades | {total} |",
        f"| Win rate | {win_rate:.1%} ({len(wins)}W / {len(losses)}L) |",
        f"| Profit factor | {profit_factor:.2f} |",
        f"| Avg win (R) | +{avg_win_r:.2f}R |",
        f"| Avg loss (R) | {avg_loss_r:.2f}R |",
        f"| Avg R per trade | {avg_r:+.2f}R |",
        f"| Total PnL | ${total_pnl:+,.2f} ({total_pnl_pct:+.1f}%) |",
        f"| Max drawdown | ${max_dd:,.2f} ({max_dd_pct:.1f}%) |",
        f"",
        f"## By Setup Type",
        f"",
        f"| Setup | Trades | Win Rate | Avg R |",
        f"|---|---|---|---|",
    ]
    for st, s in sorted(setup_stats.items()):
        wr = s["wins"] / s["trades"] if s["trades"] else 0
        ar = s["r_sum"] / s["trades"] if s["trades"] else 0
        report_lines.append(f"| {st} | {s['trades']} | {wr:.1%} | {ar:+.2f}R |")

    report_lines += [
        f"",
        f"## By Hour (ET)",
        f"",
        f"| Hour | Trades | Win Rate |",
        f"|---|---|---|",
    ]
    for h in sorted(hour_stats.keys()):
        s = hour_stats[h]
        wr = s["wins"] / s["trades"] if s["trades"] else 0
        report_lines.append(f"| {h:02d}:xx | {s['trades']} | {wr:.1%} |")

    report_lines += [
        f"",
        f"## Exit Reasons",
        f"",
        f"| Reason | Count |",
        f"|---|---|",
    ]
    for reason, count in sorted(exit_counts.items(), key=lambda x: -x[1]):
        report_lines.append(f"| {reason} | {count} |")

    # Phase 1 DoD check
    report_lines += [
        f"",
        f"## Phase 1 Definition of Done",
        f"",
        f"| Check | Target | Result | Status |",
        f"|---|---|---|---|",
        f"| Win rate | >50% | {win_rate:.1%} | {'✅' if win_rate > 0.50 else '❌'} |",
        f"| Profit factor | >1.5 | {profit_factor:.2f} | {'✅' if profit_factor > 1.5 else '❌'} |",
    ]

    return "\n".join(report_lines)


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    configure_logging(log_level=settings.LOG_LEVEL, log_file="logs/backtest.log")
    args = parse_args()

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    account_value = args.account
    use_cache = not args.no_cache

    logger.info("Backtest: %s → %s | account=$%.0f | cache=%s",
                start, end, account_value, use_cache)

    init_schema()
    universe = load_universe(args.universe)

    if not universe:
        logger.error("Universe is empty — nothing to backtest")
        sys.exit(1)

    # ── Phase A: Download daily bars ──────────────────────────────────────────
    all_daily_bars = download_daily_bars(universe, start, end, use_cache)

    # ── Phase B: Identify interesting days (pre-filter) ───────────────────────
    logger.info("Phase B: Identifying watchlist candidates per day...")
    trading_days: List[date] = []
    current = start
    while current <= end:
        # Skip weekends (proper holiday detection: if no bars, skip)
        if current.weekday() < 5:
            trading_days.append(current)
        current += timedelta(days=1)

    # First pass scan (without news) to identify which ticker+day pairs need news
    logger.info("First-pass scan: %d trading days", len(trading_days))
    pre_watchlist_by_day: Dict[date, List[str]] = {}
    for day in trading_days:
        result = scan_day(
            trading_day=day,
            universe=universe,
            all_daily_bars=all_daily_bars,
            news_by_ticker={},  # No news yet
            top_n=10,           # Wider net for pre-filter
        )
        # Pre-filter: tickers that passed 4 of 5 criteria (no news check yet)
        pre_watchlist_by_day[day] = [s.ticker for s in result.watchlist
                                      if s.price >= 1.0 and s.relative_volume >= 5.0]

    # ── Phase C: Download news ────────────────────────────────────────────────
    if not args.no_news:
        news_raw = download_news(pre_watchlist_by_day, use_cache)
    else:
        news_raw = {}
        logger.info("Skipping news fetch (--no-news flag)")

    # Reshape news: news_by_ticker[ticker] = list of items (all dates merged)
    news_by_ticker: Dict[str, list] = {}
    for (ticker, day), items in news_raw.items():
        if ticker not in news_by_ticker:
            news_by_ticker[ticker] = []
        news_by_ticker[ticker].extend(items)

    # ── Phase D: Full scan with news ──────────────────────────────────────────
    logger.info("Phase D: Full scan with news filter...")
    watchlist_by_day: Dict[date, List[str]] = {}
    all_day_results: Dict[date, DayResult] = {}

    for day in trading_days:
        result = scan_day(
            trading_day=day,
            universe=universe,
            all_daily_bars=all_daily_bars,
            news_by_ticker=news_by_ticker,
            top_n=5,
        )
        all_day_results[day] = result
        watchlist_by_day[day] = [s.ticker for s in result.watchlist]

    # ── Phase E: Download 1-min bars for watchlist ────────────────────────────
    intraday_pairs = {(ticker, day) for day, tickers in watchlist_by_day.items()
                      for ticker in tickers}
    logger.info("Phase E: Downloading 1min bars for %d ticker-day pairs...", len(intraday_pairs))
    candles_map = download_intraday_bars(watchlist_by_day, use_cache)

    # ── Phase F: Simulate trades ──────────────────────────────────────────────
    logger.info("Phase F: Simulating trades...")
    backtest_run_id = str(uuid4())
    all_trades: List[dict] = []
    current_account = account_value

    for day in trading_days:
        result = all_day_results.get(day)
        if not result or not result.watchlist:
            continue

        # Fresh risk state per day
        risk_state = RiskState(account_value=current_account, date=str(day))

        for snap in result.watchlist:
            ticker = snap.ticker
            candles = candles_map.get((ticker, day), [])
            if not candles:
                logger.debug("No 1min bars for %s on %s", ticker, day)
                continue

            signals = scan_for_patterns(candles)
            logger.debug("%s %s: %d signals detected", ticker, day, len(signals))

            for signal in signals:
                # Check daily limits before each potential entry
                signal_time = candles[signal.signal_bar_idx].bar_time
                can_trade, reason = check_daily_limits(risk_state, signal_time)
                if not can_trade:
                    logger.debug("Skipping signal: %s", reason)
                    break

                trade = simulate_trade(
                    ticker=ticker,
                    signal=signal,
                    candles=candles,
                    account_value=current_account,
                    risk_state=risk_state,
                    backtest_run_id=backtest_run_id,
                )
                if trade:
                    store_trade(trade)
                    all_trades.append(trade)
                    closed = datetime.fromisoformat(trade["closed_at"])
                    risk_state.record_trade(trade["pnl_dollars"], closed)
                    current_account += trade["pnl_dollars"]
                    logger.info(
                        "TRADE: %s %s %s entry=%.2f exit=%.2f pnl=$%.2f (%.2fR)",
                        trade["ticker"], trade["opened_at"][:10],
                        trade["setup_type"], trade["entry_price"],
                        trade["exit_price"], trade["pnl_dollars"], trade["pnl_r"],
                    )
                    break  # One trade per ticker per day

        # Update account value based on day's PnL
        current_account = account_value + sum(t["pnl_dollars"] for t in all_trades)

    # ── Phase G: Report ────────────────────────────────────────────────────────
    logger.info("Phase G: Generating report (%d trades)...", len(all_trades))
    report = generate_report(all_trades, start, end, len(universe), account_value)

    # Print to console
    print("\n" + "=" * 60)
    print(report)
    print("=" * 60 + "\n")

    # Save to file
    report_file = Path(f"backtest_report_{start}_{end}.md")
    report_file.write_text(report)
    logger.info("Report saved: %s", report_file)

    # Store run metadata in DB
    with get_connection() as conn:
        conn.execute("""
            INSERT OR IGNORE INTO backtest_runs
            (id, run_at, start_date, end_date, universe, summary_md)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (backtest_run_id, datetime.utcnow().isoformat(),
              str(start), str(end), args.universe, report[:2000]))


if __name__ == "__main__":
    main()
