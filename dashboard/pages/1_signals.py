"""Per-coin view: best signals, smart-money inflow, wallet count and score over time."""
import json

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from common import AQUA, BLUE, ORANGE, VIOLET, now, pct, q, settings, short, sidebar, style, usd

st.set_page_config(page_title="Coin-Signale · mayhambot", page_icon="🟢", layout="wide")
sidebar()
st.title("Coin-Signale")

t = now()
hours = st.sidebar.select_slider("Zeitraum", options=[1, 3, 6, 12, 24, 48, 168], value=24, format_func=lambda h: f"{h} h")
since = t - hours * 3600

coins = q("""SELECT t.mint, t.symbol, t.mcap_usd, MAX(m.score) best, MAX(s.score) best_signal, COUNT(DISTINCT s.id) n_sig
             FROM tokens t JOIN signal_metrics m ON m.mint=t.mint AND m.ts > ?
             LEFT JOIN signals s ON s.mint=t.mint AND s.ts > ?
             GROUP BY t.mint ORDER BY COALESCE(best_signal, 0) DESC, best DESC""", (since, since))
if coins.empty:
    st.info("Noch keine Messwerte. Starte den Sammler:  `python -m signals.run`")
    st.stop()

labels = {r.mint: f"{r.symbol or short(r.mint)} · bester Score {r.best:.0f} · {r.n_sig} Signale" for r in coins.itertuples()}
pre = st.query_params.get("mint")
mint = st.selectbox("Coin (nach bestem Signal sortiert)", list(labels), format_func=labels.get,
                    index=list(labels).index(pre) if pre in labels else 0)
st.query_params["mint"] = mint
tok = q("SELECT * FROM tokens WHERE mint=?", (mint,)).iloc[0]

c = st.columns(5)
c[0].metric("Preis", f"${tok.price_usd:.8g}" if tok.price_usd else "–", f"{tok.price_change_h1:+.0f}% 1h" if tok.price_change_h1 else None)
c[1].metric("Market Cap", usd(tok.mcap_usd))
c[2].metric("Liquidität", usd(tok.liquidity_usd))
c[3].metric("DEX", tok.dex or "–")
c[4].markdown(f"[DexScreener](https://dexscreener.com/solana/{mint}) · [Solscan](https://solscan.io/token/{mint})")
st.code(mint, language=None)

m = q("SELECT * FROM signal_metrics WHERE mint=? AND ts>? ORDER BY ts", (mint, since))
m["Zeit"] = pd.to_datetime(m.ts, unit="s")
sig = q("SELECT * FROM signals WHERE mint=? AND ts>? ORDER BY ts", (mint, since))
sig["Zeit"] = pd.to_datetime(sig.ts, unit="s")
px_ = q("""SELECT ts, price_usd FROM token_prices WHERE mint=? AND ts>? AND price_usd>0
           UNION ALL SELECT ts, price_usd FROM trades WHERE mint=? AND ts>? AND price_usd>0 ORDER BY ts""",
        (mint, since, mint, since))
px_["Zeit"] = pd.to_datetime(px_.ts, unit="s")

# price with signal markers
f = go.Figure(go.Scatter(x=px_.Zeit, y=px_.price_usd, mode="lines", line=dict(color=BLUE, width=2), name="Preis",
                         hovertemplate="$%{y:.8g}<extra>Preis</extra>"))
if len(sig):
    f.add_trace(go.Scatter(x=sig.Zeit, y=sig.price_usd, mode="markers", name="Signal",
                           marker=dict(size=13, color=AQUA, symbol="triangle-up", line=dict(width=2, color="white")),
                           customdata=sig[["score", "smart_buyers", "net_usd"]],
                           hovertemplate="Signal · Score %{customdata[0]:.0f} · %{customdata[1]} Wallets · $%{customdata[2]:,.0f}<extra></extra>"))
st.plotly_chart(style(f.update_layout(title="Preis (USD) mit Signalen ▲"), 300), width="stretch")

a, b = st.columns(2)
fl = go.Figure()
fl.add_trace(go.Bar(x=m.Zeit, y=m.buy_usd, name="Smart-Käufe", marker_color=BLUE, hovertemplate="$%{y:,.0f}"))
fl.add_trace(go.Bar(x=m.Zeit, y=-m.sell_usd, name="Smart-Verkäufe", marker_color=ORANGE, hovertemplate="$%{y:,.0f}"))
fl.update_layout(barmode="relative", title=f"Smart-Money-Fluss (USD, {settings.signal_window_min}-min-Fenster)")
a.plotly_chart(style(fl), width="stretch")
w = go.Figure(go.Scatter(x=m.Zeit, y=m.smart_buyers, mode="lines", line=dict(color=VIOLET, width=2, shape="hv"),
                         name="Smart Wallets", hovertemplate="%{y} Wallets<extra></extra>"))
