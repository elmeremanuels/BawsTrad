from __future__ import annotations

"""Messages panel — Discord-like chronological event stream."""

import streamlit as st
from src.dashboard.data import get_recent_events, get_recent_trades, get_learnings
from src.dashboard.utils import time_ago


_TYPE_ICONS = {
    "entry":         "📥",
    "force_close":   "🔒",
    "emergency_close": "🚨",
    "startup":       "🚀",
    "boot_test":     "🧪",
    "mode_transition": "🔄",
    "learning_extracted": "💡",
    "briefing_fetched": "🌅",
    "news_ingested": "📰",
    "error":         "❌",
    "warning":       "⚠️",
}


def _build_stream(filter_type: str) -> list[dict]:
    """Merge bot_events + recent trades into a single stream."""
    stream = []

    # Bot events
    events = get_recent_events(80)
    for e in events:
        if filter_type not in ("All", "Errors") and e["event_type"] in ("error", "warning"):
            if filter_type == "Errors":
                pass
            else:
                continue
        if filter_type == "Trades" and e["event_type"] not in ("entry", "force_close"):
            continue
        if filter_type == "Errors" and e["event_type"] not in ("error", "warning"):
            continue
        stream.append({
            "ts": e.get("occurred_at", ""),
            "type": e["event_type"],
            "msg": e.get("message", ""),
            "source": "event",
        })

    # Learnings as insight messages
    if filter_type in ("All", "Insights"):
        for l in get_learnings()[:10]:
            stream.append({
                "ts": l.get("created_at", ""),
                "type": "learning_extracted",
                "msg": l.get("observation", "")[:120],
                "source": "learning",
            })

    # Sort descending by timestamp
    stream.sort(key=lambda x: x["ts"], reverse=True)
    return stream[:60]


def render() -> None:
    filter_opt = st.radio(
        "Filter",
        ["All", "Trades", "Errors", "Insights"],
        horizontal=True,
        label_visibility="collapsed",
    )

    stream = _build_stream(filter_opt)

    if not stream:
        st.info("No events yet. Stream will populate as the bot runs.")
        return

    for item in stream:
        icon = _TYPE_ICONS.get(item["type"], "📌")
        ts   = time_ago(item["ts"])
        msg  = item["msg"] or ""

        if len(msg) > 120:
            with st.expander(f"{icon} **{item['type']}** · {ts} — {msg[:80]}…"):
                st.text(msg)
        else:
            st.markdown(f"{icon} **{item['type']}** · `{ts}` — {msg}")
