from __future__ import annotations

"""Health panel — connectivity status per external service."""

import streamlit as st
from src.dashboard.data import get_connectivity_health
from src.dashboard.utils import health_icon, time_ago

# Expected interval between service calls (seconds) — used for color thresholds
_SERVICE_INTERVALS = {
    "Alpaca":      60,
    "Finnhub":     120,
    "Discord":     300,
    "Groq":        600,
    "Anthropic":   7200,
    "Perplexity":  86400,
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
