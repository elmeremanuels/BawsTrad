from __future__ import annotations

"""Insights panel — what's next, divergence alert, empty-day indicator, manual override journal."""

from datetime import datetime
from zoneinfo import ZoneInfo

import streamlit as st

from src.dashboard.data import (
    get_today_stats, get_period_stats,
    get_recent_events, get_dashboard_actions
)
from src.dashboard.utils import time_ago

ET = ZoneInfo("America/New_York")


def render() -> None:
    st.subheader("🧠 Insights")

    # ── 1. What's next ────────────────────────────────────────────────────
    try:
        from src.engine.state import current_mode, next_mode_change, minutes_until_next_change
        mode = current_mode()
        next_mode, next_transition = next_mode_change()
        mins = minutes_until_next_change()

        now_et = datetime.now(ET)
        hrs, rem_mins = divmod(mins, 60)
        transition_str = f"{hrs}h {rem_mins}m" if hrs else f"{rem_mins}m"

        st.info(
            f"**Current:** {mode.value.replace('_', ' ').title()}\n\n"
            f"**Next:** {next_mode.value.replace('_', ' ').title()} in **{transition_str}** "
            f"(at {next_transition.strftime('%H:%M')} ET)",
            icon="⏰"
        )
    except Exception:
        st.caption("Mode timing unavailable.")

    # ── 2. Empty-day indicator ────────────────────────────────────────────
    today_stats = get_today_stats()
    if today_stats["trades"] == 0:
        # Check how many events fired today (scanner activity)
        events = get_recent_events(50)
        from datetime import date
        today_events = [
            e for e in events
            if e.get("occurred_at", "")[:10] == date.today().isoformat()
        ]
        scan_events = [e for e in today_events if "scanner" in e.get("event_type", "").lower()
                       or "watchlist" in (e.get("message") or "").lower()]

        st.warning(
            f"**0 trades today** — this is expected behavior, not a bug.\n\n"
            f"The scanner has run {len(scan_events)} cycles. "
            f"No tickers met all entry criteria (≥10% gap, Tier A/B news, rel_vol ≥5x, float <20M, "
            f"plus pattern + ACTIVE_TRADING mode). "
            f"A quiet day with no setups is correct discipline.",
            icon="📊"
        )

    # ── 3. Backtest vs live divergence ────────────────────────────────────
    live_30 = get_period_stats(30)
    if live_30["trades"] >= 10:
        live_wr = live_30["win_rate"]
        live_avg_r = live_30["avg_r"]
        # Backtest targets (from Phase 1 DoD check)
        target_wr = 55.0
        target_avg_r = 1.2

        wr_div   = abs(live_wr - target_wr) / max(target_wr, 1) * 100
        r_div    = abs(live_avg_r - target_avg_r) / max(abs(target_avg_r), 0.1) * 100

        if wr_div > 25 or r_div > 25:
            st.error(
                f"🚨 **Divergence alert** — Live metrics are >25% off backtest expectations.\n\n"
                f"Live win rate: **{live_wr:.1f}%** (target: {target_wr:.0f}%) | "
                f"Live avg R: **{live_avg_r:.2f}** (target: {target_avg_r:.1f})\n\n"
                f"Consider reviewing setup criteria or pausing until investigated.",
                icon="🚨"
            )
        else:
            st.success(
                f"Live win rate: **{live_wr:.1f}%** (target: {target_wr:.0f}%) ✓  |  "
                f"Live avg R: **{live_avg_r:.2f}** (target: {target_avg_r:.1f}) ✓",
                icon="✅"
            )

    # ── 4. Manual override journal ────────────────────────────────────────
    actions = get_dashboard_actions(20)
    if actions:
        with st.expander(f"📓 Manual override journal ({len(actions)} actions)"):
            for a in actions:
                ts   = time_ago(a.get("timestamp", ""))
                act  = a.get("action", "?")
                det  = a.get("details", {})
                icons = {
                    "pause": "⏸️", "resume": "▶️",
                    "kill_switch": "🛑", "kill_switch_cleared": "✅",
                    "mode_switch": "🔄", "learning_accepted": "✅",
                    "learning_rejected": "❌",
                }
                icon = icons.get(act, "📌")
                detail_str = ", ".join(f"{k}={v}" for k, v in det.items()) if det else ""
                st.caption(f"{icon} **{act}** · {ts}{' — ' + detail_str if detail_str else ''}")
    else:
        st.caption("No manual overrides yet.")