b.plotly_chart(style(w.update_layout(title="Anzahl kaufender Smart Wallets")), width="stretch")

a, b = st.columns(2)
s = go.Figure(go.Scatter(x=m.Zeit, y=m.score, mode="lines", line=dict(color=BLUE, width=2), name="Score",
                         hovertemplate="Score %{y:.0f}<extra></extra>"))
s.add_hline(y=settings.signal_min_score, line_dash="dot", line_color="gray",
            annotation_text=f"Signal-Schwelle {settings.signal_min_score:.0f}", annotation_position="top left")
b_ = s.update_layout(title="Signal-Score", yaxis_range=[0, 100])
a.plotly_chart(style(b_), width="stretch")
qq = go.Figure(go.Scatter(x=m.Zeit, y=m.avg_quality, mode="lines", line=dict(color=AQUA, width=2), name="Qualität",
                          hovertemplate="Qualität %{y:.0f}<extra></extra>"))
b.plotly_chart(style(qq.update_layout(title="Ø Qualität der kaufenden Wallets", yaxis_range=[0, 100])),
               width="stretch")

st.subheader("Signale dieses Coins")
if sig.empty:
    st.caption("Kein Signal im Zeitraum.")
else:
    for r in sig.sort_values("ts", ascending=False).itertuples():
        with st.expander(f"{r.Zeit:%d.%m %H:%M} UTC · MCap {usd(r.mcap_usd)} · Score {r.score:.0f} · {usd(r.net_usd)} netto · "
                         f"{r.smart_buyers} Wallets · 15m {pct(r.ret_15m)} · 1h {pct(r.ret_1h)} · max 1h {pct(r.max_ret_1h)}",
                         expanded=r.Index == sig.index[-1]):
            k = st.columns(6)
            k[0].metric("MCap beim Signal", usd(r.mcap_usd))
            k[1].metric("Inflow netto", f"{(r.net_sol or 0):.1f} SOL", usd(r.net_usd), delta_color="off")
            k[2].metric("Ultra / Smart / Good", f"{r.n_ultra or 0} / {r.n_smart or 0} / {r.n_good or 0}")
            k[3].metric("Vorhersage", "–" if pd.isna(r.prediction) else f"{r.prediction*100:.0f}%", help=r.prediction_label or None)
            k[4].metric("max MCap 1h", usd(r.max_mcap_1h))
            k[5].metric("max 24h", pct(r.max_ret_24h))
            if r.contributions_json:
                c = pd.DataFrame(json.loads(r.contributions_json)).T.reset_index(names="Eingang")
                fc = go.Figure(go.Bar(y=c.Eingang, x=c.points, orientation="h", marker_color=BLUE,
                                      text=c.why, textposition="auto", hovertemplate="%{x:.1f} Punkte<extra></extra>"))
                st.plotly_chart(style(fc.update_layout(title=f"Wie der Score {r.score:.0f} entsteht (Punkte je Eingang)",
                                                       hovermode="closest"), 220), width="stretch")
            ws = pd.DataFrame(json.loads(r.wallets_json or "[]"))
            if len(ws):
                ws["Wallet"] = ws.wallet
                st.dataframe(ws.assign(Stufe=ws.get("tier", ""), Score=ws.score.round(0), Gekauft=ws.buy_usd.map(usd), SOL=ws.buy_sol.round(2),
                                       Verkauft=ws.sell_usd.map(usd), Coins=ws.n_tokens,
                                       Einstieg=pd.to_datetime(ws.first_ts, unit="s"))
                             [["Wallet", "Stufe", "Score", "Gekauft", "SOL", "Verkauft", "Coins", "Einstieg"]],
                             hide_index=True, width="stretch")

st.subheader("Wer handelt diesen Coin? (Smart Wallets, Zeitraum)")
tr = q("""SELECT t.wallet, w.score, w.label, w.manual,
                 SUM(CASE WHEN side='buy' THEN usd_amount ELSE 0 END) buy,
                 SUM(CASE WHEN side='sell' THEN usd_amount ELSE 0 END) sell, MIN(t.ts) first, COUNT(*) n
          FROM trades t JOIN wallets w ON w.address=t.wallet
          WHERE t.mint=? AND t.ts>? AND w.is_smart=1 GROUP BY t.wallet ORDER BY buy DESC""", (mint, since))
if tr.empty:
    st.caption("Keine Smart Wallets in diesem Coin.")
else:
    st.dataframe(tr.assign(Wallet=tr.wallet, Label=tr.label, Score=tr.score.round(0), Gekauft=tr.buy.map(usd),
                           Verkauft=tr.sell.map(usd), Netto=(tr.buy - tr.sell).map(usd),
                           Erster_Trade=pd.to_datetime(tr["first"], unit="s"), Trades=tr.n)
                 [["Wallet", "Label", "Score", "Gekauft", "Verkauft", "Netto", "Erster_Trade", "Trades"]],
                 hide_index=True, width="stretch")
