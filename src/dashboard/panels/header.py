from __future__ import annotations

"""
Header panel — status badge, mode, balance, P&L, kill switch.
Auto-refreshed every 5s by the main app.
"""

import streamlit as st
from src.dashboard.data import get_bot_status, get_today_stats, get_account_value
from src.dashboard.data import trigger_kill_switch, clear_kill_switch, log_dashboard_action
from src.dashboard.utils import fmt_currency, fmt_pct, mode_badge, pnl_color


def render() -> None:
    status = get_bot_status()
    stats  = get_today_stats()
    acct   = get_account_value()

    col_status, col_mode, col_balance, col_pnl, col_kill = st.columns([2, 2, 2, 2, 1])

    # ── Status badge ──────────────────────────────────────────────────────
    with col_status:
        if status["kill_switch_active"]:
            st.error("🔴 KILLED", icon=None)
        elif status["paused"]:
            st.warning(f"⏸️ PAUSED", icon=None)
            if status.get("pause_reason"):
                st.caption(f"Reason: {status['pause_reason']}")
        elif status["systemd_status"] == "active":
            st.success("🟢 RUNNING", icon=None)
        elif status["systemd_status"] == "local-dev":
            st.info("💻 LOCAL DEV", icon=None)
        else:
            st.error(f"❌ {status['systemd_status'].upper()}", icon=None)

    # ── Mode badge ────────────────────────────────────────────────────────
    with col_mode:
        st.markdown(f"**{mode_badge(status['mode'])}**")

    # ── Account balance ───────────────────────────────────────────────────
    with col_balance:
        st.metric("Account", fmt_currency(acct))

    # ── Today P&L ─────────────────────────────────────────────────────────
    with col_pnl:
        pnl = stats["gross_pnl"]
        color = pnl_color(pnl)
        delta = fmt_pct(pnl / acct * 100, sign=True) if acct > 0 else ""
        st.metric(
            "Today P&L",
            fmt_currency(pnl, sign=True),
            delta=delta,
            delta_color="normal",
        )

    # ── Kill switch ───────────────────────────────────────────────────────
    with col_kill:
        if status["kill_switch_active"]:
            if st.button("✅ CLEAR KILL", type="secondary", use_container_width=True):
                st.session_state["confirm_clear_kill"] = True
        else:
            if st.button("🛑 KILL", type="primary", use_container_width=True):
                st.session_state["confirm_kill"] = True

    # ── Kill switch confirmation modal ────────────────────────────────────
    if st.session_state.get("confirm_kill"):
        with st.container():
            st.error(
                "⚠️ **CONFIRM KILL SWITCH**\n\n"
                "All open positions will be closed at market price. Bot halts immediately."
            )
            c1, c2 = st.columns(2)
            with c1:
                if st.button("✅ YES — KILL THE BOT", type="primary", use_container_width=True):
                    trigger_kill_switch()
                    st.session_state.pop("confirm_kill", None)
                    st.rerun()
            with c2:
                if st.button("❌ Cancel", use_container_width=True):
                    st.session_state.pop("confirm_kill", None)
                    st.rerun()

    if st.session_state.get("confirm_clear_kill"):
        with st.container():
            st.warning("Clear kill switch? Bot will resume on next start.")
            c1, c2 = st.columns(2)
            with c1:
                if st.button("✅ Clear", type="primary", use_container_width=True):
                    clear_kill_switch()
                    st.session_state.pop("confirm_clear_kill", None)
                    st.rerun()
            with c2:
                if st.button("❌ Cancel", use_container_width=True):
                    st.session_state.pop("confirm_clear_kill", None)
                    st.rerun()

    st.divider()
