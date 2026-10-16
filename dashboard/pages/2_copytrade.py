"""Trading: Modus 1 (Wal-Pumps kopieren) und Modus 2 (Smart-Money-Signale kaufen), Betriebsarten, Presets, Statistiken."""
import time

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from common import AQUA, BLUE, GRAY, ORANGE, VIOLET, pct, short, sidebar, style, usd

from copytrade import control, learn, stats, store
from copytrade import strategy as strategy_mod
from copytrade.config import load

st.set_page_config(page_title="Copy-Trading · mayhambot", page_icon="🐋", layout="wide")
sidebar()
s = load()


@st.cache_resource
def ct_conn():
    return store.connect(s)


conn = ct_conn()

st.title("🐋 Trading · Copy-Pumps und Smart-Money-Signale")

# ---------------------------------------------------------------- control panel
paused = control.paused(conn)
h = st.columns([3, 1])
h[0].caption(
    ("🔴 **Echtgeld-Hauptschalter AN** (`.env`). Pro Modus wählst du unten, ob er live oder auf Papier handelt."
     if s.live_ready else
     "🟢 **Papierhandel**: echte Marktdaten, simulierte Fills mit Jupiter-Quote, Slippage und Gebühren. "
     "Echtgeld erst nach `LIVE_TRADING=true` und Schlüssel in `.env`, dann pro Modus hier einschalten."))
if h[1].toggle("⏸ Handel pausieren", value=paused, help="Stoppt alle neuen Käufe; offene Positionen werden weiter verwaltet."):
    if not paused:
        control.set_paused(conn, True)
        st.rerun()
elif paused:
    control.set_paused(conn, False)
    st.rerun()

presets = control.presets(conn)
cols = st.columns(2)
for col, strat in zip(cols, control.STRATEGIES):
    with col.container(border=True):
        st.markdown(f"**{control.STRATEGY_LABELS[strat]}**")
        cur = control.mode(conn, strat)
        new_mode = st.radio("Betriebsart", control.MODES, index=control.MODES.index(cur), key=f"mode_{strat}",
                            format_func=control.MODE_LABELS.get, horizontal=False, label_visibility="collapsed")
        if new_mode != cur:
            control.set_mode(conn, strat, new_mode)
            st.toast(f"{control.STRATEGY_LABELS[strat]}: {control.MODE_LABELS[new_mode]}")
        r = st.columns(2)
        cur_p = control.get(conn, f"preset:{strat}")
        names = list(presets)
        new_p = r[0].selectbox("Preset", names, index=names.index(cur_p) if cur_p in names else 0, key=f"preset_{strat}")
        if new_p != cur_p:
            control.use_preset(conn, strat, new_p)
        live_now = control.live_wanted(conn, strat)
        want = r[1].toggle("Echtgeld", value=live_now and s.live_ready, disabled=not s.live_ready, key=f"live_{strat}",
                           help="Nur möglich, wenn LIVE_TRADING=true und ein Schlüssel in .env stehen.")
        if s.live_ready and want != live_now:
            control.set_live(conn, strat, want)
        pr = presets.get(new_p, control.Preset())
        st.caption(f"{pr.size_sol} SOL/Trade · max {pr.max_open} offen · Stop −{pr.stop_loss:.0%} · "
                   + (f"Ziel +{pr.take_profit:.0%}" if strat == "signal" or not pr.learned_tp else "Ziel pro Wal gelernt")
                   + f" · Tagesverlust max {pr.max_daily_loss_sol} SOL"
                   + (f" · Modell ≥ {pr.min_model_score:.0f} % Pump-Chance (vorher Signal-Score ≥ {pr.min_score:.0f}),"
                      f" ≥ {pr.min_smart_buyers} Smart Wallets" if strat == "signal" else ""))

