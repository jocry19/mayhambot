import { EChart, baseOption, timeAxis, valueAxis } from "../chart.jsx";
import { MODE_SHORT, STRAT_LABEL, sign, TIER_SOURCE, ago, dt, go, mc, n, pct, sol, solSigned, usd, usdSigned, usePoll } from "../lib.js";
import { CoinLink, FlowChart } from "../parts.jsx";
import { Callout, Card, Delta, Empty, Score, Table, TierCounts, Tile } from "../ui.jsx";

export default function Overview() {
  const { data: d, loading, error } = usePoll("/api/overview", 10000);
  if (!d) return error ? <Callout kind="bad" icon="alert">API nicht erreichbar: {error}</Callout> : <div className="empty">Lade …</div>;
  const paper = d.copytrade?.paper;
  const explore = d.copytrade?.explore;
  const ctl = d.control || {};
  return (
    <div className={`col ${loading ? "" : ""}`} style={{ gap: 16 }}>
      {d.tier_source !== "model" && <Callout icon="info">{TIER_SOURCE[d.tier_source]}. Die Ränge werden genauer, sobald das Forschungsmodell trainiert hat.</Callout>}
      <Board b={d.board} />
      <div className="tiles dense">
        <Tile label="Tier-Wallets" value={<TierCounts tiers={d.tiers} />} sub={`${n(d.wallets_total)} Wallets beobachtet`} />
        <Tile label="Signale 24 h" value={n(d.signals_24h)} sub={`${n(d.tokens_tracked)} Coins im Blick · ${n(d.trades_24h)} Trades`} />
        <Tile label="Smart-Inflow 24 h" value={usdSigned(d.net_tiered_24h, 1)} cls={d.net_tiered_24h >= 0 ? "up" : "down"} sub="netto, alle Tier-Wallets" />
        <Tile label="Signal-Trefferquote" value={d.signal_perf?.hit_rate != null ? pct(d.signal_perf.hit_rate) : "–"}
          sub={d.signal_perf?.n ? `${d.signal_perf.hits} von ${d.signal_perf.n} mit +${pct(d.signal_perf.pump_threshold)} in 1 h` : "noch keine ausgewerteten Signale"} />
        <Tile label="Copy-Trading Papier" value={paper?.pnl_sol != null ? solSigned(paper.pnl_sol, 2) : "–"} cls={(paper?.pnl_sol || 0) >= 0 ? "up" : "down"}
          sub={<>{paper?.trades ? `streng: ${paper.trades} Trades · ${pct(paper.win_rate)} Gewinner · ${paper.open} offen` : "streng: noch keine Trades"}
            {explore && <><br />Erkundung: <span className={(explore.pnl_sol || 0) >= 0 ? "up" : "down"}>{solSigned(explore.pnl_sol, 2)}</span> · {explore.trades || 0} Trades · {explore.open || 0} offen</>}</>} />
        {d.sniper && <SniperTile s={d.sniper} />}
        <Tile label="Betriebsart" value={<span style={{ fontSize: 17 }}>{MODE_SHORT[ctl.modes?.whale] || "–"} / {MODE_SHORT[ctl.modes?.signal] || "–"}</span>}
          sub={`Wale / Signale · ${ctl.paused ? "Käufe pausiert" : ctl.live_ready ? "Echtgeld an" : "Papier"}`} />
      </div>

      <div className="grid g-wide-left">
        <Card title="Smart-Money-Fluss, letzte 24 h" sub="Netto-Käufe je 15 min nach Wallet-Tier; Crowd zuschaltbar">
          <FlowChart flows={d.flows} height={270} crowdDefault={false} />
        </Card>
        <Card title="Neueste Signale" sub="Klick öffnet den Coin mit allen Signalen" flush actions={<a href="#/signals" className="btn sm ghost">Alle</a>}>
          {d.feed.length ? (
            <div className="feed" style={{ maxHeight: 330, overflow: "auto" }}>
              {d.feed.slice(0, 12).map((s) => (
                <div key={s.id} className="feed-i" onClick={() => go(`/coin/${s.mint}?signal=${s.id}`)}>
                  <Score v={s.score} g={s.grade} />
                  <div style={{ minWidth: 0 }}>
                    <div><span className="feed-sym">{s.symbol}</span> <span className="dim">bei MC {mc(s.mcap_usd)}</span></div>
                    <div className="feed-sub">{usd(s.inflow_usd)} Smart-Inflow · {s.prediction != null ? `Prognose ${pct(s.prediction)}` : "ohne Prognose"}</div>
                  </div>
                  <div style={{ textAlign: "right" }}>
                    <TierCounts tiers={s.tiers} />
                    <div className="feed-sub">{ago(s.ts)}</div>
                  </div>
                </div>
              ))}
            </div>
          ) : <Empty>Noch keine Signale. Die Engine feuert, wenn genug Smart Money gleichzeitig in einen Coin fließt.</Empty>}
        </Card>
      </div>

      <div className="grid g-wide-left">
        <Card title="Stärkste Coins jetzt" sub="Signalstärke und Tier-Käufer der letzten 30 min" flush actions={<a href="#/coins" className="btn sm ghost">Alle Coins</a>}>
          <Table rows={d.top_coins} rowKey={(r) => r.mint} onRow={(r) => go(`/coin/${r.mint}`)} max={12} cols={[
            { k: "symbol", l: "Coin", fmt: (r) => <CoinLink mint={r.mint} symbol={r.symbol} /> },
            { k: "strength", l: "Stärke", fmt: (r) => <Score v={r.strength} g={r.grade} /> },
            { k: "buyers_win", l: "Käufer U/S/G", fmt: (r) => <TierCounts tiers={r.buyers_win} />, sort: (r) => r.buyers_win.ultra * 100 + r.buyers_win.smart * 10 + r.buyers_win.good },
            { k: "net_tiered_win", l: "Inflow 30 min", r: true, fmt: (r) => <span className={sign(r.net_tiered_win)}>{usdSigned(r.net_tiered_win)}</span> },
            { k: "mcap_usd", l: "MC", r: true, fmt: (r) => mc(r.mcap_usd) },
            { k: "change_h1", l: "1 h", r: true, fmt: (r) => <Delta v={r.change_h1 != null ? r.change_h1 / 100 : null} d={0} arrow={false} /> },
            { k: "signals", l: "Signale", r: true },
          ]} />
        </Card>
        <Card title="Copy-Trading Papierkonto" sub={paper?.equity_now != null ? `Kontostand ${sol(paper.equity_now)}` : "Equity-Kurve"} actions={<a href="#/copytrade" className="btn sm ghost">Details</a>}>
          {paper?.equity?.length > 1 ? (
            <EChart height={210} deps={[paper.equity]} ariaLabel="Equity"
              build={(c) => baseOption(c, {
                tooltip: { ...baseOption(c).tooltip, valueFormatter: (v) => `${n(v, 3)} SOL` },
                xAxis: timeAxis(c), yAxis: valueAxis(c, (v) => n(v, 1), { scale: true }),
                series: [{ name: "Kontostand", type: "line", showSymbol: false, lineStyle: { width: 2, color: c.s[0] },
                  areaStyle: { color: c.s[0], opacity: 0.08 }, data: paper.equity.map((p) => [p.ts * 1000, p.equity_sol]) }],
              })} />
          ) : <Empty>Noch keine Equity-Daten</Empty>}
          {paper?.trades > 0 && (
            <div className="row" style={{ gap: 18, marginTop: 8 }}>
              <div className="kv"><span className="k">Profit-Faktor</span><span className="v">{paper.profit_factor != null ? n(paper.profit_factor, 2) : "∞"}</span></div>
              <div className="kv"><span className="k">Ø Gewinn</span><span className="v up">{pct(paper.avg_win_pct)}</span></div>
              <div className="kv"><span className="k">Ø Verlust</span><span className="v down">{pct(paper.avg_loss_pct)}</span></div>
              <div className="kv"><span className="k">Max. Drawdown</span><span className="v">{sol(paper.max_drawdown_sol, 2)}</span></div>
            </div>
          )}
        </Card>
      </div>
      {d.model && (
        <div className="dim" style={{ fontSize: 12 }}>
          Modell v{d.model.id} vom {dt(d.model.ts)} · {n(d.model.n_wallets)} Wallets · {n(d.model.n_events)} Events
          {d.model.auc_1h != null && ` · AUC 1 h ${n(d.model.auc_1h, 2)}`} · <a href="#/model">Modell ansehen</a>
        </div>
      )}
    </div>
  );
}

