# Trading Bot — Warrior Trading Momentum Strategy

A rule-based momentum day-trading bot for US small-cap stocks.

## Quick start (< 10 minutes)

### 1. Install uv (if not already installed)
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### 2. Clone and set up
```bash
cd trading-bot
uv sync
```

### 3. Configure secrets
```bash
cp .env.example .env
chmod 600 .env
# Edit .env and fill in your API keys
```

Required keys per phase:
- **Phase 0 (test):** None required — `TRADING_MODE=test` skips all checks
- **Phase 1 (backtest):** `MASSIVE_API_KEY`
- **Phase 2 (paper):** `ALPACA_API_KEY`, `ALPACA_API_SECRET`, `FINNHUB_API_KEY`, `GROQ_API_KEY`
- **Optional:** `DISCORD_WEBHOOK_URL` for alerts, `PERPLEXITY_API_KEY` for pre-market briefings

### 4. Initialise the database
```bash
uv run python scripts/seed_db.py
```

### 5. Test the setup
```bash
uv run python -m src.main --mode=test
```

Expected output: config loads, test event written to SQLite, Discord ping sent (if webhook set), clean exit.

### 6. Run tests
```bash
uv run pytest
```

## Modes

| Mode | Command | Description |
|---|---|---|
| Test | `uv run python -m src.main --mode=test` | Smoke test — no live data |
| Paper | `uv run python -m src.main --mode=paper` | Paper trading via Alpaca |
| Backtest | `uv run python scripts/backtest.py --start=YYYY-MM-DD --end=YYYY-MM-DD` | Historical replay |

## Kill switch

To stop the bot immediately from any terminal (including SSH):
```bash
touch /tmp/bot_killswitch
```

The bot will cancel open orders, close positions, log to Discord, and exit.

## Phases

- **Phase 0** ✅ Setup & skeleton (current)
- **Phase 1** Backtest mode
- **Phase 2** Paper trading live (90 days)
- **Phase 3** Live trading with micro size (after Phase 2 is proven)

## Project structure

```
src/
  main.py          — entrypoint
  config.py        — pydantic settings (loads .env + config.yaml)
  kill_switch.py   — file-based kill switch watcher
  logging_setup.py — structlog configuration
  data/            — Alpaca, Massive, Finnhub clients
  scanner/         — 5-criteria filter + quality scoring
  news/            — ingestion + LLM classification
  patterns/        — bull flag, micro pullback detection
  engine/          — decision, position sizing, risk rules
  execution/       — paper order placement
  storage/         — SQLite schema + models
  alerts/          — Discord webhook
  dashboard/       — read-only web UI (Phase 2)
  llm/             — Groq (cheap) + Claude (premium)
  learnings/       — learning loop (triggers, extraction, feedback)
  research/        — Perplexity pre-market briefing
scripts/
  seed_db.py       — initialise SQLite schema
  backtest.py      — run strategy on historical data (Phase 1)
```
