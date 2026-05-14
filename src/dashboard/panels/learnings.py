from __future__ import annotations

"""Learnings panel — active rules, shadow rules, pending suggestions."""

import streamlit as st
from src.dashboard.data import get_learnings, log_dashboard_action
from src.dashboard.utils import time_ago


def render() -> None:
    tab_active, tab_pending = st.tabs(["✅ Active learnings", "📬 Pending suggestions"])

    # ── Active learnings ──────────────────────────────────────────────────
    with tab_active:
        learnings = get_learnings(status="active")
        if not learnings:
            st.info("No active learnings yet. The bot will generate these after trades.")
        else:
            for l in learnings:
                with st.expander(
                    f"💡 {l.get('observation', 'Learning')[:80]} "
                    f"— conf {l.get('confidence', 0):.0%} | {time_ago(l.get('created_at', ''))}"
                ):
                    st.markdown(f"**Observation:** {l.get('observation', '—')}")
                    st.markdown(f"**Quantification:** {l.get('quantification', '—')}")
                    if l.get("suggested_change"):
                        st.markdown(f"**Suggested change:** {l.get('suggested_change')}")
                    st.caption(
                        f"Trigger: {l.get('trigger', '?')} | "
                        f"Scope: {l.get('scope', '?')} | "
                        f"Created: {l.get('created_at', '?')[:10]}"
                    )

    # ── Pending (not-yet-applied) learnings ───────────────────────────────
    with tab_pending:
        pending = get_learnings(status=None)
        pending = [l for l in pending if not l.get("applied")]

        if not pending:
            st.info("No pending suggestions. Check back after the first EOD reflection.")
        else:
            for l in pending:
                with st.expander(
                    f"📬 {l.get('observation', 'Suggestion')[:80]} "
                    f"— conf {l.get('confidence', 0):.0%}"
                ):
                    st.markdown(f"**Observation:** {l.get('observation', '—')}")
                    st.markdown(f"**Quantification:** {l.get('quantification', '—')}")
                    if l.get("suggested_change"):
                        st.markdown(f"**Suggested change:** `{l.get('suggested_change')}`")

                    c1, c2 = st.columns(2)
                    with c1:
                        if st.button(f"✅ Accept #{l['id'][:8]}", key=f"accept_{l['id']}"):
                            # Mark as applied via DB (learning review is intentionally manual)
                            log_dashboard_action("learning_accepted", {"id": l["id"]})
                            st.success("Accepted — will apply at next bot restart.")
                    with c2:
                        if st.button(f"❌ Reject #{l['id'][:8]}", key=f"reject_{l['id']}"):
                            log_dashboard_action("learning_rejected", {"id": l["id"]})
                            st.warning("Rejected and logged.")
