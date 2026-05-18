from __future__ import annotations

"""Health panel — connectivity status per external service."""

import streamlit as st
from src.dashboard.data import get_connectivity_health
from src.dashboard.utils import health_icon, time_ago

# Staleness thresholds per service (seconds).
# warn_seconds  = interval * 2  →  🟡
# error_seconds = interval * 5  →  🔴
#
# These are NOT polling frequencies — they're "expected max gap" per call pattern:
#   Alpaca:     scanner runs pre-market every ~25s; after 10:00 ET only fires on trades.
#               Warn after 90 min (no scanner + no trades is normal mid-session).
#   Finnhub:    scanner stops at 10:00; overnight sweeps at 20:00/22:00/02:00.
#               Warn after 3 hours (daytime silence is expected).
#   Discord:    only on startup / entry / force_close — warn after 4 hours.
#   Groq:       per-headline during news ingestion (pre-market + overnight).
#               Warn after 3 hours.
#   Anthropic:  EOD / post-trade analysis — once per day at most. Warn after 24 hours.
#   Perplexity: pre-market briefing once per trading day. Warn after 28 hours.
_SERVICE_INTERVALS = {
    "Alpaca":     2700,    # warn >90 min, error >225 min
    "Finnhub":    5400,    # warn >3 h,   error >7.5 h
    "Discord":    7200,    # warn >4 h,   error >10 h
    "Groq":       5400,    # warn >3 h,   error >7.5 h
    "Anthropic":  43200,   # warn >24 h,  error >60 h
    "Perplexity": 50400,   # warn >28 h,  error >70 h
}


def render() -> None:
    st.subheader("🔌 Connectivity")
    health = get_connectivity_health()

    for service, last_seen in health.items():
        interval = _SERVICE_INTERVALS.get(service, 300)
        icon = health_icon(last_seen, warn_seconds=interval * 2, error_seconds=interval * 5)
        age  = time_ago(last_seen) if last_seen else "never"
        st.caption(f"{icon} **{service}** — {age}")

    st.divider()
    st.caption("🟢 normal  🟡 delayed  🔴 issue  ⚪ no data")