with st.expander("✏️ Presets bearbeiten (Größe, Ziele, Stops, Limits)"):
    base = st.selectbox("Vorlage", list(presets), key="preset_edit_base")
    bp = presets[base]
    e = st.columns(4)
    vals = dict(
        size_sol=e[0].number_input("SOL pro Trade", 0.01, 100.0, float(bp.size_sol), 0.05),
        max_open=e[1].number_input("max. offene Positionen", 1, 50, int(bp.max_open)),
        max_daily_loss_sol=e[2].number_input("max. Tagesverlust (SOL)", 0.05, 1000.0, float(bp.max_daily_loss_sol), 0.1),
        slippage_bps=e[3].number_input("Slippage (bps)", 10, 3000, int(bp.slippage_bps), 50),
        take_profit=e[0].number_input("Kursziel (%)", 1.0, 2000.0, bp.take_profit * 100, 5.0) / 100,
        stop_loss=e[1].number_input("Stop-Loss (%)", 1.0, 95.0, bp.stop_loss * 100, 1.0) / 100,
        trail_activate=e[2].number_input("Trailing ab (%)", 1.0, 1000.0, bp.trail_activate * 100, 1.0) / 100,
        trail_drawdown=e[3].number_input("Trailing-Abstand (%)", 1.0, 90.0, bp.trail_drawdown * 100, 1.0) / 100,
        max_hold_min=e[0].number_input("max. Haltedauer (min)", 1.0, 10080.0, float(bp.max_hold_min), 5.0),
        min_score=e[1].number_input("min. Signal-Score (ohne Modell)", 0.0, 100.0, float(bp.min_score), 1.0),
        min_model_score=e[1].number_input("min. Modell-Score = Pump-Chance 1 h (%)", 0.0, 100.0,
                                          float(bp.min_model_score), 1.0),
        require_model=e[2].checkbox("Modus 2 nur mit trainiertem Modell", value=bp.require_model),
        min_smart_buyers=e[2].number_input("min. Smart Wallets", 1, 50, int(bp.min_smart_buyers)),
        max_signal_age_s=e[3].number_input("Signal max. alt (s)", 10.0, 3600.0, float(bp.max_signal_age_s), 10.0),
        min_liquidity_usd=e[0].number_input("min. Liquidität ($)", 0.0, 1e8, float(bp.min_liquidity_usd), 1000.0),
        max_mcap_usd=e[1].number_input("max. Market Cap ($)", 1e4, 1e11, float(bp.max_mcap_usd), 100000.0),
        learned_tp=e[2].checkbox("Modus 1: Ziel pro Wal lernen", value=bp.learned_tp),
    )
    name = e[3].text_input("Speichern als", value=base if base not in control.BUILTIN else f"{base}-eigen")
    if st.button("💾 Preset speichern"):
        control.save_preset(conn, name, control.Preset(**vals))
        st.success(f"Preset „{name}“ gespeichert. Oben einem Modus zuweisen.")
        st.rerun()

view_mode = st.sidebar.radio("Konto", ["paper", "live"], index=0, format_func=lambda m: "Papier" if m == "paper" else "Live")
view_strat = st.sidebar.radio("Statistik für", ["all", "whale", "signal", "manual"],
                              format_func=lambda x: {"all": "alle Modi", "whale": "Modus 1 · Wal-Pumps",
                                                     "signal": "Modus 2 · Signale", "manual": "manuell"}[x])
if st.sidebar.button("🔄 Copy-Daten neu laden"):
    st.cache_data.clear()


@st.cache_data(ttl=10)
def load_all(mode, strat):
    return (stats.positions(conn, mode, None if strat == "all" else strat), stats.equity(conn, mode),
            stats.wallet_table(conn), stats.outcomes(conn), stats.events(conn, 400), stats.by_strategy(conn, mode))


pos, eq, wallets, outc, ev, by_strat = load_all(view_mode, view_strat)
sm = stats.summary(pos)
mode = view_mode

# ---------------------------------------------------------------- KPIs
bal = None
if mode == "paper":
    v = store.get_state(conn, "paper_balance_sol")
    bal = float(v) if v is not None else s.paper_start_balance_sol
c = st.columns(6)
c[0].metric("Kontostand", f"{eq.equity_sol.iloc[-1]:.2f} SOL" if len(eq) else (f"{bal:.2f} SOL" if bal else "–"),
            help="Guthaben + Wert offener Positionen (alle Modi)")
