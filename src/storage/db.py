from __future__ import annotations

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent.parent.parent / "bot.db"


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_schema() -> None:
    with get_connection() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS scan_results (
            id          TEXT PRIMARY KEY,
            scanned_at  TEXT NOT NULL,
            ticker      TEXT NOT NULL,
            price       REAL,
            gap_pct     REAL,
            rel_vol     REAL,
            float_shares INTEGER,
            news_tier   TEXT,
            quality_score REAL,
            passed_filter INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS news_items (
            id          TEXT PRIMARY KEY,
            fetched_at  TEXT NOT NULL,
            ticker      TEXT NOT NULL,
            headline    TEXT NOT NULL,
            source      TEXT,
            tier        TEXT,
            category    TEXT,
            sentiment   REAL,
            reasoning   TEXT
        );

        CREATE TABLE IF NOT EXISTS trades (
            id              TEXT PRIMARY KEY,
            opened_at       TEXT NOT NULL,
            closed_at       TEXT,
            ticker          TEXT NOT NULL,
            side            TEXT NOT NULL,
            shares          INTEGER NOT NULL,
            entry_price     REAL NOT NULL,
            exit_price      REAL,
            stop_price      REAL NOT NULL,
            target_price    REAL NOT NULL,
            pnl_dollars     REAL,
            pnl_r           REAL,
            setup_type      TEXT,
            exit_reason     TEXT,
            news_item_id    TEXT REFERENCES news_items(id),
            briefing_id     TEXT,
            mode            TEXT NOT NULL DEFAULT 'paper'
        );

        CREATE TABLE IF NOT EXISTS learnings (
            id              TEXT PRIMARY KEY,
            created_at      TEXT NOT NULL,
            trigger         TEXT NOT NULL,
            scope           TEXT NOT NULL,
            related_trade_ids TEXT,
            observation     TEXT NOT NULL,
            quantification  TEXT NOT NULL,
            confidence      REAL NOT NULL,
            suggested_change TEXT,
            config_diff     TEXT,
            applied         INTEGER NOT NULL DEFAULT 0,
            applied_at      TEXT,
            evaluation_date TEXT,
            outcome         TEXT,
            outcome_notes   TEXT
        );

        CREATE TABLE IF NOT EXISTS briefings (
            id              TEXT PRIMARY KEY,
            created_at      TEXT NOT NULL,
            briefing_date   TEXT NOT NULL,
            market_context  TEXT,
            sector_watch    TEXT,
            scheduled_events TEXT,
            avoid_today     TEXT,
            raw_json        TEXT
        );

        CREATE TABLE IF NOT EXISTS bot_events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            occurred_at TEXT NOT NULL,
            event_type  TEXT NOT NULL,
            message     TEXT,
            extra       TEXT
        );

        CREATE TABLE IF NOT EXISTS price_bars (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker      TEXT NOT NULL,
            bar_time    TEXT NOT NULL,
            timeframe   TEXT NOT NULL,
            open        REAL NOT NULL,
            high        REAL NOT NULL,
            low         REAL NOT NULL,
            close       REAL NOT NULL,
            volume      INTEGER NOT NULL,
            vwap        REAL,
            UNIQUE(ticker, bar_time, timeframe)
        );

        CREATE TABLE IF NOT EXISTS ticker_reference (
            ticker              TEXT PRIMARY KEY,
            fetched_at          TEXT NOT NULL,
            shares_outstanding  INTEGER,
            company_name        TEXT,
            sector              TEXT
        );

        CREATE TABLE IF NOT EXISTS backtest_runs (
            id          TEXT PRIMARY KEY,
            run_at      TEXT NOT NULL,
            start_date  TEXT NOT NULL,
            end_date    TEXT NOT NULL,
            universe    TEXT NOT NULL,
            config_json TEXT,
            summary_md  TEXT
        );

        CREATE TABLE IF NOT EXISTS overnight_intel (
            id              TEXT PRIMARY KEY,
            fetched_at      TEXT NOT NULL,
            ticker          TEXT NOT NULL,
            headline        TEXT NOT NULL,
            source          TEXT,
            published_at    TEXT,
            summary         TEXT,
            tier            TEXT,
            category        TEXT,
            sentiment       REAL,
            used_in_briefing INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS mode_durations (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            date        TEXT NOT NULL,
            mode        TEXT NOT NULL,
            entered_at  TEXT NOT NULL,
            exited_at   TEXT,
            duration_s  REAL
        );

        CREATE INDEX IF NOT EXISTS idx_trades_ticker        ON trades(ticker);
        CREATE INDEX IF NOT EXISTS idx_trades_opened_at     ON trades(opened_at);
        CREATE INDEX IF NOT EXISTS idx_news_ticker          ON news_items(ticker);
        CREATE INDEX IF NOT EXISTS idx_scan_ticker          ON scan_results(ticker);
        CREATE INDEX IF NOT EXISTS idx_bars_ticker_time     ON price_bars(ticker, bar_time, timeframe);
        CREATE INDEX IF NOT EXISTS idx_overnight_ticker     ON overnight_intel(ticker);
        CREATE INDEX IF NOT EXISTS idx_overnight_fetched_at ON overnight_intel(fetched_at);

        -- System 5: Funnel Tracker (AUTONOMOUS_SYSTEMS_HANDOFF.md)
        CREATE TABLE IF NOT EXISTS funnel_daily (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            date        TEXT NOT NULL,
            stage       TEXT NOT NULL,
            count       INTEGER NOT NULL DEFAULT 0,
            recorded_at TEXT NOT NULL,
            UNIQUE(date, stage)
        );
        CREATE INDEX IF NOT EXISTS idx_funnel_date  ON funnel_daily(date);
        CREATE INDEX IF NOT EXISTS idx_funnel_stage ON funnel_daily(stage);

        -- System 2: Cost Tracker (AUTONOMOUS_SYSTEMS_HANDOFF.md)
        CREATE TABLE IF NOT EXISTS cost_metrics (
            trade_id             TEXT PRIMARY KEY,
            snapshot_at          TEXT,
            quote_bid_decision   REAL,
            quote_ask_decision   REAL,
            midpoint_decision    REAL,
            spread_at_entry      REAL,
            entry_fill_price     REAL,
            entry_slippage_pct   REAL,
            exit_fill_price      REAL,
            exit_slippage_pct    REAL,
            gross_profit_R       REAL,
            total_cost_R         REAL,
            net_profit_R         REAL,
            FOREIGN KEY (trade_id) REFERENCES trades(id)
        );
        """)
    print(f"Schema initialised at {DB_PATH}")
