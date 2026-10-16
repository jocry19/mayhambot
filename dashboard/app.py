"""mayhambot dashboard — start with:  streamlit run dashboard/app.py   (from the mayhambot folder)"""
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from common import BLUE, ORANGE, now, pct, q, settings, sidebar, style, usd

st.set_page_config(page_title="mayhambot", page_icon="🟢", layout="wide")
sidebar()
st.title("mayhambot · Smart-Money-Übersicht")

t = now()
tokens = q("SELECT COUNT(*) n FROM tokens WHERE tracked=1")["n"][0]
smart = q("SELECT COUNT(*) n FROM wallets WHERE is_smart=1")["n"][0]
sig = q("SELECT * FROM signals WHERE ts > ? ORDER BY ts DESC", (t - 7 * 86400,))
sig24 = sig[sig.ts > t - 86400]
done = sig.dropna(subset=["max_ret_1h"])
hit = (done.max_ret_1h >= 0.2).mean() if len(done) else None
last = q("SELECT MAX(ts) ts FROM signal_metrics")["ts"][0]

c = st.columns(5)
c[0].metric("Beobachtete Coins", int(tokens))
c[1].metric("Smart Wallets", int(smart))
c[2].metric("Signale 24h", len(sig24))
c[3].metric("Treffer (≥+20% in 1h)", "–" if hit is None else f"{hit*100:.0f}%", help=f"aus {len(done)} ausgewerteten Signalen (7 Tage)")
c[4].metric("Letzter Scan", "–" if last is None else f"vor {max(0, (t - int(last)) // 60)} min")

if last is None:
    st.info("Noch keine Daten. Starte den Sammler:  `python -m signals.run`")
    st.stop()

st.subheader("Beste Coins gerade (letzter Scan)")
cur = q("""SELECT m.*, t.symbol, t.mcap_usd AS cur_mcap, t.liquidity_usd, t.price_change_h1
           FROM signal_metrics m JOIN tokens t ON t.mint=m.mint
           WHERE m.ts = (SELECT MAX(ts) FROM signal_metrics) ORDER BY m.score DESC""")
view = cur[cur.score > 0].copy()
if view.empty:
    st.caption("Im aktuellen Fenster kauft gerade kein Smart Wallet netto.")
else:
    st.dataframe(
        view.assign(Coin=view.symbol, Score=view.score.round(0), Netto=view.net_usd.map(usd),
                    Käufe=view.buy_usd.map(usd), Wallets=view.smart_buyers, Qualität=view.avg_quality.round(0),
                    MCap=view.cur_mcap.map(usd), Liquidität=view.liquidity_usd.map(usd),
                    **{"1h %": view.price_change_h1.round(0)}, Mint=view.mint)
        [["Coin", "Score", "Netto", "Käufe", "Wallets", "Qualität", "MCap", "Liquidität", "1h %", "Mint"]],
        hide_index=True, width="stretch",
        column_config={"Score": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%d")})

st.subheader("Signale")
if sig.empty:
    st.caption("Noch keine Signale ausgelöst. Schwellen in .env (SIGNAL_MIN_SCORE usw.).")
else:
    a, b = st.columns(2)
    hourly = sig.assign(h=(sig.ts // 3600) * 3600)
    hourly["Zeit"] = pd.to_datetime(hourly.h, unit="s")
    cnt = hourly.groupby("Zeit").size().reset_index(name="Signale")
    a.plotly_chart(style(px.bar(cnt, x="Zeit", y="Signale", title="Signale pro Stunde",
                                color_discrete_sequence=[BLUE])), width="stretch")
    if len(done):
        f = px.scatter(done, x="score", y=done.max_ret_1h * 100, hover_name="symbol",
                       labels={"score": "Signal-Score", "y": "max. Rendite 1h (%)"},
                       title="Trifft der Score? Score vs. max. Rendite in 1h", color_discrete_sequence=[BLUE])
        f.add_hline(y=0, line_color="gray", line_width=1)
        f.update_traces(marker=dict(size=10, line=dict(width=2, color="white")))
        b.plotly_chart(style(f).update_layout(hovermode="closest"), width="stretch")
    else:
        b.caption("Ergebnisse (Rendite nach 5 min / 15 min / 1 h) erscheinen, sobald Signale alt genug sind.")
    tab = sig.copy()
    tab["Zeit"] = pd.to_datetime(tab.ts, unit="s")
    st.dataframe(tab.assign(Coin=tab.symbol, Score=tab.score.round(0), Netto=tab.net_usd.map(usd),
                            Wallets=tab.smart_buyers, Qualität=tab.avg_quality.round(0), MCap=tab.mcap_usd.map(usd),
                            **{"5m": tab.ret_5m.map(pct), "15m": tab.ret_15m.map(pct), "1h": tab.ret_1h.map(pct),
                               "max 1h": tab.max_ret_1h.map(pct)})
                 [["Zeit", "Coin", "Score", "Netto", "Wallets", "Qualität", "MCap", "5m", "15m", "1h", "max 1h"]],
                 hide_index=True, width="stretch")

st.subheader("Netto-Smart-Money-Fluss aller Coins (24h)")
flow = q("""SELECT ts, SUM(net_usd) net, SUM(smart_buyers) buyers FROM signal_metrics
            WHERE ts > ? GROUP BY ts ORDER BY ts""", (t - 86400,))
if len(flow):
    flow["Zeit"] = pd.to_datetime(flow.ts, unit="s")
    fig = go.Figure(go.Bar(x=flow.Zeit, y=flow.net, marker_color=[BLUE if v >= 0 else ORANGE for v in flow.net],
                           name="Netto USD", hovertemplate="%{y:$,.0f}<extra></extra>"))
    st.plotly_chart(style(fig.update_layout(title=f"Summe Netto-Inflow im {settings.signal_window_min}-min-Fenster je Scan")),
                    width="stretch")