c[1].metric("Gewinn/Verlust", f"{sm.get('pnl_sol', 0):+.3f} SOL", f"{sm['avg_pnl_pct']*100:+.1f}% Ø/Trade" if sm.get("trades") else None)
c[2].metric("Trefferquote", f"{sm['win_rate']*100:.0f}%" if sm.get("trades") else "–", help=f"{sm.get('trades', 0)} abgeschlossene Trades")
c[3].metric("Profit-Faktor", f"{sm['profit_factor']:.2f}" if sm.get("trades") else "–", help="Summe Gewinne / Summe Verluste")
c[4].metric("Offene Positionen", sm.get("open", 0))
c[5].metric("Max. Drawdown", f"{sm['max_drawdown_sol']:.3f} SOL" if sm.get("trades") else "–")

active = int(wallets.active.sum()) if len(wallets) else 0
ranked = int(wallets.samples.notna().sum()) if len(wallets) else 0
last_ev = ev.ts.max() if len(ev) else None
st.caption(f"{active} Wale beobachtet · {ranked} bewertet · {len(outc)} Wal-Käufe ausgewertet · "
           f"letzte Aktivität {'vor ' + str(int((time.time() - last_ev) // 60)) + ' min' if last_ev else '–'}")
if not active and not len(pos):
    st.info("Noch keine Daten. Der Copy-Trader startet mit der App (`python mayham.py start`) und findet Wale selbst.")

tab_open, tab_alerts, tab_perf, tab_learn, tab_wal, tab_timing, tab_trades, tab_log = st.tabs(
    ["📂 Offen", "🔔 Einstiege", "📈 Performance", "🧠 Lernen", "🐋 Wal-Ranking", "⏱ Timing", "📋 Trades", "🧾 Protokoll"])

# ---------------------------------------------------------------- open positions (sell buttons)
with tab_open:
    allpos = stats.positions(conn)
    op = allpos[allpos.status == "open"] if len(allpos) else allpos
    if op.empty:
        st.caption("Keine offenen Positionen.")
    for r in op.itertuples():
        with st.container(border=True):
            k = st.columns([2, 1, 1, 1, 1, 1.2])
            k[0].markdown(f"**{r.symbol}** · {control.STRATEGY_LABELS.get(r.strategy, r.strategy)}  \n"
                          f"`{short(r.mint)}` · {'LIVE' if r.mode == 'live' else 'Papier'} · "
                          f"{'du verkaufst' if r.managed == 'semi' else 'Bot verkauft'}")
            k[1].metric("Stand", pct(r.current_pct))
            k[2].metric("Einsatz", f"{r.entry_cost_sol:.3f} SOL")
            k[3].metric("Ziel / Stop", f"+{r.take_profit:.0%} / −{r.stop_loss:.0%}")
            k[4].metric("Seit", f"{r.hold_min:.0f} min")
            if r.exit_hint:
                k[5].warning(f"Verkaufen? {strategy_mod.EXIT_NAMES.get(r.exit_hint, r.exit_hint)}")
            if k[5].button("Verkaufen", key=f"sell_{r.id}", type="primary"):
                control.request_sell(conn, int(r.id))
                st.toast(f"Verkaufsauftrag für {r.symbol} an den Bot geschickt")
    pend = conn.execute("SELECT COUNT(*) FROM ct_commands WHERE status='pending'").fetchone()[0]
    if pend:
        st.caption(f"⏳ {pend} Auftrag/Aufträge warten auf den Bot (läuft er? `python mayham.py status`).")
    with st.expander("➕ Coin selbst kaufen (du verkaufst)"):
        m1, m2, m3 = st.columns([3, 1, 1])
        mint_in = m1.text_input("Token-Mint", key="buy_mint")
        size_in = m2.number_input("SOL", 0.01, 100.0, float(control.preset(conn, "signal").size_sol), 0.05, key="buy_size")
        if m3.button("Kaufen", disabled=not mint_in):
            control.request_buy(conn, mint_in.strip(), size_in)
            st.toast("Kaufauftrag an den Bot geschickt")

