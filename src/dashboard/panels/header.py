from __future__ import annotations

"""
Header panel — status badge, mode, balance, P&L, symmetric START / KILL control.

State machine for the right-most action column:
  bot_state="running"  → 🛑 KILL button
  bot_state="stopped"
  bot_state="killed"   → ▶ START button (clears killswitch + starts service)
  bot_state="failed"   → ▶ START button + red crash banner with journal tail
  effective="starting" → disabled "Starting…" (session-state flag, TTL 30 s)
  bot_state="local-dev"→ no action button (dev mode indicator only)
"""

import time

import streamlit as st

from src.dashboard.data import (
    clear_kill_switch,
    get_account_value,
    get_bot_status,
    get_journalctl_tail,
    get_today_stats,
    log_dashboard_action,
    preflight_check,
    start_bot,
    trigger_kill_switch,
)
from src.dashboard.utils import fmt_currency, fmt_pct, mode_badge, pnl_color

# How long (seconds) to show "Starting…" after issuing systemctl start
_START_TIMEOUT_S = 30


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

def render() -> None:
    status = get_bot_status()
    stats  = get_today_stats()
    acct   = get_account_value()

    bot_state = status["bot_state"]  # "running"|"stopped"|"killed"|"failed"|"local-dev"

    # ── Resolve "starting" overlay from session state ─────────────────────
    starting_at: float | None = st.session_state.get("bot_starting_at")
    if starting_at is not None:
        if bot_state == "running":
            # Start succeeded — clear the overlay
            st.session_state.pop("bot_starting_at", None)
            starting_at = None
        elif time.time() - starting_at > _START_TIMEOUT_S:
            # Timed out — fall back to whatever systemd says
            st.session_state.pop("bot_starting_at", None)
            starting_at = None

    effective_state = "starting" if starting_at else bot_state

    # ── Header row ────────────────────────────────────────────────────────
    col_status, col_mode, col_balance, col_pnl, col_action = st.columns([2, 2, 2, 2, 1])

    with col_status:
        _render_status_badge(effective_state, status)

    with col_mode:
        st.markdown(f"**{mode_badge(status['mode'])}**")

    with col_balance:
        st.metric("Account", fmt_currency(acct))

    with col_pnl:
        pnl   = stats["gross_pnl"]
        delta = fmt_pct(pnl / acct * 100, sign=True) if acct > 0 else ""
        st.metric(
            "Today P&L",
            fmt_currency(pnl, sign=True),
            delta=delta,
            delta_color="normal",
        )

    with col_action:
        _render_action_button(effective_state, status)

    # ── Crash banner (systemd "failed" state) ────────────────────────────
    if effective_state == "failed":
        _render_crash_banner()

    # ── Start-failure banner (from the previous start attempt) ────────────
    if st.session_state.get("start_failed_error"):
        err  = st.session_state.pop("start_failed_error")
        logs = st.session_state.pop("start_failed_logs", "")
        st.error(f"🚨 **Failed to start bot:** {err}")
        if logs:
            with st.expander("📋 Journal output"):
                st.code(logs, language=None)

    # ── Modals ────────────────────────────────────────────────────────────
    if st.session_state.get("confirm_kill"):
        _render_kill_modal()

    if st.session_state.get("show_start_modal"):
        _render_start_modal(status)

    st.divider()


# ─────────────────────────────────────────────────────────────────────────────
# Sub-renderers
# ─────────────────────────────────────────────────────────────────────────────

def _render_status_badge(effective_state: str, status: dict) -> None:
    if effective_state == "starting":
        st.info("⏳ STARTING")
    elif effective_state == "killed":
        st.error("🔴 KILLED")
    elif effective_state == "failed":
        st.error("💀 FAILED")
    elif effective_state == "running":
        if status["paused"]:
            st.warning("⏸️ PAUSED")
            if status.get("pause_reason"):
                st.caption(f"Reason: {status['pause_reason']}")
        else:
            st.success("🟢 RUNNING")
    elif effective_state == "local-dev":
        st.info("💻 LOCAL DEV")
    else:  # stopped
        st.warning("⚫ STOPPED")


