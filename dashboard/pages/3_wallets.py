"""Smart-wallet leaderboard and per-wallet track record."""
import os
import sys

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from common import BLUE, ORANGE, get_conn, pct, q, settings, short, sidebar, style, usd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from core import wallets as wreg  # noqa: E402

st.set_page_config(page_title="Smart Wallets · mayhambot", page_icon="🟢", layout="wide")
sidebar()
st.title("Smart Wallets")

with st.sidebar.form("add"):
    st.write("Wallet manuell hinzufügen")
    addr = st.text_input("Adresse")
    label = st.text_input("Label (optional)")
    if st.form_submit_button("Hinzufügen") and len(addr.strip()) >= 32:
        wreg.add_wallet(get_conn(), addr.strip(), label.strip())
        st.cache_data.clear()
        st.success("Hinzugefügt. Historie wird im nächsten Zyklus geladen.")

w = q("SELECT * FROM wallets WHERE tokens_traded > 0 OR manual=1")
smart = w[w.is_smart == 1].sort_values("score", ascending=False)
c = st.columns(4)
c[0].metric("Wallets mit Historie", len(w))
c[1].metric("Smart Wallets", len(smart))
c[2].metric("Als Bot markiert", int(w.is_bot.sum()))
c[3].metric("Manuell", int(w.manual.sum()))

a, b = st.columns(2)
if len(w):
    h = px.histogram(w[w.is_bot == 0], x="score", nbins=20, title="Verteilung Wallet-Score",
                     color_discrete_sequence=[BLUE], labels={"score": "Score"})
    h.add_vline(x=float(settings.smart_min_score), line_dash="dot",
                line_color="gray", annotation_text="Smart-Schwelle")
    a.plotly_chart(style(h).update_layout(hovermode="closest"), width="stretch")
if len(smart):
    sc = px.scatter(smart, x="win_rate", y="roi", size=smart.tokens_traded.clip(lower=1), hover_name=smart.address.map(short),
                    title="Smart Wallets: Trefferquote vs. ROI", color_discrete_sequence=[BLUE],
                    labels={"win_rate": "Trefferquote", "roi": "ROI gesamt"})
    sc.update_xaxes(tickformat=".0%").update_yaxes(tickformat=".0%")
    b.plotly_chart(style(sc).update_layout(hovermode="closest"), width="stretch")

st.subheader("Rangliste")
if smart.empty:
    st.caption("Noch keine Smart Wallets. Sie entstehen, sobald Wallets in ≥2 Coins mit Gewinn früh eingestiegen sind.")
else:
    st.dataframe(smart.assign(Wallet=smart.address, Label=smart.label, Score=smart.score.round(0),
                              Coins=smart.tokens_traded, Trefferquote=smart.win_rate.map(pct).str.lstrip("+"),
                              Früh=smart.early_rate.map(pct).str.lstrip("+"), ROI=smart.roi.map(pct),
                              PnL=smart.total_pnl_usd.map(usd), Realisiert=smart.realized_pnl_usd.map(usd),
                              Peak=smart.median_peak_multiple.round(1),
                              Letzter_Trade=pd.to_datetime(smart.last_trade_ts, unit="s"))
                 [["Wallet", "Label", "Score", "Coins", "Trefferquote", "Früh", "ROI", "PnL", "Realisiert", "Peak", "Letzter_Trade"]],
                 hide_index=True, width="stretch",
                 column_config={"Score": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%d"),
                                "Peak": st.column_config.NumberColumn(help="Median: höchster Preis nach Einstieg ÷ Einstiegspreis")})

st.subheader("Wallet-Detail")
opts = list(smart.address) + [a for a in w.sort_values("score", ascending=False).address if a not in set(smart.address)]
if opts:
    sel = st.selectbox("Wallet", opts, format_func=lambda a: f"{short(a)} · Score {w.set_index('address').score.get(a, 0):.0f}")
    st.code(sel, language=None)
    st.markdown(f"[Solscan](https://solscan.io/account/{sel}) · [GMGN](https://gmgn.ai/sol/address/{sel})")
    pos = q("""SELECT p.*, t.symbol FROM wallet_positions p LEFT JOIN tokens t ON t.mint=p.mint
               WHERE p.wallet=? ORDER BY p.first_buy_ts""", (sel,))
    if len(pos):
        pos["pnl"] = pos.realized_pnl_usd + pos.unrealized_pnl_usd
        pos["Zeit"] = pd.to_datetime(pos.first_buy_ts, unit="s")
        pos["kum"] = pos.pnl.cumsum()
        a, b = st.columns(2)
        f = go.Figure(go.Bar(x=pos.symbol.fillna(pos.mint.str[:6]), y=pos.pnl,
                             marker_color=[BLUE if v >= 0 else ORANGE for v in pos.pnl], hovertemplate="$%{y:,.0f}<extra></extra>"))
        a.plotly_chart(style(f.update_layout(title="PnL je Coin (USD)")), width="stretch")
        k = go.Figure(go.Scatter(x=pos.Zeit, y=pos.kum, mode="lines+markers", line=dict(color=BLUE, width=2),
                                 marker=dict(size=8), hovertemplate="$%{y:,.0f}<extra></extra>"))
        b.plotly_chart(style(k.update_layout(title="Kumulierter PnL (nach Einstieg)")), width="stretch")
        st.dataframe(pos.assign(Coin=pos.symbol, Einstieg=pos.Zeit, Gekauft=pos.buy_usd.map(usd), Verkauft=pos.sell_usd.map(usd),
                                PnL=pos.pnl.map(usd), ROI=pos.roi.map(pct), Peak=pos.peak_multiple.round(1), Trades=pos.trades, Mint=pos.mint)
                     [["Coin", "Einstieg", "Gekauft", "Verkauft", "PnL", "ROI", "Peak", "Trades", "Mint"]],
                     hide_index=True, width="stretch")