# ---------------------------------------------------------------- entries / alerts
with tab_alerts:
    al = stats.alerts(conn, since_ts=int(time.time()) - 7 * 86400)
    if al.empty:
        st.caption("Noch keine Einstiege. Im Modus „Manuell“ erscheinen hier die Kaufsignale, die du selbst ausführen kannst.")
    else:
        sig_only = al[al.action == "signal"].head(20)
        if len(sig_only):
            st.markdown("**Kaufsignale zum Selbst-Handeln** (Modus „Manuell“)")
            for r in sig_only.itertuples():
                k = st.columns([3, 1, 1, 1])
                k[0].markdown(f"**{r.symbol or short(r.mint)}** · {control.STRATEGY_LABELS.get(r.strategy, r.strategy)}  \n"
                              f"{r.reason}  \n`{r.mint}`")
                k[1].metric("Market Cap", usd(r.mcap_usd))
                k[2].metric("vor", f"{(time.time() - r.ts) / 60:.0f} min")
                if k[3].button("Kaufen", key=f"buy_alert_{r.id}"):
                    control.request_buy(conn, r.mint, None, r.strategy)
                    st.toast(f"Kaufauftrag für {r.symbol} an den Bot geschickt")
        st.markdown("**Alle Einstiege**")
        st.dataframe(al.assign(Zeit=al.dt, Modus=al.strategy.map(lambda x: control.STRATEGY_LABELS.get(x, x)),
                               Aktion=al.action.map({"bought": "gekauft", "signal": "nur Signal"}),
                               Coin=al.symbol, MC=al.mcap_usd.map(usd), Score=al.score.round(1), Grund=al.reason,
                               Mint=al.mint)[["Zeit", "Modus", "Aktion", "Coin", "MC", "Score", "Grund", "Mint"]],
                     hide_index=True, width="stretch")