def _render_action_button(effective_state: str, status: dict) -> None:
    """Right-most column: one button whose label/type/action depends on state."""
    if effective_state == "local-dev":
        return  # No action in dev mode

    if effective_state == "starting":
        # Disabled spinner button — auto-refresh (5 s) will pick up "running"
        st.button(
            "⏳ Starting…",
            disabled=True,
            use_container_width=True,
            help="Bot is starting — this updates automatically.",
        )
        return

    if effective_state == "running":
        if st.button(
            "🛑 KILL",
            type="primary",
            use_container_width=True,
            help="Trigger kill switch: close all positions and halt the bot.",
        ):
            st.session_state["confirm_kill"] = True
            st.rerun()
        return

    # stopped | killed | failed → START
    btn_label = "▶ START BOT"
    if st.button(
        btn_label,
        type="primary",
        use_container_width=True,
        help="Clear kill switch (if present) and start the trading bot.",
    ):
        with st.spinner("Running pre-flight checks…"):
            checks = preflight_check()
        st.session_state["preflight_results"] = checks
        st.session_state["show_start_modal"] = True
        st.rerun()


def _render_crash_banner() -> None:
    st.error(
        "🚨 **Bot crashed or failed to start** — check the Messages tab for details.",
        icon="🚨",
    )
    logs = get_journalctl_tail(25)
    with st.expander("📋 Recent journal output (last 25 lines)"):
        st.code(logs, language=None)


def _render_kill_modal() -> None:
    with st.container(border=True):
        st.error(
            "⚠️ **CONFIRM KILL SWITCH**\n\n"
            "All open positions will be closed at market price. "
            "The bot halts immediately and will not restart automatically."
        )
        c1, c2 = st.columns(2)
        with c1:
            if st.button(
                "✅ YES — KILL THE BOT",
                type="primary",
                use_container_width=True,
                key="kill_confirm_yes",
            ):
                trigger_kill_switch()
                st.session_state.pop("confirm_kill", None)
                st.rerun()
        with c2:
            if st.button("❌ Cancel", use_container_width=True, key="kill_confirm_no"):
                st.session_state.pop("confirm_kill", None)
                st.rerun()


def _render_start_modal(status: dict) -> None:
    """
    2-step START confirmation modal.
    Step 1: show pre-flight results + current config.
    Step 2: CONFIRM button (enabled only when all checks pass).
    """
    checks   = st.session_state.get("preflight_results", [])
    all_ok   = bool(checks) and all(c["ok"] for c in checks)
    ks_active = status.get("kill_switch_active", False)

    with st.container(border=True):
        st.markdown("### ▶ Start Bot — Pre-flight")

        col_info, col_checks = st.columns([1, 1])

        with col_info:
            st.markdown(f"**Trading mode:** `{status.get('mode', 'unknown')}`")
            if ks_active:
                st.warning("⚠️ Kill switch is active — will be removed automatically.")
            else:
                st.success("✅ No kill switch present.")

        with col_checks:
            st.markdown("**Checks:**")
            for chk in checks:
                icon = "✅" if chk["ok"] else "❌"
                st.caption(f"{icon} **{chk['name']}** — {chk['detail']}")

        if not all_ok:
            st.error("One or more checks failed. Fix the issues above before starting.")

        c1, c2, c3 = st.columns([2, 1, 1])

        with c1:
            if st.button(
                "✅ CONFIRM — START BOT",
                type="primary",
                use_container_width=True,
                disabled=not all_ok,
                key="start_confirm_yes",
                help="Disabled until all pre-flight checks pass." if not all_ok else None,
            ):
                _do_start()

        with c2:
            if st.button("🔄 Re-check", use_container_width=True, key="start_recheck"):
                with st.spinner("Re-running checks…"):
                    st.session_state["preflight_results"] = preflight_check()
                st.rerun()

        with c3:
            if st.button("❌ Cancel", use_container_width=True, key="start_confirm_no"):
                st.session_state.pop("show_start_modal", None)
                st.session_state.pop("preflight_results", None)
                st.rerun()


def _do_start() -> None:
    """Execute the start sequence and update session state accordingly."""
    result = start_bot()

    st.session_state.pop("show_start_modal", None)
    st.session_state.pop("preflight_results", None)

    if result["ok"]:
        st.session_state["bot_starting_at"] = time.time()
        st.session_state["last_start_result"] = result
    else:
        # Persist error so it survives the rerun
        st.session_state["start_failed_error"] = result.get("error") or "Unknown error"
        st.session_state["start_failed_logs"]  = get_journalctl_tail(20)

    st.rerun()
