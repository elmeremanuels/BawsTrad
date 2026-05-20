from __future__ import annotations

"""
Universe Builder — System 5 (PROBLEEM 5).

Programmatic implementation of the three-tier universe pipeline.
Called by scripts/refresh_universe.py (CLI) and pre_market_prep.py (automated).

Pipeline (4 waves):
  Wave 0 — base_universe:   All active common stocks NYSE/NASDAQ/AMEX/ARCA
            (~3 000-5 000 tickers, rebuilt weekly via build_base_universe.py)
  Wave 1 — price_gap:       Price $1-$20, gap ≥ min_gap_pct vs prev close
  Wave 2 — volume:          Pre-market volume above floor (skipped when data unavailable)
  Wave 3 — float:           Float < float_max_shares (Finnhub cache, 7-day TTL)
  Wave 4 — news:            Tier A or B news catalyst present in DB
            → writes state/universe_today.csv, logs each wave to funnel_daily

Config keys (all from config.yaml scanner section):
  universe_gap_filter_pct   — min gap% for daily universe (default 3.0)
  price_min / price_max     — $1.00 / $20.00
  float_max                 — 20_000_000  (wave-3 threshold for intraday watchlist)
  universe_float_wave3_max  — 30_000_000  (wave-3 threshold, wider for daily universe)
  universe_file             — state/universe_today.csv
  universe_fallback         — small_cap_runners.csv
"""

import csv
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from src.health.funnel_tracker import log_funnel
from src.storage.db import get_connection

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# ─── Config defaults (overridden by config.yaml) ─────────────────────────────

_DEFAULTS = {
    "price_min":              1.00,
    "price_max":             20.00,
    "universe_gap_filter_pct": 3.0,
    "universe_float_wave3_max": 30_000_000,
    "universe_file":          "state/universe_today.csv",
    "universe_fallback":      "small_cap_runners.csv",
}


def _load_config() -> dict:
    try:
        with open(_PROJECT_ROOT / "config.yaml") as f:
            cfg = yaml.safe_load(f) or {}
        return {**_DEFAULTS, **cfg.get("scanner", {})}
    except Exception:
        return dict(_DEFAULTS)


# ─── Wave helpers ─────────────────────────────────────────────────────────────

def _load_base_universe() -> List[Dict]:
    """
    Load base_universe.csv (or fallback).
    Returns list of {ticker, exchange, name}.
    """
    candidates = [
        _PROJECT_ROOT / "data" / "base_universe.csv",
        _PROJECT_ROOT / "data" / "small_cap_base_universe.csv",
        _PROJECT_ROOT / "small_cap_runners.csv",
    ]
    for path in candidates:
        if path.exists():
            with open(path, newline="") as f:
                rows = list(csv.DictReader(f))
            if rows:
                logger.info("universe_builder: base_universe from %s (%d tickers)", path.name, len(rows))
                return rows
    logger.warning("universe_builder: no base universe CSV found")
    return []


def _wave1_price_gap(
    base: List[Dict],
    snapshots: Dict[str, dict],
    price_min: float,
    price_max: float,
    gap_min_pct: float,
) -> List[Dict]:
    """
    Filter by price $price_min–$price_max and gap ≥ gap_min_pct vs prev close.
    Adds keys: price, prev_close, gap_pct.
    """
    result = []
    for row in base:
        ticker = row["ticker"]
        snap = snapshots.get(ticker)
        if not snap:
            continue
        prev_bar = snap.get("prevDailyBar") or {}
        daily    = snap.get("dailyBar") or {}
        prev_close = prev_bar.get("c", 0)
        price = daily.get("c") or daily.get("o") or 0
        if prev_close <= 0 or price <= 0:
            continue
        if not (price_min <= price <= price_max):
            continue
        gap_pct = (price - prev_close) / prev_close * 100.0
        if gap_pct < gap_min_pct:
            continue
        result.append({
            **row,
            "price":      round(price, 4),
            "prev_close": round(prev_close, 4),
            "gap_pct":    round(gap_pct, 2),
        })
    return result


