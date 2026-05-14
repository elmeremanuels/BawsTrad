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

## VPS deploy (Hetzner / fresh server)

One command sets up both systemd services, UFW, sudoers, and the state directory:

```bash
# 1. Clone repo
git clone https://github.com/elmeremanuels/BawsTrad.git
cd BawsTrad

# 2. Create .env with API keys (see Configure secrets above)
cp .env.example .env
chmod 600 .env
nano .env   # fill in keys

# 3. Run setup script (as root or sudo)
sudo bash deploy/setup.sh
```

The script installs:
- `trading-bot.service` — the bot (enabled, **not auto-started** until you verify `.env`)
- `trading-dashboard.service` — Streamlit UI on port 8501 (auto-started)
- `/etc/sudoers.d/trading-bot-control` — lets the dashboard restart/stop/start the bot
- `state/` — pause flags, audit log

After setup:
```bash
# Verify .env is complete, then start the bot
sudo systemctl start trading-bot
sudo journalctl -u trading-bot -n 50 --no-pager

# Dashboard (Tailscale VPN required)
# http://<tailscale-ip>:8501
```

### Service files in the repo

| File | Purpose |
|---|---|
| `deploy/trading-bot.service` | Bot process (`uv run python -m src.main --mode=paper`) |
| `deploy/trading-dashboard.service` | Streamlit dashboard on port 8501 |
| `deploy/setup.sh` | Installs both services + UFW + sudoers |

### Useful commands

```bash
# Bot
sudo systemctl status trading-bot
sudo journalctl -u trading-bot -f        # live log tail
sudo systemctl restart trading-bot

# Dashboard
sudo systemctl status trading-dashboard
sudo journalctl -u trading-dashboard -f

# Kill switch (immediate position close + exit)
touch /tmp/bot_killswitch
```

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
deploy/
  setup.sh                  — one-command VPS setup (bot + dashboard)
  trading-bot.service       — systemd unit for the bot
  trading-dashboard.service — systemd unit for the dashboard
```
