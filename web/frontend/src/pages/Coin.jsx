import { useEffect, useMemo, useState } from "react";
import { EChart, baseOption, tierColor, timeAxis, tipRow, valueAxis } from "../chart.jsx";
import { ALGO_LABEL, EXIT_LABEL, STRAT_LABEL, TIER_LABEL, TIER_SOURCE, act, ago, dt, go, mc, n, pct, pctSigned, price, sol, solSigned, usd, usdSigned, usePoll } from "../lib.js";
import { Breakdown, FlowChart, Outcome, SignalDetail, WalletLink } from "../parts.jsx";
import { Addr, Callout, Card, Delta, Empty, Icon, Legend, Modal, Score, Seg, Switch, Table, Tier, TierCounts, Tile } from "../ui.jsx";
import { BuyModal } from "../buy.jsx";
import { PosRisk, RiskBadge, SafetyCard } from "../safety.jsx";

function PriceChart({ d, showTrades, hl, onPick }) {
  const sigs = [...d.signals].sort((a, b) => a.ts - b.ts);
  const num = new Map(sigs.map((s, i) => [s.id, i + 1]));
  const priceKey = d.price.some((p) => p.mcap_usd) ? "mcap_usd" : "price_usd";
  const fmt = priceKey === "mcap_usd" ? mc : price;
  const at = (ts) => {
    let best = null;
    for (const p of d.price) { if (p.ts <= ts) best = p; else break; }
    return best ? best[priceKey] : d.price[0]?.[priceKey];
  };
  const tiered = showTrades ? d.tiered_trades.filter((t) => t.side === "buy") : [];
  const pos = d.positions || [];
  if (d.price.length < 2) return <Empty>Noch zu wenig Preisdaten für diesen Coin</Empty>;
  return (
    <EChart height={330} group="coin" deps={[d, showTrades, hl]} ariaLabel="Market Cap mit Signalen"
      onClick={(p) => p.componentType === "markPoint" && p.data?.sid != null && onPick(p.data.sid)}
      build={(c) => baseOption(c, {
        grid: { left: 8, right: 14, top: 26, bottom: 8, containLabel: true },
        tooltip: {
          ...baseOption(c).tooltip,
          formatter: (ps) => {
            const p = ps.find((x) => x.seriesId === "px") || ps[0];
            const t = p.value[0] / 1000;
            const near = sigs.filter((s) => Math.abs(s.ts - t) < 90);
            return `<div style="color:${c.muted};margin-bottom:4px">${dt(t)}</div>${tipRow(c.s[0], priceKey === "mcap_usd" ? "Market Cap" : "Preis", fmt(p.value[1]))}` +
              ps.filter((x) => x.seriesId !== "px").map((x) => tipRow(x.color, x.seriesName, x.data.label || "")).join("") +
              near.map((s) => tipRow(c.s[1], `Signal #${num.get(s.id)}`, `Score ${n(s.score, 0)}`)).join("");
          },
        },
        dataZoom: [{ type: "inside", filterMode: "none" }],
        xAxis: timeAxis(c),
        yAxis: valueAxis(c, (v) => fmt(v), { scale: true }),
        series: [
          {
            id: "px", name: priceKey === "mcap_usd" ? "Market Cap" : "Preis", type: "line", showSymbol: false, lineStyle: { width: 2, color: c.s[0] },
            areaStyle: { color: c.s[0], opacity: 0.06 }, data: d.price.map((p) => [p.ts * 1000, p[priceKey]]),
            markPoint: {
              symbol: "pin", symbolSize: 38, label: { color: "#fff", fontWeight: 700, fontSize: 11 },
              data: sigs.map((s) => ({
                coord: [s.ts * 1000, at(s.ts)], value: `#${num.get(s.id)}`, sid: s.id,
                itemStyle: { color: String(s.id) === String(hl) ? c.text : c.s[1], borderColor: c.surface, borderWidth: 2 },
              })),
            },
          },
          ...["ultra", "smart", "good"].map((k) => ({
            id: `t-${k}`, name: `Kauf ${TIER_LABEL[k]}`, type: "scatter", symbolSize: (v, p) => Math.max(7, Math.min(18, 5 + Math.sqrt(p.data.usd || 0) / 6)),
            itemStyle: { color: tierColor(c, k), borderColor: c.surface, borderWidth: 1.5 },
            data: tiered.filter((t) => t.tier === k && t.mcap_usd).map((t) => ({ value: [t.ts * 1000, priceKey === "mcap_usd" ? t.mcap_usd : t.price_usd], usd: t.usd_amount, label: `${usd(t.usd_amount)} · ${t.wallet.slice(0, 4)}…` })),
          })),
          {
            id: "bot-in", name: "Bot-Kauf", type: "scatter", symbol: "triangle", symbolSize: 13, itemStyle: { color: c.good, borderColor: c.surface, borderWidth: 1.5 },
            data: pos.filter((p) => p.entry_mcap_usd || p.entry_price_usd).map((p) => ({ value: [p.opened_at * 1000, priceKey === "mcap_usd" ? p.entry_mcap_usd : p.entry_price_usd], label: `${sol(p.size_sol)} (${STRAT_LABEL[p.strategy] || p.strategy})` })),
          },
          {
            id: "bot-out", name: "Bot-Verkauf", type: "scatter", symbol: "triangle", symbolRotate: 180, symbolSize: 13, itemStyle: { color: c.bad, borderColor: c.surface, borderWidth: 1.5 },
            data: pos.filter((p) => p.closed_at && p.exit_mcap_usd).map((p) => ({ value: [p.closed_at * 1000, p.exit_mcap_usd], label: `${pctSigned(p.pnl_pct, 0)} · ${EXIT_LABEL[p.exit_reason] || p.exit_reason}` })),
          },
        ],
      })} />
  );
}