def _wave2_volume(tickers_data: List[Dict], snapshots: Dict[str, dict]) -> List[Dict]:
    """
    Soft volume filter: keep tickers where dailyBar.v > 0.
    Pre-market volume is unreliable before 09:30 on IEX, so this is a loose pass.
    Upgrade to stricter threshold once SIP feed is available.
    """
    result = []
    for row in tickers_data:
        snap = snapshots.get(row["ticker"])
        if not snap:
            continue
        vol = (snap.get("dailyBar") or {}).get("v", 0)
        if vol > 0:
            result.append(row)
    return result


def _wave3_float(tickers_data: List[Dict], float_max: int) -> List[Dict]:
    """
    Filter by float < float_max using Finnhub cache (float_cache module).
    Adds key: float_shares.
    """
    from src.data.float_cache import get_float_shares

    result = []
    for row in tickers_data:
        ticker = row["ticker"]
        float_shares = get_float_shares(ticker)
        if float_shares < float_max:
            result.append({**row, "float_shares": float_shares})
    return result


def _wave4_news(tickers_data: List[Dict]) -> List[Dict]:
    """
    Filter to tickers with a Tier A or B news catalyst in DB (today only).
    Adds key: news_tier.
    """
    from src.news.ingest import get_best_catalyst_today

    result = []
    for row in tickers_data:
        ticker = row["ticker"]
        best = get_best_catalyst_today(ticker)
        if best and best.get("tier") in ("A", "B"):
            result.append({**row, "news_tier": best["tier"]})
    return result


def _top30_fallback(wave1: List[Dict]) -> List[Dict]:
    """
    If normal filtering leaves no tickers (quiet day), return top-30 by gap_pct.
    Ensures scanner always has candidates.
    """
    return sorted(wave1, key=lambda r: r.get("gap_pct", 0), reverse=True)[:30]


