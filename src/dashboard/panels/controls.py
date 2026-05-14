from __future__ import annotations

"""
Controls panel — sidebar: pause/resume, mode selector, risk params viewer.
"""

import yaml
import streamlit as st
from pathlib import Path
from src.dashboard.data import (
    get_bot_status, request_pause, request_resume,
    switch_trading_mode, log_dashboard_action
)


def _load_risk_config() -> dict:
    cfg_path = Path(__file__).parent.parent.parent.parent / "config.yaml"
    try:
        with open(cfg_path) as f:
            return (yaml.safe_load(f) or {}).get("risk", {})
    except Exception:
        return {}


def render() -> None:
    status = get_bot_status()

    st.subheader("⚙️ Controls")

    # ── 1. Pause / Resume ──────────────────────────────────────────────────
    st.markdown("**Pause bot**")
    paused = status["paused"]

    if paused:
        st.warning(f"⏸️ Paused: *{status.get('pause_reason', '—')}*")
        if st.button("▶️ Resume bot", use_container_width=True):
            st.session_state["confirm_resume"] = True
    else:
        if st.button("⏸️ Pause bot", use_container_width=True):
            st.session_state["show_pause_form"] = True

    # Pause form
    if st.session_state.get("show_pause_form"):
        with st.form("pause_form"):
            reason = st.text_input(
                "Reason for pause (required)",
                placeholder="e.g. Feeling uncertain about market direction",
            )
            submitted = st.form_submit_button("Confirm pause")
            if submitted:
                if reason.strip():
                    request_pause(reason.strip())
                    st.session_state.pop("show_pause_form", None)
                    st.success("Bot will pause after current cycle.")
                    st.rerun()
                else:
                    st.error("You must provide a reason.")

    # Resume confirmation
    if st.session_state.get("confirm_resume"):
        st.warning("Resume bot? It will start trading again on next signal.")
        c1, c2 = st.columns(2)
        with c1:
            if st.button("✅ Resume", use_container_width=True):
                request_resume()
                st.session_state.pop("confirm_resume", None)
                st.success("Bot resumed.")
                st.rerun()
        with c2:
            if st.button("❌ Cancel", use_container_width=True):
                st.session_state.pop("confirm_resume", None)
                st.rerun()

    st.divider()

    # ── 2. Mode selector ───────────────────────────────────────────────────
    st.markdown("**Trading mode**")
    current_mode_label = "🔴 Live" if "live" in status["systemd_status"] else "📄 Paper"
    st.caption(f"Current: `{current_mode_label}`")

    new_mode = st.radio(
        "Switch mode",
        ["Paper", "Live"],
        horizontal=True,
        label_visibility="collapsed",
    )

    if st.button(f"Switch to {new_mode}", use_container_width=True):
        if new_mode == "Live":
            st.session_state["confirm_live_mode"] = True
        else:
            st.session_state["confirm_paper_mode"] = True

    # Live mode typed confirmation
    if st.session_state.get("confirm_live_mode"):
        st.error("⚠️ You are switching to **LIVE TRADING** with real money.")
        typed = st.text_input('Type "I UNDERSTAND LIVE TRADING" to confirm')
        c1, c2 = st.columns(2)
        with c1:
            if st.button("🔴 SWITCH TO LIVE", type="primary", use_container_width=True):
                if typed == "I UNDERSTAND LIVE TRADING":
                    ok = switch_trading_mode("live")
                    st.session_state.pop("confirm_live_mode", None)
                    if ok:
                        st.success("Switched to live. Bot restarting...")
                    else:
                        st.error("Switch failed — check systemctl logs.")
                    st.rerun()
                else:
                    st.error("Confirmation text doesn't match.")
        with c2:
            if st.button("❌ Cancel", use_container_width=True):
                st.session_state.pop("confirm_live_mode", None)
                st.rerun()

    # Paper mode confirmation
    if st.session_state.get("confirm_paper_mode"):
        st.warning("Switch back to paper trading?")
        c1, c2 = st.columns(2)
        with c1:
            if st.button("✅ Yes", use_container_width=True):
                ok = switch_trading_mode("paper")
                st.session_state.pop("confirm_paper_mode", None)
                if ok:
                    st.success("Switched to paper. Bot restarting...")
                else:
                    st.error("Switch failed.")
                st.rerun()
        with c2:
            if st.button("❌ Cancel", use_container_width=True):
                st.session_state.pop("confirm_paper_mode", None)
                st.rerun()

    st.divider()

    # ── 3. Risk parameters (read-only) ─────────────────────────────────────
    st.markdown("**Risk parameters** *(read-only)*")
    risk = _load_risk_config()
    if risk:
        st.caption(f"R/trade: **{risk.get('max_risk_per_trade_pct', '?') * 100:.0f}%**")
        st.caption(f"Daily max loss: **{risk.get('daily_max_loss_pct', '?') * 100:.0f}%**")
        st.caption(f"Min R:R: **{risk.get('profit_loss_ratio_min', '?')}:1**")
        st.caption(f"Max consecutive losses: **{risk.get('max_consecutive_losses', '?')}**")
        st.caption(f"Max trades/day: **{risk.get('max_trades_per_day', '?')}**")
    st.info("To modify: edit `config.yaml` and restart bot via SSH.", icon="ℹ️")