function ScoreChart({ d }) {
  const m = d.metrics || [];
  if (m.length < 2) return <Empty>Noch keine Score-Historie</Empty>;
  const hasPred = m.some((x) => x.prediction != null);
  return (
    <>
      <EChart height={170} group="coin" deps={[m]} ariaLabel="Signal-Score über Zeit"
        build={(c) => baseOption(c, {
          dataZoom: [{ type: "inside", filterMode: "none" }],
          tooltip: { ...baseOption(c).tooltip, valueFormatter: (v) => (v == null ? "–" : n(v, 0)) },
          xAxis: timeAxis(c), yAxis: valueAxis(c, (v) => v, { min: 0, max: 100 }),
          series: [
            { name: "Signal-Score", type: "line", showSymbol: false, lineStyle: { width: 2, color: c.s[1] }, data: m.map((x) => [x.ts * 1000, x.score]),
              markLine: { symbol: "none", silent: true, lineStyle: { color: c.muted, type: "solid", width: 1 }, label: { color: c.muted, formatter: "Schwelle", position: "insideEndTop" }, data: [{ yAxis: 60 }] } },
            ...(hasPred ? [{ name: "Pump-Chance 1 h (%)", type: "line", showSymbol: false, lineStyle: { width: 1.5, color: c.s[2] }, data: m.map((x) => [x.ts * 1000, x.prediction != null ? x.prediction * 100 : null]) }] : []),
          ],
        })} />
      <Legend items={[{ label: "Signal-Score (0–100)", color: "var(--series-2)" }, ...(hasPred ? [{ label: "Pump-Chance 1 h in %", color: "var(--series-3)" }] : [])]} />
    </>
  );
}

function SignalCards({ signals, hl }) {
  const sigs = [...signals].sort((a, b) => a.ts - b.ts);
  const [open, setOpen] = useState(() => new Set(hl ? [String(hl)] : sigs.length ? [String(sigs[sigs.length - 1].id)] : []));
  useEffect(() => {
    if (hl) {
      setOpen((o) => new Set([...o, String(hl)]));
      setTimeout(() => document.getElementById(`sig-${hl}`)?.scrollIntoView({ behavior: "smooth", block: "center" }), 200);
    }
  }, [hl]);
  if (!sigs.length) return <Empty>Für diesen Coin gab es noch kein Signal. Score-Verlauf und Tier-Käufe oben zeigen, wie nah er dran war.</Empty>;
  return (
    <div className="col" style={{ gap: 10 }}>
      {sigs.map((s, i) => {
        const id = String(s.id);
        const isOpen = open.has(id);
        return (
          <div key={id} id={`sig-${id}`} className={`sig-card ${id === String(hl) ? "hl" : ""}`}>
            <div className="sig-head" onClick={() => setOpen((o) => { const x = new Set(o); x.has(id) ? x.delete(id) : x.add(id); return x; })}>
              <div className="sig-num">#{i + 1}</div>
              <div className="kv"><span className="k">Zeit</span><span className="v">{dt(s.ts)}</span></div>
              <div className="kv"><span className="k">Market Cap</span><span className="v">{mc(s.mcap_usd)}</span></div>
              <div className="kv"><span className="k">Score</span><span className="v"><Score v={s.score} g={s.grade} /></span></div>
              <div className="kv"><span className="k">Smart-Wallets U/S/G</span><span className="v"><TierCounts tiers={s.tiers} /></span></div>
              <div className="kv"><span className="k">Smart-Inflow</span><span className="v">{usd(s.inflow_usd)}{s.inflow_sol != null ? <span className="dim"> · {n(s.inflow_sol, 1)} SOL</span> : null}</span></div>
              <div className="kv"><span className="k">Prognose</span><span className="v">{s.prediction != null ? pct(s.prediction) : "–"}</span></div>
              <div className="kv"><span className="k">Max 1 h</span><span className="v"><Delta v={s.outcome.max_ret_1h} d={0} /></span></div>
              <span className="sp" />
              <Icon name="chevron" size={16} />
            </div>
            {isOpen && <SignalDetail s={s} />}
          </div>
        );
      })}
    </div>
  );
}