def _write_universe(rows: List[Dict], path: Path) -> None:
    """Write universe CSV. Creates parent dirs if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["ticker", "float_shares", "sector", "gap_pct", "price", "news_tier"]
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({
                "ticker":       row.get("ticker", ""),
                "float_shares": row.get("float_shares", 15_000_000),
                "sector":       row.get("sector", row.get("name", "")),
                "gap_pct":      row.get("gap_pct", 0),
                "price":        row.get("price", 0),
                "news_tier":    row.get("news_tier", ""),
            })


# ─── Main builder ─────────────────────────────────────────────────────────────

class UniverseBuilder:
    """
    Builds state/universe_today.csv from scratch using the 4-wave funnel.
    Logs each wave count to funnel_daily via FunnelTracker.

    Usage:
        from src.universe.dynamic_builder import UniverseBuilder
        result = UniverseBuilder().build()
    """

    REFRESH_TIMES_ET = ["04:00", "07:00", "08:30", "09:25"]

    def __init__(self) -> None:
        self._cfg = _load_config()

    def build(self, force: bool = False) -> List[Dict]:
        """
        Run the full 4-wave pipeline.

        Args:
            force: if False, skip if universe_today.csv is already fresh today.

        Returns:
            List of ticker dicts that passed all waves.
        """
        cfg = self._cfg
        out_path = _PROJECT_ROOT / cfg["universe_file"]

        # Skip if already fresh (04:00 slot)
        if not force and out_path.exists():
            mtime = datetime.fromtimestamp(out_path.stat().st_mtime).date()
            if mtime >= date.today():
                logger.info("universe_builder: already fresh today, skipping (force=False)")
                return self._load_current(out_path)

        # ── Wave 0 — base universe ────────────────────────────────────────────
        base = _load_base_universe()
        log_funnel("base_universe", len(base))
        if not base:
            logger.warning("universe_builder: base_universe empty — aborting build")
            return []

        # ── Fetch snapshots (single batch call) ───────────────────────────────
        tickers = [r["ticker"] for r in base]
        snapshots = self._fetch_snapshots(tickers)

        # ── Wave 1 — price + gap ──────────────────────────────────────────────
        wave1 = _wave1_price_gap(
            base, snapshots,
            float(cfg["price_min"]),
            float(cfg["price_max"]),
            float(cfg["universe_gap_filter_pct"]),
        )
        log_funnel("wave1_price_gap", len(wave1))
        logger.info("universe_builder: wave1=%d (price $%.0f-$%.0f, gap≥%.1f%%)",
                    len(wave1), cfg["price_min"], cfg["price_max"],
                    cfg["universe_gap_filter_pct"])

        # ── Wave 2 — volume ───────────────────────────────────────────────────
        wave2 = _wave2_volume(wave1, snapshots)
        log_funnel("wave2_volume", len(wave2))

        # ── Fallback: if wave2 too small, use top-30 by gap ──────────────────
        if len(wave2) < 5 and wave1:
            logger.info("universe_builder: wave2 thin (%d), applying top-30 gap fallback", len(wave2))
            wave2 = _top30_fallback(wave1)
            log_funnel("wave2_volume", len(wave2))

        # ── Wave 3 — float ────────────────────────────────────────────────────
        float_max = int(cfg.get("universe_float_wave3_max",
                                cfg.get("float_max", 30_000_000)))
        wave3 = _wave3_float(wave2, float_max)
        log_funnel("wave3_float", len(wave3))
        logger.info("universe_builder: wave3=%d (float < %dM)", len(wave3), float_max // 1_000_000)

        # ── Wave 4 — news catalyst ────────────────────────────────────────────
        # Only applied during ACTIVE_TRADING hours; pre-market we include wave3 output
        # so we don't miss stocks that develop news before open.
        # wave4 is informational — all of wave3 goes into universe_today.csv,
        # with news_tier populated where available.
        wave4 = _wave4_news(wave3)
        log_funnel("wave4_news", len(wave4))

        # Universe = wave3 with news_tier annotation (not hard-filtered on news)
        # The intraday scanner applies the hard news requirement.
        final = wave3  # news_tier populated where found
        for row in final:
            if not row.get("news_tier"):
                # Try to populate news_tier even if not Tier A/B
                from src.news.ingest import get_best_catalyst_today
                best = get_best_catalyst_today(row["ticker"])
                if best:
                    row["news_tier"] = best.get("tier", "")

        # ── Write output ──────────────────────────────────────────────────────
        _write_universe(final, out_path)
        logger.info("universe_builder: wrote %d tickers → %s", len(final), out_path)

        self._post_discord_report(base, wave1, wave2, wave3, wave4, final)
        return final

    def _fetch_snapshots(self, tickers: List[str]) -> Dict[str, dict]:
        try:
            from src.data.alpaca_client import get_snapshots
            result: Dict[str, dict] = {}
            batch = 100
            for i in range(0, len(tickers), batch):
                chunk = tickers[i : i + batch]
                result.update(get_snapshots(chunk))
            return result
        except Exception as exc:
            logger.error("universe_builder: snapshot fetch failed: %s", exc)
            return {}

    @staticmethod
    def _load_current(path: Path) -> List[Dict]:
        if not path.exists():
            return []
        with open(path, newline="") as f:
            return list(csv.DictReader(f))

    def _post_discord_report(
        self,
        base: List, wave1: List, wave2: List, wave3: List, wave4: List, final: List,
    ) -> None:
        try:
            import asyncio
            from src.alerts.discord import post_message
            from src.config import settings

            top5 = sorted(final, key=lambda r: float(r.get("gap_pct", 0) or 0), reverse=True)[:5]
            top5_lines = "".join(
                f"\n   • **{r['ticker']}** {float(r.get('gap_pct',0)):+.1f}% "
                f"{'[' + r['news_tier'] + ']' if r.get('news_tier') else ''}"
                for r in top5
            )
            msg = (
                f"🌐 **Universe built** {date.today()}\n"
                f"   Base {len(base)} → Wave1 {len(wave1)} → Wave2 {len(wave2)} "
                f"→ Wave3 {len(wave3)} → w/news {len(wave4)}\n"
                f"   Final: **{len(final)} tickers**{top5_lines}"
            )
            asyncio.run(post_message(settings.DISCORD_WEBHOOK_URL, msg))
        except Exception as exc:
            logger.debug("universe_builder: Discord report failed: %s", exc)