# ---------------------------------------------------------------- performance
with tab_perf:
    a, b = st.columns(2)
    if len(eq):
        f = go.Figure(go.Scatter(x=eq.dt, y=eq.equity_sol, mode="lines", line=dict(color=BLUE, width=2),
                                 name="Kontostand", hovertemplate="%{y:.3f} SOL<extra></extra>"))
        a.plotly_chart(style(f.update_layout(title="Equity-Kurve (SOL)"), 300), width="stretch")
    else:
        a.caption("Die Equity-Kurve erscheint, sobald der Copy-Trader läuft.")
    closed = pos[pos.status == "closed"] if len(pos) else pos
    if len(closed):
        cl = closed.sort_values("closed_at")
        f = go.Figure(go.Bar(x=cl.closed_at_dt, y=cl.pnl_pct * 100,
                             marker_color=[BLUE if v > 0 else ORANGE for v in cl.pnl_pct],
                             customdata=cl[["symbol", "exit_reason", "pnl_sol"]],
                             hovertemplate="%{customdata[0]} · %{y:+.1f}% · %{customdata[2]:+.3f} SOL · %{customdata[1]}<extra></extra>"))
        b.plotly_chart(style(f.update_layout(title="Ergebnis je Trade (%) · blau Gewinn, orange Verlust"), 300),
                       width="stretch")
        d, e = st.columns(2)
        by_reason = cl.groupby("exit_reason").agg(Trades=("id", "count"), Rendite=("pnl_pct", "mean")).reset_index()
        names = strategy_mod.EXIT_NAMES
        by_reason["Ausstieg"] = by_reason.exit_reason.map(lambda r: names.get(r, r))
        f = go.Figure(go.Bar(y=by_reason.Ausstieg, x=by_reason.Rendite * 100, orientation="h",
                             marker_color=[BLUE if v > 0 else ORANGE for v in by_reason.Rendite],
                             text=by_reason.Trades.map(lambda n: f"{n} Trades"), textposition="auto",
                             hovertemplate="%{y}: Ø %{x:+.1f}%<extra></extra>"))
        d.plotly_chart(style(f.update_layout(title="Ø Rendite je Ausstiegsgrund (%)", hovermode="closest"), 300),
                       width="stretch")
        sm_ = cl.assign(Typ=cl.meta.fillna("").str.contains('"explore": true').map({True: "Erkundung (unbewertet)", False: "bewerteter Wal"}))
        g = sm_.groupby("Typ").agg(Trades=("id", "count"), Treffer=("pnl_sol", lambda x: (x > 0).mean()),
                                   PnL=("pnl_sol", "sum")).reset_index()
        e.dataframe(g.assign(Treffer=g.Treffer.map(lambda v: f"{v*100:.0f}%"), PnL=g.PnL.map(lambda v: f"{v:+.3f} SOL")),
                    hide_index=True, width="stretch")
        e.caption("Erkundung = Wale ohne genug Historie, im Papiermodus mit halber Größe kopiert, damit das "
                  "Ranking schneller lernt. Live wird nur bewerteten Walen gefolgt.")
    st.subheader("Modus-Vergleich")
    bs = by_strat[by_strat.trades > 0] if "trades" in by_strat else by_strat
    if bs.empty:
        st.caption("Noch keine abgeschlossenen Trades.")
    else:
        lab = bs.strategy.map(lambda x: control.STRATEGY_LABELS.get(x, x))
        a2, b2 = st.columns([1, 1.4])
        f = go.Figure(go.Bar(x=lab, y=bs.pnl_sol, marker_color=[BLUE if v >= 0 else ORANGE for v in bs.pnl_sol],
                             text=bs.pnl_sol.map(lambda v: f"{v:+.3f} SOL"), textposition="outside",
                             hovertemplate="%{x}: %{y:+.3f} SOL<extra></extra>"))
        a2.plotly_chart(style(f.update_layout(title="Gewinn je Modus (SOL)", hovermode="closest"), 280), width="stretch")
        b2.dataframe(pd.DataFrame({
            "Modus": lab, "Trades": bs.trades, "Treffer": bs.win_rate.map(lambda v: f"{v*100:.0f}%"),
            "Ø Trade": bs.avg_pnl_pct.map(pct), "Ø Gewinn": bs.avg_win_pct.map(pct), "Ø Verlust": bs.avg_loss_pct.map(pct),
            "Profit-Faktor": bs.profit_factor.round(2), "PnL (SOL)": bs.pnl_sol.round(3),
            "Ø Haltedauer (min)": bs.avg_hold_min.round(1)}), hide_index=True, width="stretch")
    sp = control.preset(conn, "signal")
    sbt = stats.signal_backtest(conn, sp)
    if len(sbt):
        f = go.Figure(go.Scatter(x=sbt.dt, y=sbt.equity_sol, mode="lines", line=dict(color=VIOLET, width=2),
                                 hovertemplate="%{y:+.3f} SOL<extra></extra>"))
        f.add_hline(y=0, line_color=GRAY, line_width=1)
        st.plotly_chart(style(f.update_layout(
            title=f"Modus 2 im Rückblick mit Preset „{control.get(conn, 'preset:signal')}“: {len(sbt)} Signale, "
                  f"{(sbt.gain > 0).mean()*100:.0f}% Treffer, {sbt.pnl_sol.sum():+.2f} SOL"), 260), width="stretch")
        st.caption("Aus den gemessenen 1-h-Ergebnissen der Signale: Ziel erreicht, sonst 1-h-Ergebnis, begrenzt auf den Stop. Schätzung.")
    bt = stats.backtest(conn, s)
    st.subheader("Modus 1 im Rückblick (walk-forward)")
    if bt.empty:
        st.caption("Erscheint, sobald genug Wal-Käufe ausgewertet sind (`python -m copytrade rank`).")
    else:
        f = go.Figure(go.Scatter(x=bt.dt, y=bt.equity_sol, mode="lines", line=dict(color=AQUA, width=2),
                                 name="Rückblick", hovertemplate="%{y:+.3f} SOL<extra></extra>"))
        f.add_hline(y=0, line_color=GRAY, line_width=1)
        st.plotly_chart(style(f.update_layout(
            title=f"Was die Strategie mit {s.position_size_sol} SOL/Trade verdient hätte: {len(bt)} Trades, "
                  f"{(bt.gain > 0).mean()*100:.0f}% Treffer, {bt.pnl_sol.sum():+.2f} SOL"), 280),
            width="stretch")
        st.caption("Ohne Blick in die Zukunft: ein Kauf zählt nur, wenn der Wal schon vorher gut bewertet war. "
                   "Basis sind 1-Minuten-Kerzen, keine Ticks; echte Fills können schlechter sein.")

