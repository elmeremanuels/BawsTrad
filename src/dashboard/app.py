from __future__ import annotations

"""
BawsTrad — Streamlit trading dashboard.

Run:
  uv run streamlit run src/dashboard/app.py --server.address 0.0.0.0 --server.port 8501

Access: http://baws-bot:8501 via Tailscale (or http://localhost:8501 locally).
Auth: Tailscale VPN only — no password required.
"""

import streamlit as st
from streamlit_autorefresh import st_autorefresh

# ── Page config — must be first Streamlit call ────────────────────────────────
st.set_page_config(
    page_title="BawsTrad",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="auto",  # collapsed on mobile
    menu_items={
        "Get Help": None,
        "Report a bug": None,
        "About": "BawsTrad momentum day-trading bot dashboard",
    },
)

# ── Auto-refresh (header + messages need this) ────────────────────────────────
st_autorefresh(interval=5000, key="global_refresh")

# ── Import panels ─────────────────────────────────────────────────────────────
from src.dashboard.panels import (
    header,
    controls,
    stats,
    activity,
    messages,
    learnings,
    health,
    insights,
)

# ── Custom CSS for mobile-friendly kill switch ────────────────────────────────
st.markdown("""
<style>
/* Make kill switch button bigger on mobile */
button[kind="primary"] {
    min-height: 48px;
    font-size: 1rem;
}
/* Tighter spacing on small screens */
@media (max-width: 640px) {
    .block-container { padding: 0.5rem 0.5rem 2rem; }
}
</style>
""", unsafe_allow_html=True)

# ── Layout ────────────────────────────────────────────────────────────────────

# Header — full width, always visible
header.render()

# Sidebar
with st.sidebar:
    controls.render()
    st.divider()
    health.render()

# Main content — 4 tabs
tab_stats, tab_activity, tab_messages, tab_learnings = st.tabs([
    "📊 Stats",
    "🎯 Activity",
    "💬 Messages",
    "🧠 Learnings",
])

with tab_stats:
    stats.render()
    st.divider()
    insights.render()

with tab_activity:
    activity.render()

with tab_messages:
    messages.render()

with tab_learnings:
    learnings.render()
