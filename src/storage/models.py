from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from uuid import uuid4


@dataclass
class ScanResult:
    ticker: str
    price: float
    gap_pct: float
    rel_vol: float
    float_shares: int
    news_tier: Optional[str]
    quality_score: float
    passed_filter: bool
    id: str = field(default_factory=lambda: str(uuid4()))
    scanned_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class NewsItem:
    ticker: str
    headline: str
    source: str
    tier: str = "C"
    category: str = ""
    sentiment: float = 0.0
    reasoning: str = ""
    id: str = field(default_factory=lambda: str(uuid4()))
    fetched_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class Trade:
    ticker: str
    side: str
    shares: int
    entry_price: float
    stop_price: float
    target_price: float
    setup_type: str
    mode: str = "paper"
    exit_price: Optional[float] = None
    pnl_dollars: Optional[float] = None
    pnl_r: Optional[float] = None
    exit_reason: Optional[str] = None
    news_item_id: Optional[str] = None
    briefing_id: Optional[str] = None
    closed_at: Optional[datetime] = None
    id: str = field(default_factory=lambda: str(uuid4()))
    opened_at: datetime = field(default_factory=datetime.utcnow)
