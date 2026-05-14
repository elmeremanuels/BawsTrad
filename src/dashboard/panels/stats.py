from __future__ import annotations

"""Stats panel — KPI cards, equity curve, P&L bars, setup breakdown."""

import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from src.dashboard.data import (
    get_today_stats, get_period_stats,
    get_equity_curve, get_recent_trades, get_setup_breakdown,
)
from src.dashboard.utils import fmt_currency, fmt_pct, fmt_r


def _kpi_column(label: str, stats: dict) -> None:
    st.metric(label=f"**{label}**", value=" ")
    c1, c2, c3 = st.columns(3)
    with c1:
        st.metric("Trades", stats["trades"])
    with c2:
        wr = stats["win_rate"]
        st.metric("Win rate", fmt_pct(wr), delta=None)
    with c3:
        pnl = stats["gross_pnl"]
        st.metric("Net P&L", fmt_currency(pnl, sign=True),
                  delta=fmt_r(stats["avg_r"]))


def render() -> None:
    today   = get_today_stats()
    week    = get_period_stats(7)
    month   = get_period_stats(30)
    alltime = get_period_stats(365)

    # ── KPI cards ──────────────────────────────────────────────────────────
    cols = st.columns(4)
    for col, label, stats in zip(
        cols,
        ["Today", "This week", "This month", "All-time"],
        [today, week, month, alltime],
    ):
        with col:
            wins = stats["wins"]
            losses = stats["losses"]
            wr = stats["win_rate"]
            pnl = stats["gross_pnl"]
            ar = stats["avg_r"]
            st.markdown(f"**{label}**")
            st.metric("Trades", stats["trades"])
            st.metric("Win rate", fmt_pct(wr))
            st.metric("Net P&L", fmt_currency(pnl, sign=True), delta=fmt_r(ar))

    st.divider()

    # ── Equity curve ──────────────────────────────────────────────────────
    df = get_equity_curve(30)
    if not df.empty:
        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=df["day"], y=df["cumulative_pnl"],
            mode="lines+markers",
            line=dict(color="#22c55e", width=2),
            fill="tozeroy",
            fillcolor="rgba(34,197,94,0.1)",
            hovertemplate="<b>%{x}</b><br>Cumulative P&L: $%{y:,.2f}<extra></extra>",
        ))
        fig.update_layout(
            title="30-day Equity Curve",
            xaxis_title=None, yaxis_title="Cumulative P&L ($)",
            height=280, margin=dict(l=0, r=0, t=30, b=0),
            plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
        )
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("No closed trades yet — equity curve will appear after first trade.")

    # ── Daily P&L bars ────────────────────────────────────────────────────
    if not df.empty:
        colors = ["#22c55e" if v >= 0 else "#ef4444" for v in df["daily_pnl"]]
        fig2 = go.Figure(go.Bar(
            x=df["day"], y=df["daily_pnl"],
            marker_color=colors,
            hovertemplate="<b>%{x}</b><br>Daily P&L: $%{y:,.2f}<extra></extra>",
        ))
        fig2.update_layout(
            title="Daily P&L",
            xaxis_title=None, yaxis_title="P&L ($)",
            height=200, margin=dict(l=0, r=0, t=30, b=0),
            plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
        )
        st.plotly_chart(fig2, use_container_width=True)

    # ── Rolling win rate ──────────────────────────────────────────────────
    trades = get_recent_trades(50)
    if len(trades) >= 5:
        window = 20
        results = [1 if (t["pnl_r"] or 0) >= 0 else 0 for t in reversed(trades)]
        rolling = [
            sum(results[max(0, i - window):i]) / min(i, window) * 100
            for i in range(1, len(results) + 1)
        ]
        fig3 = go.Figure(go.Scatter(
            y=rolling, mode="lines",
            line=dict(color="#6366f1", width=2),
            hovertemplate="Trade %{x}: %{y:.1f}% win rate<extra></extra>",
        ))
        fig3.add_hline(y=50, line_dash="dash", line_color="gray", opacity=0.5)
        fig3.update_layout(
            title=f"Rolling win rate (last {window} trades)",
            xaxis_title="Trade #", yaxis_title="Win rate (%)",
            height=200, margin=dict(l=0, r=0, t=30, b=0),
            plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
        )
        st.plotly_chart(fig3, use_container_width=True)

    # ── Setup breakdown ───────────────────────────────────────────────────
    setups = get_setup_breakdown()
    if setups:
        st.markdown("**Setup breakdown**")
        c1, c2 = st.columns([1, 1])
        with c1:
            fig4 = go.Figure(go.Pie(
                labels=[s["setup_type"] for s in setups],
                values=[s["count"] for s in setups],
                hole=0.4,
            ))
            fig4.update_layout(height=220, margin=dict(l=0, r=0, t=0, b=0))
            st.plotly_chart(fig4, use_container_width=True)
        with c2:
            for s in setups:
                wr = s.get("win_rate", 0)
                ar = s.get("avg_r", 0)
                insight = "✅" if wr >= 60 and ar >= 1.5 else "⚠️" if wr < 40 else "➡️"
                st.caption(
                    f"{insight} **{s['setup_type']}**: {s['count']} trades, "
                    f"{wr:.0f}% win rate, {fmt_r(ar)} avg"
                )
