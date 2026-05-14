from __future__ import annotations

"""Activity panel — watchlist, open positions, recent trades."""

import streamlit as st
from src.dashboard.data import get_watchlist, get_open_positions, get_recent_trades
from src.dashboard.utils import fmt_currency, fmt_r, time_ago, pnl_color


def render() -> None:
    tab_watch, tab_pos, tab_trades = st.tabs(
        ["👀 Watchlist", "📂 Open positions", "📋 Recent trades"]
    )

    # ── Watchlist ─────────────────────────────────────────────────────────
    with tab_watch:
        wl = get_watchlist()
        if not wl:
            st.info("No qualified candidates today yet. Scanner runs until 10:00 ET.")
        else:
            for row in wl:
                tier = row.get("news_tier") or "?"
                color = {"A": "🟢", "B": "🟡", "C": "🔴"}.get(tier, "⚪")
                st.markdown(
                    f"{color} **{row['ticker']}** — "
                    f"${row['price']:.2f} | "
                    f"gap {row.get('gap_pct', 0):.1f}% | "
                    f"rvol {row.get('rel_vol', 0):.1f}x | "
                    f"float {row.get('float_shares', 0) / 1e6:.1f}M | "
                    f"score {row.get('quality_score', 0):.0f} | "
                    f"news {color} Tier {tier}"
                )

    # ── Open positions ────────────────────────────────────────────────────
    with tab_pos:
        positions = get_open_positions()
        if not positions:
            st.info("No open positions.")
        else:
            for pos in positions:
                entry = pos.get("entry_price", 0)
                stop  = pos.get("stop_price", 0)
                target = pos.get("target_price", 0)
                shares = pos.get("shares", 0)
                risk_per_share = abs(entry - stop)
                c1, c2, c3, c4 = st.columns([2, 1, 2, 2])
                with c1:
                    st.markdown(f"**{pos['ticker']}** — {pos.get('setup_type', '?')}")
                    st.caption(f"Opened {time_ago(pos.get('opened_at', ''))}")
                with c2:
                    st.metric("Qty", shares)
                with c3:
                    st.metric("Entry", fmt_currency(entry))
                with c4:
                    st.metric(
                        "Stop / Target",
                        f"{fmt_currency(stop)} / {fmt_currency(target)}"
                    )
                st.divider()

    # ── Recent trades ─────────────────────────────────────────────────────
    with tab_trades:
        filter_opt = st.radio(
            "Filter",
            ["All", "Winners", "Losers", "Big wins (>2R)", "Big losses (<-1R)"],
            horizontal=True, label_visibility="collapsed",
        )
        trades = get_recent_trades(50)

        if filter_opt == "Winners":
            trades = [t for t in trades if (t.get("pnl_r") or 0) >= 0]
        elif filter_opt == "Losers":
            trades = [t for t in trades if (t.get("pnl_r") or 0) < 0]
        elif filter_opt == "Big wins (>2R)":
            trades = [t for t in trades if (t.get("pnl_r") or 0) >= 2.0]
        elif filter_opt == "Big losses (<-1R)":
            trades = [t for t in trades if (t.get("pnl_r") or 0) <= -1.0]

        if not trades:
            st.info("No trades match this filter.")
        else:
            for t in trades:
                pnl = t.get("pnl_dollars") or 0
                r   = t.get("pnl_r") or 0
                col = pnl_color(pnl)
                c1, c2, c3, c4 = st.columns([2, 2, 2, 2])
                with c1:
                    st.markdown(f"**{t['ticker']}** — {t.get('setup_type', '?')}")
                    st.caption(time_ago(t.get("closed_at", "")))
                with c2:
                    st.metric("Entry→Exit",
                              f"${t.get('entry_price', 0):.2f} → ${t.get('exit_price', 0) or 0:.2f}")
                with c3:
                    st.metric("P&L", fmt_currency(pnl, sign=True))
                with c4:
                    st.metric("R-multiple", fmt_r(r))
                st.caption(f"Exit reason: {t.get('exit_reason', '?')}")
                st.divider()