# ---------------------------------------------------------------- whales
with tab_wal:
    rk = wallets[wallets.samples.notna()].copy()
    if rk.empty:
        st.caption("Noch keine bewerteten Wale. Das Ranking braucht Käufe, die mindestens "
                   f"{s.pump_window_min} min alt sind, und Kerzendaten dazu.")
    else:
        f = px.scatter(rk, x=rk.hit_rate * 100, y=rk.median_first_pump * 100, size=rk.samples.clip(lower=1),
                       hover_name=rk.address.map(short), color_discrete_sequence=[BLUE],
                       labels={"x": "Pump-Quote (%)", "y": "Median erster Pump (%)"},
                       custom_data=[rk.samples, rk.expected_value * 100, rk.median_lead_min])
        f.update_traces(marker=dict(line=dict(width=2, color="white")),
                        hovertemplate="%{hovertext}<br>Pump-Quote %{x:.0f}% · erster Pump %{y:.0f}%<br>"
                                      "%{customdata[0]} Käufe · EV %{customdata[1]:+.1f}% · Copy-Ansturm nach %{customdata[2]:.1f} min<extra></extra>")
        st.plotly_chart(style(f.update_layout(title="Welche Wale lösen Pumps aus? (Größe = Anzahl Käufe)",
                                              hovermode="closest"), 340), width="stretch")
        view = rk.assign(
            Wallet=rk.address, Quelle=rk.source, Käufe=rk.samples.astype(int),
            **{"Pump-Quote": rk.hit_rate.map(pct).str.replace("+", ""),
               "erster Pump": rk.median_first_pump.map(pct),
               "Pump nach (min)": rk.median_pump_min.round(1),
               "Copy-Ansturm nach (min)": rk.median_lead_min.round(1),
               "EV für uns": rk.expected_value.map(pct),
               "Ziel": rk.take_profit.map(pct),
               "Kopiert": rk.copied, "Copy-PnL (SOL)": rk.copy_pnl_sol.round(3)},
            Score=(rk.score * 100).round(1), aktiv=rk.active.astype(bool))
        st.dataframe(view[["Wallet", "Score", "Käufe", "Pump-Quote", "erster Pump", "Pump nach (min)",
                           "Copy-Ansturm nach (min)", "EV für uns", "Ziel", "Kopiert", "Copy-PnL (SOL)", "Quelle", "aktiv"]],
                     hide_index=True, width="stretch",
                     column_config={"Wallet": st.column_config.TextColumn(width="medium")})
        st.caption("Score = erwarteter Gewinn pro Kopie nach Kosten × Vertrauen (mehr Käufe = mehr Vertrauen). "
                   "EV für uns rechnet mit unserer Verzögerung von "
                   f"{s.assumed_latency_s:.0f}s, nicht mit dem Einstieg des Wals.")
    unr = wallets[wallets.samples.isna()]
    if len(unr):
        with st.expander(f"{len(unr)} noch nicht bewertete Wallets"):
            st.dataframe(unr[["address", "source", "active", "last_swap_dt"]], hide_index=True, width="stretch")

# ---------------------------------------------------------------- timing
with tab_timing:
    o = outc.dropna(subset=["first_pump_min"])
    if o.empty:
        st.caption("Timing-Daten erscheinen, sobald Wal-Käufe ausgewertet sind.")
    else:
        a, b = st.columns(2)
        lead = o.inflow_lead_min.dropna().clip(upper=60)
        f = go.Figure(go.Histogram(x=lead, xbins=dict(start=0, end=60, size=1), marker_color=BLUE,
                                   hovertemplate="%{x} min: %{y} Käufe<extra></extra>"))
        f.add_vline(x=s.assumed_latency_s / 60, line_color=ORANGE, line_width=2,
                    annotation_text="unsere Verzögerung", annotation_position="top right")
        a.plotly_chart(style(f.update_layout(title="Wann kommen die Copy-Trader? (min nach Wal-Kauf)",
                                             hovermode="closest"), 300), width="stretch")
        p = o[o.pumped == 1].first_pump_min.clip(upper=s.pump_window_min)
        f = go.Figure(go.Histogram(x=p, nbinsx=30, marker_color=AQUA,
                                   hovertemplate="%{x} min: %{y} Pumps<extra></extra>"))
        b.plotly_chart(style(f.update_layout(title="Wann ist der erste Pump oben? (min nach Wal-Kauf)",
                                             hovermode="closest"), 300), width="stretch")
        k = st.columns(4)
        k[0].metric("Wal-Käufe mit Pump", f"{o.pumped.mean()*100:.0f}%", help=f"≥ +{s.pump_min_gain:.0%} bevor {s.pump_retrace:.0%} Rücksetzer")
        k[1].metric("Median Copy-Ansturm", f"{o.inflow_lead_min.median():.1f} min")
        k[2].metric("Median erster Pump oben", f"{p.median():.1f} min" if len(p) else "–")
        k[3].metric("Median erster Pump", pct(o[o.pumped == 1].first_pump_gain.median()))
        f = px.scatter(o, x=o.inflow_lead_min.clip(upper=60), y=o.achievable_gain * 100, color_discrete_sequence=[BLUE],
                       labels={"x": "Copy-Ansturm nach (min)", "y": "für uns erreichbar (%)"},
                       hover_name=o.mint.map(short))
        f.update_traces(marker=dict(size=8, line=dict(width=1, color="white")))
        f.add_hline(y=0, line_color=GRAY, line_width=1)
        st.plotly_chart(style(f.update_layout(title="Je früher die Masse kommt, desto weniger bleibt für uns?",
                                              hovermode="closest"), 300), width="stretch")