export { STRAT_LABEL };

/** Launch-sniper (paper only): launches today, result, last buy. */
function SniperTile({ s }) {
  const lb = s.last_buy;
  const sym = lb ? (lb.symbol || `${lb.mint.slice(0, 4)}…`) : null;
  return (
    <Tile label={<a href="#/sniper" className="linkish">Launch-Sniper Papier</a>} value={solSigned(s.pnl_sol, 2)} cls={sign(s.pnl_sol)}
      title={s.service_up === false ? "Der Dienst sniper meldet sich nicht" : undefined}
      sub={<>{n(s.launches_today)} Launches heute · {n(s.entered_today)} gekauft
        {sym ? <> · zuletzt {sym} {ago(lb.opened_ts)}</> : " · noch kein Kauf"}
        {s.service_up === false && <> · <span className="down">Dienst steht</span></>}</>} />
  );
}

const BOARD_STATE = { ok: ["läuft", "good"], langsam: ["verzögert", "warn"], steht: ["steht", "bad"], neu: ["noch keine Daten", "idle"] };

/** Situation board: every service in one line with its state, headline number and a link to its page. */
function Board({ b }) {
  if (!b?.rows?.length) return null;
  return (
    <div className="col" style={{ gap: 10 }}>
      {b.problems?.length > 0 && <Callout kind="bad" icon="alert">{b.problems.join(" · ")} · <a href="#/system">Zustand ansehen</a></Callout>}
      <div className="board" role="list" aria-label="Lagebild der Dienste">
        {b.rows.map((r) => {
          const [l, c] = BOARD_STATE[r.state] || [r.state, "idle"];
          return (
            <a key={r.name} href={`#${r.page}`} className="board-i" role="listitem" title={`${r.label}: ${l}${r.last_ts ? `, letzte Daten ${ago(r.last_ts)}` : ""}`}>
              <div className="board-h"><span className={`dot ${c}`} aria-hidden="true" /><span className="board-l">{r.label}</span>
                <span className={`board-s ${c}`}>{l}</span></div>
              <div className="board-v">{r.headline}</div>
              <div className="board-d">{r.detail || (r.last_ts ? `letzte Daten ${ago(r.last_ts)}` : "\u00a0")}</div>
            </a>
          );
        })}
      </div>
    </div>
  );
}
