# Appendix E — Universe Strategy

> **Version**: 2026-05  
> **Status**: Implemented  
> **Related files**: `data/base_universe.csv`, `state/universe_today.csv`, `scripts/refresh_universe.py`, `scripts/build_base_universe.py`, `src/handlers/pre_market_prep.py`, `src/data/float_cache.py`

---

## Overview — Three-Tier System

```
TIER 1: BASE UNIVERSE         (~3000–5000 tickers)
   All active US common stocks on NYSE/NASDAQ/AMEX/ARCA.
   Static — rebuilt weekly.
   File: data/base_universe.csv
          ↓  (daily Alpaca snapshot scan)
TIER 2: DAILY UNIVERSE        (~30–80 tickers on normal days)
   Stocks gapping ≥3% with price $1–$20 today.
   Refreshed 4× pre-market.
   File: state/universe_today.csv
          ↓  (5-criteria scanner filter)
TIER 3: INTRADAY WATCHLIST    (max 5 tickers)
   Pass all 5 trading criteria: price, gap ≥10%, rel-vol ≥5×,
   Tier A/B news catalyst, float <20M.
   In-memory: _state.watchlist
   WebSocket bars for these tickers trigger pattern detection → trades.
```

---

## Tier 1 — Base Universe

### Source
Alpaca REST `GET /v2/assets?status=active&asset_class=us_equity`

### Filter criteria
| Criterion | Rule |
|---|---|
| Exchange | NYSE, NASDAQ, AMEX, or ARCA only |
| Tradable | `tradable = true` |
| Symbol pattern | 1–5 uppercase letters, no dots/dashes/suffixes |
| Name exclusions | Name must NOT contain: `etf`, `fund`, `trust`, `index`, `reit`, `mlp`, `holding(s)`, `income`, `portfolio` |

### Output
`data/base_universe.csv` — columns: `ticker, exchange, name`  
Typical size: 3 000–5 000 tickers.

### Maintenance
Rebuild weekly (Sunday evening) via:
```bash
uv run python scripts/build_base_universe.py
```
Also rebuild after major index rebalances (Russell 1000/2000 reconstitution in June).

---

## Tier 2 — Daily Universe

### Source
Alpaca `GET /v2/stocks/snapshots` on the base universe (batches of 100).

### Filter criteria
| Criterion | Value | Config key |
|---|---|---|
| Price range | $1.00 – $20.00 | `scanner.price_min/max` |
| Gap % (soft) | ≥ 3% vs prev close | `scanner.universe_gap_filter_pct` |
| Fallback | Top-30 by gap% regardless of filter (ensures scanner always has candidates on low-volatility days) | hardcoded 30 |

**Note**: Gap ≥3% is intentionally below the trading threshold (≥10%) to catch stocks that might accelerate into the open.

### Output
`state/universe_today.csv` — columns: `ticker, float_shares, sector, gap_pct`

`float_shares` comes from `ticker_reference` cache (see Float Data section).

### Refresh schedule

| Time (ET) | Trigger | Behaviour |
|---|---|---|
| 04:00 | `pre_market_prep.py` task `universe_refresh` | Initial build; skips if already fresh today |
| 07:00 | `universe_refresh_2` | Force re-run (overnight news now ingested) |
| 09:20 | `universe_refresh_3` | Force re-run (latest pre-market prices) |
| Manual | `uv run python scripts/refresh_universe.py` | Always runs, prints delta |

### Delta logging
Each refresh prints:
```
[refresh_universe] NEW:     AAPL, NVDA
[refresh_universe] REMOVED: MULN, NKLA
```

### WebSocket subscription
`run_paper_mode()` loads `state/universe_today.csv` at startup and subscribes the WebSocket to all tickers in that file. Bars received during the session drive pattern detection for the intraday watchlist.

---

## Tier 3 — Intraday Watchlist

### Criteria (all must pass)
| # | Criterion | Threshold | Config key |
|---|---|---|---|
| C1 | Price | $1.00 – $20.00 | `scanner.price_min/max` |
| C2 | Gap % | ≥ 10% vs prev close | `scanner.gap_percent_min` |
| C3 | Relative volume | ≥ 5× 30d average | `scanner.relative_volume_min` |
| C4 | News catalyst | Tier A or B (Finnhub + Groq LLM) | `scanner.require_news_catalyst` |
| C5 | Float | < 20M shares | `scanner.float_max` |