# ---------------------------------------------------------------- self-learning
with tab_learn:
    lst = learn.status(conn)
    cols = st.columns(2)
    for col, strat in zip(cols, control.STRATEGIES):
        info = lst[strat]
        with col.container(border=True):
            st.markdown(f"**{control.STRATEGY_LABELS.get(strat, strat)}**")
            on = st.toggle("Selbstlernen", value=info["autotune"], key=f"autotune_{strat}")
            if on != info["autotune"]:
                control.set_value(conn, f"autotune:{strat}", "1" if on else "0")
                st.rerun()
            st.caption(f"{info['total']} Einstiege ausgewertet {info['evaluated']}"
                       + (f", noch {info['needed_for_tuning']} bis zum ersten Tuning" if info["needed_for_tuning"] else ""))
            act = info["active_version"]
            if act:
                st.success(f"Version {act['version']}: Ziel +{act['take_profit']:.0%}, Stop {act['stop_loss']:.0%}, "
                           f"Trailing {act['trail_activate']:.0%}→{act['trail_drawdown']:.0%}, "
                           f"Haltezeit {act['max_hold_min']:.0f} min, Copy-Ansturm-Ausstieg "
                           f"{'ja' if act.get('inflow_exit') else 'nein'}"
                           + (f", Einstieg {act['delay_s']} s nach dem Wal" if strat == "whale" else "")
                           + (f"; Filter: {act['block']}" if act.get("block") else ""))
            else:
                st.info("Noch keine gelernte Version aktiv: dein Preset gilt.")
            if info["wallet_versions"]:
                st.caption(f"{info['wallet_versions']} Wale haben eigene gelernte Ausstiege.")
    lstrat = st.radio("Modus", control.STRATEGIES, horizontal=True, key="learn_strat",
                      format_func=lambda x: control.STRATEGY_LABELS.get(x, x))
    vers = learn.versions(conn, lstrat)
    if len(vers):
        st.subheader("Entwicklung der Parameter")
        f = go.Figure()
        f.add_scatter(x=vers.dt, y=vers.test_gain, mode="markers+lines", name="gelernt (Test)",
                      marker=dict(color=[AQUA if a else GRAY for a in vers.accepted], size=10), line=dict(color=AQUA))
        f.add_scatter(x=vers.dt, y=vers.baseline_test_gain, mode="lines", name="bisher (Test)", line=dict(color=ORANGE, dash="dot"))
        f.add_hline(y=0, line_color=GRAY, line_width=1)
        st.plotly_chart(style(f.update_layout(title="Ø Netto pro Trade auf ungesehenen Daten (farbig = übernommen)",
                                              yaxis_tickformat=".0%"), 300), width="stretch")
        st.dataframe(vers.assign(Zeit=vers.dt, Bereich=vers.scope.map(lambda x: "Modus" if x == "mode" else short(x)),
                                 Übernommen=vers.accepted.astype(bool), Aktiv=vers.active.astype(bool),
                                 Test=vers.test_gain.map(pct), Bisher=vers.baseline_test_gain.map(pct),
                                 Parameter=vers.params, Trades=vers.trades, PnL=vers.pnl_sol.round(4))
                     [["Zeit", "Bereich", "Übernommen", "Aktiv", "Test", "Bisher", "n_train", "n_test", "Trades", "PnL",
                       "Parameter"]], hide_index=True, width="stretch")
    c1, c2 = st.columns(2)
    surf = learn.variant_surface(conn, lstrat)
    if len(surf):
        hm = surf.pivot(index="stop_loss", columns="take_profit", values="gain")
        c1.plotly_chart(style(px.imshow(hm, text_auto=".1%", color_continuous_scale="RdYlGn", aspect="auto",
                                        labels=dict(x="Kursziel", y="Stop-Loss", color="Ø Netto"))
                              .update_layout(title=f"Was hätte sich gelohnt? ({int(surf.n.iloc[0])} Einstiege)"), 320),
                        width="stretch")
    cmp = learn.exit_rule_comparison(conn, lstrat)
    if len(cmp):
        c2.plotly_chart(style(px.bar(cmp, x="style", y="avg_gain", hover_data=["best_variant"],
                                     color_discrete_sequence=[VIOLET])
                              .update_layout(title="Eigene Trades: Ausstiegsregeln im Vergleich", yaxis_tickformat=".0%",
                                             xaxis_title=""), 320), width="stretch")
    att = learn.attribution(conn, lstrat)
    if len(att):
        st.subheader("Was bringt Gewinn? (mit den aktuellen Einstellungen)")
        st.plotly_chart(style(px.bar(att, x="bucket", y="avg_gain", color="feature", facet_col="feature",
                                     hover_data=["n", "win_rate"], facet_col_wrap=4)
                              .update_xaxes(matches=None).update_layout(yaxis_tickformat=".0%", showlegend=False), 420),
                        width="stretch")
    if not len(vers) and not len(surf):
        st.caption("Das Lernen braucht 4,5 h alte Einstiege mit Kursdaten. Es läuft alle 10 min in der App "
                   "oder von Hand: `python -m copytrade learn`.")