export default function Coin({ mint, hl }) {
  const { data: d, error } = usePoll(`/api/coins/${mint}`, 15000);
  const [showTrades, setShowTrades] = useState(true);
  const [walletTab, setWalletTab] = useState("tier");
  const [buy, setBuy] = useState(false);
  const [pick, setPick] = useState(hl);
  useEffect(() => setPick(hl), [hl]);
  const tok = d?.token;
  const wallets = useMemo(() => (d ? (walletTab === "tier" ? d.wallets.filter((w) => w.tier) : d.wallets) : []), [d, walletTab]);
  if (!d) return error ? <Callout kind="bad" icon="alert">{error}</Callout> : <div className="empty">Lade …</div>;
  const st = d.stats;
  const sym = tok?.symbol || mint.slice(0, 6);
  return (
    <div className="col" style={{ gap: 16 }}>
      <div className="card" style={{ padding: "14px 16px" }}>
        <div className="row" style={{ gap: 18, alignItems: "flex-start" }}>
          <div>
            <div className="row" style={{ gap: 10 }}>
              <span style={{ fontSize: 22, fontWeight: 750 }}>{sym}</span>
              <span className="t2">{tok?.name}</span>
              {st.signals > 0 && <span className="badge accent">{st.signals} Signal{st.signals > 1 ? "e" : ""}</span>}
              {st.tiers.ultra > 0 && <span className="badge">{st.tiers.ultra} Ultra drin</span>}
              {d.safety?.checked && <RiskBadge s={d.safety} />}
              {!d.safety?.checked && d.safety?.blocked && <span className="badge risk bad">manuell gesperrt</span>}
            </div>
            <div className="row dim" style={{ marginTop: 4, fontSize: 12 }}>
              <Addr a={mint} k={6} />
              <a href={`https://dexscreener.com/solana/${mint}`} target="_blank" rel="noreferrer">DexScreener <Icon name="ext" size={11} /></a>
              <a href={`https://solscan.io/token/${mint}`} target="_blank" rel="noreferrer">Solscan <Icon name="ext" size={11} /></a>
              <a href={`https://birdeye.so/token/${mint}?chain=solana`} target="_blank" rel="noreferrer">Birdeye <Icon name="ext" size={11} /></a>
            </div>
          </div>
          <span className="sp" />
          <div className="kv"><span className="k">Preis</span><span className="v">{price(tok?.price_usd)}</span></div>
          <div className="kv"><span className="k">Market Cap</span><span className="v">{mc(tok?.mcap_usd)}</span></div>
          <div className="kv"><span className="k">Liquidität</span><span className="v">{mc(tok?.liquidity_usd)}</span></div>
          <div className="kv"><span className="k">1 h</span><span className="v"><Delta v={tok?.price_change_h1 != null ? tok.price_change_h1 / 100 : null} d={0} /></span></div>
          <div className="kv"><span className="k">24 h</span><span className="v"><Delta v={tok?.price_change_h24 != null ? tok.price_change_h24 / 100 : null} d={0} /></span></div>
          <div className="kv"><span className="k">Alter</span><span className="v">{tok?.pair_created_at ? ago(tok.pair_created_at).replace("vor ", "") : "–"}</span></div>
          <button className="btn primary" onClick={() => setBuy(true)}>Kaufen …</button>
        </div>
      </div>
      {buy && tok && <BuyModal mint={tok.mint} symbol={tok.symbol} signal={d.signals[0]} onClose={() => setBuy(false)} />}

      <div className="tiles">
        <Tile label="Tier-Wallets im Coin" value={<TierCounts tiers={st.tiers} />} sub={`${n(st.traders)} Trader insgesamt`} />
        <Tile label="Tier-Netto-Inflow" value={usdSigned(st.tiered_net_usd)} cls={st.tiered_net_usd >= 0 ? "up" : "down"} sub={`Käufe ${usd(st.buy_usd)} · Verkäufe ${usd(st.sell_usd)} (alle)`} />
        <Tile label="Erster Tier-Einstieg" value={mc(st.first_tiered_mcap)} sub={st.first_tiered_ts ? dt(st.first_tiered_ts) : "–"} />
        <Tile label="Erstes Signal bei" value={mc(st.first_signal_mcap)} sub={st.first_signal_mcap && tok?.mcap_usd ? `jetzt ${pctSigned(tok.mcap_usd / st.first_signal_mcap - 1, 0)}` : "noch kein Signal"}
          cls={st.first_signal_mcap && tok?.mcap_usd ? (tok.mcap_usd >= st.first_signal_mcap ? "up" : "down") : ""} />
        <Tile label="Hoch / Tief (MC)" value={`${mc(st.ath_mcap)}`} sub={`Tief ${mc(st.low_mcap)}`} />
        <Tile label="Bester Score" value={st.best_score ? n(st.best_score, 0) : "–"} sub={`${n(st.trades)} Trades erfasst`} />
      </div>
      {d.tier_source !== "model" && <Callout>{TIER_SOURCE[d.tier_source]}.</Callout>}
      <SafetyCard mint={mint} symbol={sym} s={d.safety} />

      <Card title={`${sym}: Market Cap, Signale und Smart-Money-Käufe`} sub="Nummern = Signale (Klick öffnet sie unten) · Punkte = Käufe der Tier-Wallets, Größe = Betrag · Dreiecke = Bot-Käufe/-Verkäufe · Mausrad zoomt"
        actions={<label className="row nowrap dim" style={{ fontSize: 12 }}>Tier-Käufe <Switch on={showTrades} onChange={setShowTrades} label="Tier-Käufe einblenden" /></label>}>
        <PriceChart d={d} showTrades={showTrades} hl={pick} onPick={(id) => { setPick(id); go(`/coin/${mint}?signal=${id}`); }} />
        <Legend items={[{ label: "Market Cap", color: "var(--series-1)" }, { label: "Signal", color: "var(--series-2)" },
          ...["ultra", "smart", "good"].map((k) => ({ label: `Kauf ${TIER_LABEL[k]}`, color: `var(--tier-${k})` })),
          { label: "Bot-Kauf ▲ / -Verkauf ▼", color: "var(--good)" }]} />
      </Card>
      <div className="grid g2">
        <Card title="Inflow nach Wallet-Tier" sub={`Netto-Käufe je ${d.flow_bucket_s / 60} min · grau = Crowd`}><FlowChart flows={d.flows} group="coin" height={200} /></Card>
        <Card title="Score-Verlauf" sub="Wie nah der Coin am Signal war (Schwelle 60)"><ScoreChart d={d} /></Card>
      </div>

      {d.prediction_live && !d.prediction_live.error && (
        <Card title="Live-Prognose des Modells" sub={`Modell v${d.prediction_live.version ?? "?"}${d.prediction_live.algo ? ` · ${ALGO_LABEL[d.prediction_live.algo] || d.prediction_live.algo}` : ""} · jetzt berechnet`}>
          <div className="grid g-wide-right">
            <div className="col">
              <div className="row" style={{ gap: 18 }}>
                <div className="kv"><span className="k">Score</span><span className="v"><Score v={d.prediction_live.score} /></span></div>
                <div className="kv"><span className="k">+30 % in 15 min</span><span className="v">{pct(d.prediction_live.p_15m)}</span></div>
                <div className="kv"><span className="k">+50 % in 1 h</span><span className="v">{pct(d.prediction_live.p_1h)}</span></div>
                <div className="kv"><span className="k">+100 % in 6 h</span><span className="v">{pct(d.prediction_live.p_6h)}</span></div>
              </div>
              <TierCounts tiers={d.prediction_live.tiers} />
            </div>
            <Breakdown items={d.prediction_live.breakdown} />
          </div>
        </Card>
      )}

      <Card title={`Alle Signale zu ${sym}`} sub="Jedes Signal mit Market Cap, Wallets je Tier, Inflow, Prognose, Gewichtung der Eingänge und Ergebnis">
        <SignalCards signals={d.signals} hl={pick} />
      </Card>

      <div className="grid g-wide-left">
        <Card title="Wallets in diesem Coin" sub="Wer wann bei welcher Market Cap eingestiegen ist" flush
          actions={<Seg value={walletTab} onChange={setWalletTab} options={[["tier", "Nur Tier-Wallets"], ["all", "Alle"]]} />}>
          <Table rows={wallets} rowKey={(r) => r.address} onRow={(r) => go(`/wallet/${r.address}`)} tall max={300}
            empty="Noch keine Tier-Wallet in diesem Coin" cols={[
              { k: "tier", l: "Tier", fmt: (r) => <Tier t={r.tier} />, sort: (r) => ({ ultra: 3, smart: 2, good: 1 }[r.tier] || 0) },
              { k: "address", l: "Wallet", fmt: (r) => <WalletLink a={r.address} /> },
              { k: "score", l: "Score", r: true, fmt: (r) => n(r.score, 0) },
              { k: "first_ts", l: "Erster Kauf", r: true, fmt: (r) => dt(r.first_ts) },
              { k: "entry_mcap_usd", l: "Einstieg MC", r: true, fmt: (r) => mc(r.entry_mcap_usd) },
              { k: "buy_usd", l: "Gekauft", r: true, fmt: (r) => usd(r.buy_usd) },
              { k: "sell_usd", l: "Verkauft", r: true, fmt: (r) => usd(r.sell_usd) },
              { k: "pnl_usd", l: "PnL", r: true, fmt: (r) => <span className={r.pnl_usd > 0 ? "up" : r.pnl_usd < 0 ? "down" : ""}>{usdSigned(r.pnl_usd)}</span> },
            ]} />
        </Card>
        <Card title="Trades der Tier-Wallets" sub="Neueste zuerst" flush>
          <Table rows={d.tiered_trades} rowKey={(r) => r.sig + r.side + r.wallet} tall max={150} empty="Keine Trades von Tier-Wallets" cols={[
            { k: "ts", l: "Zeit", fmt: (r) => dt(r.ts) },
            { k: "side", l: "", fmt: (r) => <span className={`badge ${r.side === "buy" ? "good" : "bad"}`}>{r.side === "buy" ? "Kauf" : "Verkauf"}</span> },
            { k: "tier", l: "Tier", fmt: (r) => <Tier t={r.tier} /> },
            { k: "usd_amount", l: "Betrag", r: true, fmt: (r) => usd(r.usd_amount) },
            { k: "mcap_usd", l: "MC", r: true, fmt: (r) => mc(r.mcap_usd) },
            { k: "wallet", l: "Wallet", fmt: (r) => <WalletLink a={r.wallet} /> },
          ]} />
        </Card>
      </div>
      {(d.positions.length > 0 || d.alerts.length > 0) && (
        <Card title="Bot-Trades in diesem Coin" flush>
          <Table rows={d.positions} rowKey={(r) => r.id} cols={[
            { k: "opened_at", l: "Kauf", fmt: (r) => dt(r.opened_at) },
            { k: "strategy", l: "Modus", fmt: (r) => STRAT_LABEL[r.strategy] || r.strategy },
            { k: "mode", l: "Konto", fmt: (r) => <span className={`badge ${r.mode === "live" ? "live" : ""}`}>{r.mode === "live" ? "Echtgeld" : "Papier"}</span> },
            { k: "entry_mcap_usd", l: "Einstieg MC", r: true, fmt: (r) => mc(r.entry_mcap_usd) },
            { k: "size_sol", l: "Größe", r: true, fmt: (r) => sol(r.size_sol) },
            { k: "safety_score", l: "Risiko beim Kauf", fmt: (r) => <PosRisk r={r} /> },
            { k: "status", l: "Status", fmt: (r) => (r.status === "open" ? <span className="badge accent">offen</span> : EXIT_LABEL[r.exit_reason] || r.exit_reason) },
            { k: "pnl_pct", l: "Ergebnis", r: true, fmt: (r) => <Delta v={r.status === "open" ? r.unrealized_pct : r.pnl_pct} d={1} /> },
            { k: "pnl_sol", l: "SOL", r: true, fmt: (r) => solSigned(r.status === "open" ? r.unrealized_sol : r.pnl_sol) },
          ]} />
        </Card>
      )}
    </div>
  );
}

export { Outcome };