### Scanner timing
The scanner runs during two modes:

| Mode | Interval | Behaviour |
|---|---|---|
| `PRE_MARKET_PREP` (04:00–09:30) | 30s (`premarket_scan_interval_sec`) | Full scan, news ingest |
| `ACTIVE_TRADING` (09:30–15:55) | 300s (`intraday_scan_interval_sec`) | Watchlist refresh only |
| Other modes | — | Scanner stops automatically |

The scanner stops when `current_mode() not in (PRE_MARKET_PREP, ACTIVE_TRADING)` — no hardcoded stop hour.

### Intra-day additions
A ticker can join the watchlist mid-session if:
1. It is already in `state/universe_today.csv` (i.e., the WebSocket is already subscribed)
2. It develops a Tier A/B news catalyst during the session (checked via `news_items` DB table)
3. It meets all 5 criteria at the moment of the next scanner cycle

No WebSocket re-subscription is needed — the daily universe covers all candidates.

---

## Float Data

### Problem
Alpaca snapshot API does not include float shares. The 5th criterion (C5: float < 20M) requires an external source.

### Solution
**Finnhub `/stock/profile2`** endpoint returns `shareOutstanding` (millions).  
We treat this as a float proxy — accurate enough for the <20M filter.

### Cache
Stored in `ticker_reference` SQLite table:

| Column | Type | Notes |
|---|---|---|
| `ticker` | TEXT PK | |
| `fetched_at` | TEXT | ISO timestamp |
| `shares_outstanding` | INTEGER | shares_outstanding × 1 000 000 |
| `company_name` | TEXT | from `name` field |
| `sector` | TEXT | from `finnhubIndustry` field |

**TTL**: 7 days — float rarely changes significantly week-to-week.  
**Fallback**: 15 000 000 shares (conservative — won't falsely block a low-float stock).  
**Rate limit**: 1 call/sec (Finnhub free tier: 60/min).

### Module
`src/data/float_cache.py`  
Key exports: `get_float_shares(ticker) → int`, `prefetch_floats(tickers) → dict`

---

## Known Limitations & Future Work

| Limitation | Impact | Planned fix |
|---|---|---|
| Alpaca IEX feed: no pre-market bars before 09:30 | Snapshots during 04:00–09:30 use `dailyBar` (prev day's close + today's gap vs pre-market) | Use SIP feed (paid tier) or Yahoo Finance pre-market |
| `shareOutstanding` ≠ `float` | Very minor — outstanding vs tradeable float difference is small for micro-caps | Polygon.io `weighted_shares_outstanding` if precision needed |
| Base universe rebuild is manual | Stale symbols over time | Automate with weekly systemd timer |
| No real-time Finnhub webhook | Breaking news mid-session not instantly picked up | Implement Finnhub WebSocket feed |
| IEX feed may not carry all small-caps | Bars missing for thinly traded stocks | Cross-check with SIP feed for the watchlist top-5 |

---

## Operations Runbook

### Morning routine (automated via pre_market_prep.py)
```
04:00  universe_refresh   → state/universe_today.csv (skip if fresh)
04:10  news_ingest        → news_items table (Finnhub)
06:00  watchlist_rank     → _state.watchlist pre-populated
07:00  universe_refresh_2 → forced re-run after news
08:30  perplexity_briefing → market context
09:20  universe_refresh_3 → forced re-run, latest prices
09:25  t5_alert           → Discord "opens in 5 min"
09:30  ACTIVE_TRADING     → bars arrive, scanner runs every 5 min
```

### Verify universe health
```bash
uv run python scripts/test_universe_refresh.py
# Shows: base count, top-20 gappers, sector distribution
# Posts summary to Discord
```

### Rebuild base universe (weekly)
```bash
uv run python scripts/build_base_universe.py
# Writes data/base_universe.csv (~3000–5000 tickers)
```

### Diagnose scanner / watchlist issues
```bash
uv run python scripts/diagnose_scanner.py
# Shows each ticker's pass/fail on all 5 criteria
uv run python scripts/diagnose_scanner.py --ticker AAPL
# Deep-dive single ticker with raw Alpaca response
```
