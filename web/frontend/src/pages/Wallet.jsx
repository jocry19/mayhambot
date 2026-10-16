import { EChart, baseOption, catAxis, timeAxis, valueAxis } from "../chart.jsx";
import { EXIT_LABEL, STRAT_LABEL, act, ago, dt, mc, n, pct, solSigned, usd, usdSigned, usePoll } from "../lib.js";
import { CoinLink, PnlCurve } from "../parts.jsx";
import { Addr, Bars, Callout, Card, Delta, Empty, Icon, Table, Tier, Tile } from "../ui.jsx";

const COMP = { win: "Trefferquote", early: "Frühe Einstiege", roi: "ROI", consistency: "Konstanz", timing: "Timing", influence: "Einfluss", rug_avoid: "Rugs meiden" };

export default function Wallet({ address }) {
  const { data: d, error } = usePoll(`/api/wallets/${address}`, 30000);
  if (!d) return error ? <Callout kind="bad" icon="alert">{error}</Callout> : <div className="empty">Lade …</div>;
  const m = d.model || {};
  const c = d.core || {};
  const s = { ...c, ...m };
  const cr = d.copy_rank;
  return (
    <div className="col" style={{ gap: 16 }}>
      <div className="card" style={{ padding: "14px 16px" }}>
        <div className="row" style={{ gap: 16 }}>
          <div>
            <div className="row" style={{ gap: 10 }}>
              <span style={{ fontSize: 18, fontWeight: 750 }} className="mono">{c.label || `${address.slice(0, 6)}…${address.slice(-6)}`}</span>
              <Tier t={d.tier} />
              {d.copy_tracked && <span className="badge accent">Copy-Trading beobachtet</span>}
              {c.manual ? <span className="badge">eigene</span> : null}
            </div>
            <div className="row dim" style={{ marginTop: 4, fontSize: 12 }}>
              <Addr a={address} k={8} />
              <a href={`https://solscan.io/account/${address}`} target="_blank" rel="noreferrer">Solscan <Icon name="ext" size={11} /></a>
              <a href={`https://gmgn.ai/sol/address/${address}`} target="_blank" rel="noreferrer">GMGN <Icon name="ext" size={11} /></a>
            </div>
          </div>
          <span className="sp" />
          <button className="btn" onClick={() => act(`/api/wallets/${address}/follow`)}><Icon name="star" />Folgen (Signale + Copy-Trading)</button>
        </div>
      </div>

      <div className="tiles">
        <Tile label="Score" value={s.score != null ? n(s.score, 0) : "–"} sub={d.tier ? `Tier ${d.tier}` : "kein Tier"} />
        <Tile label="Trefferquote" value={pct(s.win_rate)} sub={`${n(s.n_tokens ?? s.tokens_traded)} Coins gehandelt`} />
        <Tile label="ROI" value={pct(s.roi)} cls={s.roi > 0 ? "up" : s.roi < 0 ? "down" : ""} sub={`Früh-Quote ${pct(s.early_rate)}`} />
        <Tile label="Gewinn" value={usdSigned(s.total_pnl_usd)} cls={s.total_pnl_usd > 0 ? "up" : "down"} sub={`realisiert ${usdSigned(s.realized_pnl_usd)}`} />
        <Tile label="Einfluss" value={m.influence_followers != null ? n(m.influence_followers, 1) : "–"} sub={m.influence_usd != null ? `Ø Nachkäufer · ${usd(m.influence_usd)} folgen` : "nach Modell-Lauf"} />
        <Tile label="Einstieg typisch" value={mc(m.median_entry_mcap)} sub={m.median_min_to_2x != null ? `${n(m.median_min_to_2x, 0)} min bis 2x (Median)` : "–"} />
        {cr && <Tile label="Copy-Pump-Quote" value={pct(cr.hit_rate)} sub={`${cr.samples} Käufe · erster Pump ${pct(cr.median_first_pump)}`} />}
      </div>

      <div className="grid g3">
        <Card title="Warum dieser Score" sub="Bausteine des Modell-Scores (0–1)">
          {m.components && Object.keys(m.components).length ? (
            <Bars max={1} items={Object.entries(m.components).map(([k, v]) => ({ label: COMP[k] || k, value: v }))} fmt={(v) => n(v, 2)} />
          ) : <Empty>Erst nach dem nächsten Modell-Lauf</Empty>}
        </Card>
        <Card title="Gewinn kumuliert" sub="Nach Coin, in Kaufreihenfolge (USD)">
          <PnlCurve points={d.pnl_curve} key_="pnl_usd" label="Gewinn kumuliert" unit="USD" height={200} />
        </Card>
        <Card title="Tier über Modell-Versionen" sub="Bleibt die Wallet gut?">
          {d.tier_history.length > 1 ? (
            <EChart height={200} deps={[d.tier_history]} ariaLabel="Score je Modell-Version"
              build={(c) => baseOption(c, {
                xAxis: catAxis(c, d.tier_history.map((h) => `v${h.version}`)), yAxis: valueAxis(c, (v) => v, { min: 0, max: 100 }),
                series: [{ name: "Score", type: "line", symbolSize: 7, lineStyle: { width: 2, color: c.s[0] }, itemStyle: { color: c.s[0] }, data: d.tier_history.map((h) => h.score) }],
              })} />
          ) : <Empty>Noch keine Historie</Empty>}
        </Card>
      </div>

      <div className="grid g-wide-left">
        <Card title="Positionen" sub="Jede Coin-Position dieser Wallet" flush>
          <Table rows={d.positions} rowKey={(r) => r.mint} tall max={300} cols={[
            { k: "symbol", l: "Coin", fmt: (r) => <CoinLink mint={r.mint} symbol={r.symbol} /> },
            { k: "first_buy_ts", l: "Erster Kauf", r: true, fmt: (r) => dt(r.first_buy_ts) },
            { k: "entry_mcap_usd", l: "Einstieg MC", r: true, fmt: (r) => mc(r.entry_mcap_usd) },
            { k: "buy_usd", l: "Gekauft", r: true, fmt: (r) => usd(r.buy_usd) },
            { k: "sell_usd", l: "Verkauft", r: true, fmt: (r) => usd(r.sell_usd) },
            { k: "peak_multiple", l: "Hoch danach", r: true, fmt: (r) => (r.peak_multiple ? `${n(r.peak_multiple, 1)}x` : "–") },
            { k: "pnl_usd", l: "PnL", r: true, fmt: (r) => <span className={r.pnl_usd > 0 ? "up" : r.pnl_usd < 0 ? "down" : ""}>{usdSigned(r.pnl_usd)}</span> },
            { k: "roi", l: "ROI", r: true, title: "Gewinn geteilt durch Einsatz, inkl. offener Bestände", sort: (r) => (r.buy_usd ? (r.pnl_usd || 0) / r.buy_usd : r.roi), fmt: (r) => <Delta v={r.buy_usd ? (r.pnl_usd || 0) / r.buy_usd : r.roi} d={0} arrow={false} /> },
          ]} />
        </Card>
        <Card title="Letzte Trades" flush>
          <Table rows={d.trades} rowKey={(r) => r.sig + r.side + r.mint} tall max={300} cols={[
            { k: "ts", l: "Zeit", fmt: (r) => dt(r.ts) },
            { k: "side", l: "", fmt: (r) => <span className={`badge ${r.side === "buy" ? "good" : "bad"}`}>{r.side === "buy" ? "Kauf" : "Verkauf"}</span> },
            { k: "symbol", l: "Coin", fmt: (r) => <CoinLink mint={r.mint} symbol={r.symbol} /> },
            { k: "usd_amount", l: "Betrag", r: true, fmt: (r) => usd(r.usd_amount) },
            { k: "sol_amount", l: "SOL", r: true, fmt: (r) => n(r.sol_amount, 2) },
          ]} />
        </Card>
      </div>

      {(d.copy_outcomes.length > 0 || d.copies.length > 0) && (
        <div className="grid g2">
          <Card title="Was nach ihren Käufen passierte" sub="Copy-Trading-Messung: erster Pump, Zeit bis Gipfel, Vorsprung vor dem Copy-Ansturm" flush>
            <Table rows={d.copy_outcomes} rowKey={(r) => r.sig} tall cols={[
              { k: "ts", l: "Kauf", fmt: (r) => dt(r.ts) },
              { k: "symbol", l: "Coin", fmt: (r) => <CoinLink mint={r.mint} symbol={r.symbol} /> },
              { k: "sol_amount", l: "SOL", r: true, fmt: (r) => n(r.sol_amount, 1) },
              { k: "pumped", l: "Pump", fmt: (r) => (r.pumped ? <span className="badge good">ja</span> : <span className="badge">nein</span>) },
              { k: "first_pump_gain", l: "Erster Pump", r: true, fmt: (r) => pct(r.first_pump_gain) },
              { k: "first_pump_min", l: "bis Gipfel", r: true, fmt: (r) => `${n(r.first_pump_min, 0)} min` },
              { k: "inflow_lead_min", l: "Vorsprung", r: true, title: "Minuten vor dem Copy-Ansturm", fmt: (r) => (r.inflow_lead_min != null ? `${n(r.inflow_lead_min, 1)} min` : "–") },
              { k: "achievable_gain", l: "Für uns drin", r: true, fmt: (r) => <Delta v={r.achievable_gain} d={0} arrow={false} /> },
            ]} />
          </Card>
          <Card title="Unsere Kopien dieser Wallet" flush>
            <Table rows={d.copies} rowKey={(r) => r.id} cols={[
              { k: "opened_at", l: "Kauf", fmt: (r) => dt(r.opened_at) },
              { k: "symbol", l: "Coin", fmt: (r) => <CoinLink mint={r.mint} symbol={r.symbol} /> },
              { k: "strategy", l: "Modus", fmt: (r) => STRAT_LABEL[r.strategy] || r.strategy },
              { k: "exit_reason", l: "Ausstieg", fmt: (r) => (r.status === "open" ? "offen" : EXIT_LABEL[r.exit_reason] || r.exit_reason) },
              { k: "pnl_pct", l: "Ergebnis", r: true, fmt: (r) => <Delta v={r.pnl_pct} /> },
              { k: "pnl_sol", l: "SOL", r: true, fmt: (r) => solSigned(r.pnl_sol) },
            ]} />
          </Card>
        </div>
      )}
      <div className="dim" style={{ fontSize: 12 }}>Zuletzt aktiv {ago(c.last_trade_ts)}</div>
    </div>
  );
}

export { timeAxis };
