"""Shared dashboard helpers: DB access, formatting, chart styling."""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from core import db  # noqa: E402
from core.config import settings  # noqa: E402

# reference palette (dataviz skill), roles not ranks
BLUE, ORANGE, AQUA, VIOLET, GRAY = "#2a78d6", "#eb6834", "#1baf7a", "#6250d6", "#8a8984"


@st.cache_resource
def get_conn():
    return db.connect()


@st.cache_data(ttl=30)
def q(sql, params=()):
    return pd.read_sql_query(sql, get_conn(), params=params)


def now():
    return int(time.time())


def usd(x):
    if x is None or pd.isna(x):
        return "–"
    a = abs(x)
    s = f"{a/1e6:.2f}M" if a >= 1e6 else f"{a/1e3:.1f}k" if a >= 1e3 else f"{a:.0f}"
    return ("-$" if x < 0 else "$") + s


def pct(x):
    return "–" if x is None or pd.isna(x) else f"{x*100:+.0f}%"


def short(addr):
    return f"{addr[:4]}…{addr[-4:]}" if isinstance(addr, str) and len(addr) > 10 else addr


def style(fig, height=280):
    fig.update_layout(height=height, margin=dict(l=8, r=8, t=36, b=8), hovermode="x unified",
                      legend=dict(orientation="h", y=1.12, x=0), bargap=0.15)
    fig.update_xaxes(showgrid=False)
    fig.update_yaxes(gridcolor="rgba(128,128,128,0.15)", zeroline=False)
    return fig


def sidebar():
    with st.sidebar:
        if st.button("🔄 Daten neu laden"):
            st.cache_data.clear()
        st.caption(f"Fenster {settings.signal_window_min} min · Signal ab Score {settings.signal_min_score:.0f}, "
                   f"≥{settings.signal_min_wallets} Wallets, ≥${settings.signal_min_net_usd:,.0f} netto")
        st.caption(f"DB: {settings.db_path}")