# ---------------------------------------------------------------- trades
with tab_trades:
    st.subheader("Abgeschlossen")
    cl = pos[pos.status == "closed"].sort_values("closed_at", ascending=False) if len(pos) else pos
    if len(cl):
        st.dataframe(cl.assign(Coin=cl.symbol, Kauf=cl.opened_at_dt, Verkauf=cl.closed_at_dt,
                               Haltedauer=cl.hold_min.round(1), Einsatz=cl.entry_cost_sol.round(3),
                               Ergebnis=cl.pnl_pct.map(pct), PnL=cl.pnl_sol.round(4), Grund=cl.exit_reason,
                               Modus=cl.strategy.map(lambda x: control.STRATEGY_LABELS.get(x, x)),
                               Grund2=cl.exit_reason.map(lambda r: strategy_mod.EXIT_NAMES.get(r, r)),
                               MC_Kauf=cl.entry_mcap_usd.map(usd), MC_Verkauf=cl.exit_mcap_usd.map(usd),
                               Wal=cl.wallet.map(short), Mint=cl.mint,
                               Rug=cl.safety_level.fillna("–") if "safety_level" in cl else "–")
                     .drop(columns=["Grund"]).rename(columns={"Grund2": "Grund", "MC_Kauf": "MC Kauf", "MC_Verkauf": "MC Verkauf"})
                     [["Coin", "Modus", "Kauf", "Verkauf", "Haltedauer", "Einsatz", "Ergebnis", "PnL", "Grund",
                       "MC Kauf", "MC Verkauf", "Rug", "Wal", "Mint"]], hide_index=True, width="stretch")
    else:
        st.caption("Noch keine abgeschlossenen Trades.")

# ---------------------------------------------------------------- log
with tab_log:
    kinds = st.multiselect("Art", sorted(ev.kind.unique()) if len(ev) else [],
                           default=[k for k in ("buy", "sell", "error", "discover", "start") if len(ev) and k in set(ev.kind)])
    lv = ev[ev.kind.isin(kinds)] if kinds else ev
    st.dataframe(lv[["dt", "kind", "message"]].rename(columns={"dt": "Zeit", "kind": "Art", "message": "Nachricht"}),
                 hide_index=True, width="stretch")
    st.caption("„skip“ zeigt, warum ein Wal-Kauf nicht kopiert wurde (zu spät, Liquidität, schon gepumpt …).")
