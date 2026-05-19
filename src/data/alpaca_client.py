from __future__ import annotations

"""
Alpaca REST + WebSocket client for paper trading.

REST:  account info, positions, order placement/cancellation
WS:    real-time 1-min bars via IEX stream
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

import httpx
import websockets

from src.config import settings
from src.patterns.candles import Candle

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")

_REST_BASE   = settings.ALPACA_BASE_URL           # paper-api.alpaca.markets
_DATA_BASE   = settings.ALPACA_DATA_URL           # data.alpaca.markets
_WS_DATA_URL = "wss://stream.data.alpaca.markets/v2/iex"

_HEADERS = {
    "APCA-API-KEY-ID":     settings.ALPACA_API_KEY,
    "APCA-API-SECRET-KEY": settings.ALPACA_API_SECRET,
    "Content-Type":        "application/json",
}


# ─── REST helpers ─────────────────────────────────────────────────────────────

def _rest(method: str, path: str, **kwargs) -> dict:
    url = f"{_REST_BASE}{path}"
    with httpx.Client(headers=_HEADERS, timeout=15.0) as c:
        r = c.request(method, url, **kwargs)
        r.raise_for_status()
        return r.json() if r.content else {}


def _data_rest(path: str, params: Optional[dict] = None) -> dict:
    url = f"{_DATA_BASE}{path}"
    with httpx.Client(headers=_HEADERS, timeout=15.0) as c:
        r = c.get(url, params=params or {})
        r.raise_for_status()
        return r.json()


# ─── Account & positions ──────────────────────────────────────────────────────

def get_account() -> dict:
    return _rest("GET", "/v2/account")


def get_positions() -> List[dict]:
    return _rest("GET", "/v2/positions") or []


def get_open_orders() -> List[dict]:
    return _rest("GET", "/v2/orders", params={"status": "open"}) or []


# ─── Snapshots (for pre-market scanning) ─────────────────────────────────────

def get_snapshots(tickers: List[str]) -> Dict[str, dict]:
    """
    Fetch latest bar + daily bar for a list of tickers.
    Returns {ticker: snapshot_dict}.
    """
    if not tickers:
        return {}
    # Alpaca allows max 100 tickers per call
    result: Dict[str, dict] = {}
    for chunk_start in range(0, len(tickers), 100):
        chunk = tickers[chunk_start:chunk_start + 100]
        data = _data_rest("/v2/stocks/snapshots", params={"symbols": ",".join(chunk), "feed": "iex"})
        result.update(data)
    return result


def get_all_tradeable_assets() -> List[dict]:
    """
    Fetch all active, tradeable US equity assets from Alpaca.
    Returns a list of asset dicts (symbol, name, exchange, tradable, status).
    Used by scripts/build_base_universe.py to build the static base universe.
    Returns [] on error.
    """
    try:
        assets: List[dict] = []
        # Alpaca returns up to 10 000 assets per call for us_equity class
        data = _rest("GET", "/v2/assets", params={
            "status":      "active",
            "asset_class": "us_equity",
        })
        if isinstance(data, list):
            assets = data
        logger.info("get_all_tradeable_assets: fetched %d assets", len(assets))
        return assets
    except Exception as exc:
        logger.warning("get_all_tradeable_assets failed: %s", exc)
        return []


def get_latest_bars(tickers: List[str]) -> Dict[str, dict]:
    """Latest 1-min bar for each ticker."""
    if not tickers:
        return {}
    data = _data_rest("/v2/stocks/bars/latest", params={"symbols": ",".join(tickers), "feed": "iex"})
    return data.get("bars", {})


# ─── Orders ───────────────────────────────────────────────────────────────────

@dataclass
class BracketOrder:
    symbol: str
    qty: int
    entry_price: float      # Market order — this is just for logging
    take_profit_price: float
    stop_loss_price: float
    stop_limit_price: float  # Slightly below stop for safety


def place_bracket_order(order: BracketOrder) -> dict:
    """Place a bracket order (entry + take_profit + stop_loss) via Alpaca paper API."""
    payload = {
        "symbol":        order.symbol,
        "qty":           str(order.qty),
        "side":          "buy",
        "type":          "market",
        "time_in_force": "day",
        "order_class":   "bracket",
        "take_profit": {
            "limit_price": str(round(order.take_profit_price, 2)),
        },
        "stop_loss": {
            "stop_price":  str(round(order.stop_loss_price, 2)),
            "limit_price": str(round(order.stop_limit_price, 2)),
        },
    }
    logger.info("Placing bracket order: %s qty=%d tp=%.2f sl=%.2f",
                order.symbol, order.qty, order.take_profit_price, order.stop_loss_price)
    return _rest("POST", "/v2/orders", json=payload)


def cancel_order(order_id: str) -> None:
    try:
        _rest("DELETE", f"/v2/orders/{order_id}")
        logger.info("Cancelled order %s", order_id)
    except httpx.HTTPStatusError as e:
        logger.warning("Cancel order %s failed: %s", order_id, e)


def cancel_all_orders() -> None:
    try:
        _rest("DELETE", "/v2/orders")
        logger.info("All open orders cancelled")
    except Exception as e:
        logger.error("cancel_all_orders failed: %s", e)


def close_all_positions() -> None:
    try:
        _rest("DELETE", "/v2/positions")
        logger.info("All positions closed")
    except Exception as e:
        logger.error("close_all_positions failed: %s", e)


def close_position(symbol: str) -> None:
    try:
        _rest("DELETE", f"/v2/positions/{symbol}")
        logger.info("Closed position: %s", symbol)
    except Exception as e:
        logger.error("close_position %s failed: %s", symbol, e)


# ─── WebSocket market data stream ─────────────────────────────────────────────

async def stream_bars(
    tickers: List[str],
    on_bar: Callable[[Candle], None],
    stop_event: Optional[asyncio.Event] = None,
) -> None:
    """
    Connect to Alpaca IEX data stream and deliver 1-min bars to `on_bar`.
    Reconnects automatically on disconnect.
    `stop_event`: when set, exits the loop cleanly.
    """
    if stop_event is None:
        stop_event = asyncio.Event()

    while not stop_event.is_set():
        try:
            async with websockets.connect(_WS_DATA_URL, ping_interval=20) as ws:
                # Authenticate
                auth = await ws.recv()
                logger.debug("WS auth msg: %s", auth[:100])

                await ws.send(json.dumps({
                    "action": "auth",
                    "key": settings.ALPACA_API_KEY,
                    "secret": settings.ALPACA_API_SECRET,
                }))
                auth_resp = await ws.recv()
                logger.debug("WS auth response: %s", auth_resp[:100])

                # Subscribe to bars
                await ws.send(json.dumps({
                    "action": "subscribe",
                    "bars": tickers,
                }))
                logger.info("WS subscribed to bars: %s", tickers)

                async for raw in ws:
                    if stop_event.is_set():
                        break
                    try:
                        msgs = json.loads(raw)
                        for msg in (msgs if isinstance(msgs, list) else [msgs]):
                            if msg.get("T") == "b":
                                bar_time = datetime.fromisoformat(
                                    msg["t"].replace("Z", "+00:00")
                                )
                                candle = Candle(
                                    bar_time=bar_time,
                                    open=msg["o"],
                                    high=msg["h"],
                                    low=msg["l"],
                                    close=msg["c"],
                                    volume=int(msg["v"]),
                                    vwap=msg.get("vw"),
                                )
                                candle._ticker = msg["S"]  # type: ignore[attr-defined]
                                on_bar(candle)
                    except Exception as exc:
                        logger.warning("WS parse error: %s", exc)

        except Exception as exc:
            if stop_event.is_set():
                break
            logger.warning("WS disconnected: %s — reconnecting in 5s", exc)
            await asyncio.sleep(5)

    logger.info("WS stream stopped")
