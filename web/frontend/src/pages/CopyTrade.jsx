import { useState } from "react";
import { EChart, baseOption, catAxis, timeAxis, tipRow, valueAxis } from "../chart.jsx";
import { EXIT_LABEL, MODE_LABEL, STRAT_LABEL, act, ago, dt, dur, go, mc, n, pct, sol, solSigned, usePoll } from "../lib.js";
import { CoinLink, HistBars, PnlCurve, StatusBadge, WalletLink } from "../parts.jsx";
import { Callout, Card, Delta, Empty, Modal, Seg, Table, Tabs, Tier, Tile } from "../ui.jsx";
import Learn from "./Learn.jsx";
import { LoadBar } from "./System.jsx";
import { PosRisk, RiskBadge } from "../safety.jsx";

const ver = (r) => (r.param_version ? <span className="badge" title={r.use_inflow_exit ? "mit Verkauf am Ansturm-Ende" : undefined}>v{r.param_version}</span> : <span className="dim">Preset</span>);

function SellButton({ p }) {
  const [ask, setAsk] = useState(false);
  return (
    <>
      <button className={`btn sm ${p.exit_hint ? "primary" : ""}`} onClick={(e) => { e.stopPropagation(); setAsk(true); }}>Verkaufen</button>
      {ask && (
        <Modal title={`${p.symbol} verkaufen?`} onClose={() => setAsk(false)} actions={<>
          <button className="btn" onClick={() => setAsk(false)}>Abbrechen</button>
          <button className={`btn ${p.mode === "live" ? "danger" : "primary"}`} onClick={() => act(`/api/positions/${p.id}/sell`).finally(() => setAsk(false))}>
            {p.mode === "live" ? "Echt verkaufen" : p.mode === "dry" ? "Verkaufen (Dry-Run)" : "Verkaufen (Papier)"}
          </button>
        </>}>
          <div className="t2">Position #{p.id}: {sol(p.size_sol)} in {p.symbol}, aktuell <Delta v={p.unrealized_pct} />. Der Bot verkauft alles in wenigen Sekunden.</div>
        </Modal>
      )}
    </>
  );
}

function PnlPerTrade({ closed }) {
  const rows = [...closed].sort((a, b) => a.closed_at - b.closed_at).slice(-150);
  if (!rows.length) return <Empty>Noch keine abgeschlossenen Trades</Empty>;
  return (
    <EChart height={200} deps={[rows]} ariaLabel="Ergebnis je Trade" onClick={(p) => rows[p.dataIndex] && go(`/coin/${rows[p.dataIndex].mint}`)}
      build={(c) => baseOption(c, {
        tooltip: {
          ...baseOption(c).tooltip, axisPointer: { type: "shadow" },
          formatter: (ps) => { const r = rows[ps[0].dataIndex]; return `<b>${r.symbol}</b> · ${dt(r.closed_at)}${tipRow(ps[0].color, "Ergebnis", `${n(r.pnl_pct * 100, 1)} % · ${solSigned(r.pnl_sol)}`)}${tipRow("transparent", "Ausstieg", EXIT_LABEL[r.exit_reason] || r.exit_reason)}${tipRow("transparent", "Modus", STRAT_LABEL[r.strategy] || r.strategy || "")}`; },
        },
        xAxis: catAxis(c, rows.map((r) => r.id), { axisLabel: { show: false } }),
        yAxis: valueAxis(c, (v) => `${Math.round(v * 100)} %`),
        series: [{ type: "bar", barMaxWidth: 10, name: "Ergebnis", data: rows.map((r) => ({ value: r.pnl_pct, itemStyle: { color: r.pnl_pct >= 0 ? c.good : c.bad, borderRadius: r.pnl_pct >= 0 ? [3, 3, 0, 0] : [0, 0, 3, 3] } })) }],
      })} />
  );
}

function Backtest() {
  const [strategy, setStrategy] = useState("whale");
  const { data, loading, error } = usePoll(`/api/copytrade/backtest?strategy=${strategy}`, 0);
  return (
    <Card title="Backtest" sub="Rückblick ohne Blick in die Zukunft: was der Bot mit den aktuellen Regeln verdient hätte"
      actions={<Seg value={strategy} onChange={setStrategy} options={[["whale", "Modus 1 · Wale"], ["signal", "Modus 2 · Signale"]]} />}>
      {error ? <Callout kind="warn" icon="alert">{error}</Callout> : !data ? <div className="empty">Rechne …</div> : !data.available ? <Empty>Für diesen Modus gibt es noch keinen Backtest</Empty> : !data.n ? <Empty>Noch zu wenige ausgewertete Käufe</Empty> : (
        <div className={loading ? "loading" : ""}>
          <div className="row" style={{ gap: 18, marginBottom: 8 }}>
            <div className="kv"><span className="k">Trades</span><span className="v">{n(data.n)}</span></div>
            <div className="kv"><span className="k">Gewinner</span><span className="v">{pct(data.win_rate)}</span></div>
            <div className="kv"><span className="k">Ergebnis</span><span className={`v ${data.pnl_sol >= 0 ? "up" : "down"}`}>{solSigned(data.pnl_sol, 2)}</span></div>
          </div>
          <PnlCurve points={data.rows.filter((r) => r.ts).map((r, i, a) => ({ ts: r.ts, pnl_sol: r.equity_sol ?? a.slice(0, i + 1).reduce((s, x) => s + (x.pnl_sol || 0), 0) }))} height={200} label="Backtest kumuliert" />
        </div>
      )}
    </Card>
  );
}

const ACCT = { paper: "Papier streng", explore: "Erkundung", dry: "Dry-Run", live: "Echtgeld" };

/** How far each strategy is on the way to real money: trades the learner never saw (after its active parameters)
 *  against the ~100 needed, net after costs and hit rate per account, and the live gate on the strict book. */
function Learning() {
  const { data: d } = usePoll("/api/learning", 30000);
  if (!d?.available || !d.rows?.length) return null;
  return (
    <Card title="Lernfortschritt zum Echtgeld" sub={`Zählt nur Trades nach den aktuell gelernten Einstellungen (out of sample). Ab etwa ${d.target} solcher Trades ist ein Ergebnis belastbar. Netto = nach Gebühren und Slippage.`} flush>
      <Table rows={d.rows} rowKey={(r) => `${r.strategy}-${r.account}`} cols={[
        { k: "strategy", l: "Modus", fmt: (r) => STRAT_LABEL[r.strategy] || r.strategy },
        { k: "account", l: "Konto", fmt: (r) => (r.account === "paper" ? <b>{ACCT.paper}</b> : ACCT[r.account] || r.account) },
        { k: "progress", l: `Out of sample (Ziel ${d.target})`, sort: (r) => r.oos_trades,
          fmt: (r) => <span className="col" style={{ gap: 2, minWidth: 140 }}><LoadBar v={r.progress} /><span className="dim" style={{ fontSize: 11 }}>{n(r.oos_trades)} / {d.target}{r.since_params ? ` · seit ${dt(r.since_params)}` : " · noch keine gelernte Version"}</span></span> },
        { k: "oos_net_sol", l: "Netto out of sample", r: true, fmt: (r) => <span className={(r.oos_net_sol || 0) >= 0 ? "up" : "down"}>{solSigned(r.oos_net_sol, 3)}</span> },
        { k: "oos_win_rate", l: "Treffer oos", r: true, fmt: (r) => (r.oos_win_rate != null ? pct(r.oos_win_rate) : "–") },
        { k: "net_sol", l: "Netto gesamt", r: true, fmt: (r) => <span className={(r.net_sol || 0) >= 0 ? "up" : "down"}>{solSigned(r.net_sol, 3)}</span> },
        { k: "trades", l: "Trades gesamt", r: true },
        { k: "gate", l: "Go-Live-Prüfung", sort: (r) => (r.gate ? (r.gate.ok ? 2 : 1) : 0),
          fmt: (r) => (r.gate ? <span className={`badge ${r.gate.ok ? "good" : ""}`} title={`Letzte ${n(r.gate.days)} Tage: ${r.gate.n} Trades (min. ${r.gate.min_trades}), netto ${solSigned(r.gate.net_sol, 3)}, Profit-Faktor ${r.gate.profit_factor != null ? n(r.gate.profit_factor, 2) : "∞"} (min. ${n(r.gate.min_pf, 1)}), beide Hälften im Plus: ${r.gate.halves_positive ? "ja" : "nein"}`}>
            {r.gate.ok ? "bestanden" : r.gate.n < r.gate.min_trades ? `${r.gate.n}/${r.gate.min_trades} Trades` : r.gate.net_sol <= 0 ? "netto im Minus"
              : r.gate.profit_factor != null && r.gate.profit_factor < r.gate.min_pf ? "Profit-Faktor zu niedrig" : "eine Hälfte im Minus"}</span> : <span className="dim">–</span>) },
      ]} />
    </Card>
  );
}

export default function CopyTrade() {
  const [mode, setMode] = useState("paper");
  const [strategy, setStrategy] = useState("");
  const [tab, setTab] = useState("open");
  const { data: d, error } = usePoll(`/api/copytrade?mode=${mode}${strategy ? `&strategy=${strategy}` : ""}`, 6000);
  if (!d) return error ? <Callout kind="bad" icon="alert">{error}</Callout> : <div className="empty">Lade …</div>;
  if (!d.available) return <Callout icon="info">Das Copy-Trading hat noch keine Daten in der Datenbank. Es startet mit der App (python mayham.py start).</Callout>;
  const s = d.summary || {};
  const eq = d.equity?.length ? d.equity[d.equity.length - 1].equity_sol : null;
  const ctl = d.control || {};
  return (
    <div className="col" style={{ gap: 16 }}>
      <div className="row" style={{ gap: 12 }}>
        <Seg value={mode} onChange={setMode} options={[["paper", "Papier streng"], ["explore", "Erkundung"], ["dry", "Dry-Run"], ["live", "Echtgeld-Konto"]]} />
        <Seg value={strategy} onChange={setStrategy} options={[["", "Alle Modi"], ["whale", "Modus 1 · Wal-Pumps"], ["signal", "Modus 2 · Signale"], ["manual", "Manuell"]]} />
        <span className="sp" />
        <span className="dim">Modus 1: {MODE_LABEL[ctl.modes?.whale] || "–"} · Modus 2: {MODE_LABEL[ctl.modes?.signal] || "–"}{ctl.paused ? " · Käufe pausiert" : ""}</span>
        <a href="#/control" className="btn sm">Steuerung</a>
      </div>
      {mode === "paper" && <Callout icon="info">Strenges Papierkonto: genau die Regeln, die mit Echtgeld gelten würden. Dieses Ergebnis entscheidet über den Schritt zu Echtgeld.</Callout>}
      {mode === "explore" && <Callout icon="info">Erkundungskonto (Start 5 SOL): kauft bewusst breiter, auch Ultra- und Smart-Wale, die die strengen Regeln auslassen, mit kleinen Beträgen. Es dient zum Lernen, nicht als Maßstab für Echtgeld.</Callout>}
      {mode === "dry" && <Callout icon="info">Dry-Run-Konto: Orders, die der Echtgeld-Pfad gebaut, signiert und simuliert, aber nie gesendet hat. Gebucht mit Quote-Preisen wie auf Papier.</Callout>}
      {mode === "live" && !ctl.live_ready && <Callout icon="info">Echtgeld ist aus. Der Hauptschalter steht in der .env (LIVE_TRADING=true und ein Schlüssel), danach je Modus unter Steuerung.</Callout>}

      <Learning />

      <div className="tiles">
        <Tile label="Kontostand" value={eq != null ? sol(eq) : d.balance_sol != null ? sol(d.balance_sol) : "–"} sub={`${sol(s.open_value_sol, 2)} in offenen Positionen (Verkaufswert)`} />
        <Tile label="Gewinn / Verlust (realisiert + offen)" value={solSigned(s.total_sol, 2)} cls={(s.total_sol || 0) >= 0 ? "up" : "down"}
          title="Abgeschlossene Trades plus offene Positionen zum heutigen Verkaufswert (Mittelkurs minus 3 % Ausstiegskosten)"
          sub={`realisiert ${solSigned(s.realized_sol, 2)} · offen ${solSigned(s.unrealized_sol, 2)}${s.trades ? ` · Ø ${n((s.avg_pnl_pct || 0) * 100, 1)} % pro Trade` : ""}${s.unpriced_open ? ` · ${s.unpriced_open} ohne Kurs` : ""}`} />
        <Tile label="Gewinner-Quote" value={s.trades ? pct(s.win_rate) : "–"} sub={`${n(s.trades)} abgeschlossene Trades`} />
        <Tile label="Profit-Faktor" value={s.profit_factor != null ? n(s.profit_factor, 2) : s.trades ? "∞" : "–"} sub={s.trades ? `Ø Gewinn ${pct(s.avg_win_pct)} · Ø Verlust ${pct(s.avg_loss_pct)}` : "Gewinne ÷ Verluste"} />
        <Tile label="Max. Drawdown" value={s.max_drawdown_sol != null ? sol(s.max_drawdown_sol, 2) : "–"} sub={`bester ${pct(s.best_pct)} · schlechtester ${pct(s.worst_pct)}`} />
        <Tile label="Timing" value={s.avg_entry_delay_s != null ? `${n(s.avg_entry_delay_s, 0)} s` : "–"} sub={`nach dem Wal · Ø Haltedauer ${dur(s.avg_hold_min)}`} />
      </div>

      <div className="grid g-wide-left">
        <Card title="Kontostand über Zeit" sub="Guthaben + Wert offener Positionen">
          {d.equity?.length > 1 ? (
            <EChart height={240} deps={[d.equity]} ariaLabel="Equity-Kurve"
              build={(c) => baseOption(c, {
                tooltip: { ...baseOption(c).tooltip, valueFormatter: (v) => `${n(v, 3)} SOL` },
                xAxis: timeAxis(c), yAxis: valueAxis(c, (v) => n(v, 1), { scale: true }),
                dataZoom: [{ type: "inside" }],
                series: [{ name: "Kontostand", type: "line", showSymbol: false, lineStyle: { width: 2, color: c.s[0] }, areaStyle: { color: c.s[0], opacity: 0.08 }, data: d.equity.map((p) => [p.ts * 1000, p.equity_sol]) }],
              })} />
          ) : <PnlCurve points={d.pnl_curve} height={240} />}
        </Card>
        <Card title="Modus-Vergleich" sub="Welche Strategie verdient mehr?" flush>
          <Table rows={Object.entries(d.by_strategy || {}).map(([k, v]) => ({ k, ...v }))} rowKey={(r) => r.k} empty="Noch keine Trades" cols={[
            { k: "k", l: "Modus", fmt: (r) => (STRAT_LABEL[r.k] || r.k).split(" · ").pop() },
            { k: "trades", l: "Trades", r: true },
            { k: "win_rate", l: "Gewinner", r: true, fmt: (r) => pct(r.win_rate) },
            { k: "avg_pnl_pct", l: "Ø Trade", r: true, fmt: (r) => <Delta v={r.avg_pnl_pct} arrow={false} /> },
            { k: "pnl_sol", l: "Ergebnis", r: true, fmt: (r) => <span className={(r.pnl_sol || 0) >= 0 ? "up" : "down"}>{solSigned(r.pnl_sol, 2)}</span> },
            { k: "profit_factor", l: "PF", r: true, fmt: (r) => (r.profit_factor != null ? n(r.profit_factor, 2) : "–") },
          ]} />
          <div style={{ padding: "8px 14px 6px" }}>
            <div className="b" style={{ marginBottom: 6 }}>Ausstiegsgründe</div>
            <Table rows={d.exit_reasons} rowKey={(r) => r.reason} empty="–" cols={[
              { k: "reason", l: "Grund", fmt: (r) => EXIT_LABEL[r.reason] || r.reason },
              { k: "n", l: "Anzahl", r: true },
              { k: "wins", l: "Gewinner", r: true, fmt: (r) => pct(r.wins / r.n) },
              { k: "pnl_sol", l: "SOL", r: true, fmt: (r) => <span className={r.pnl_sol >= 0 ? "up" : "down"}>{solSigned(r.pnl_sol, 2)}</span> },
            ]} />
          </div>
        </Card>
      </div>

      <div className="grid g3">
        <Card title="Ergebnis je Trade" sub="Letzte 150 Trades, Klick öffnet den Coin"><PnlPerTrade closed={d.closed} /></Card>
        <Card title="Verteilung der Ergebnisse" sub="Anzahl Trades je 10-%-Stufe">
          <HistBars data={d.pnl_hist} fmtX={(b) => `${b > 0 ? "+" : ""}${b}`} label="Trades" height={200} color={(r, c) => (r.bucket >= 0 ? c.good : c.bad)} />
        </Card>
        <Card title="Ergebnis nach Tageszeit" sub="Summe SOL je Stunde (UTC) des Kaufs">
          <HistBars data={d.by_hour.map((h) => ({ bucket: h.hour, n: h.pnl_sol, cnt: h.n }))} label="SOL" height={200} color={(r, c) => (r.n >= 0 ? c.good : c.bad)} />
        </Card>
      </div>

      <Card flush>
        <div style={{ padding: "4px 10px 0" }}>
          <Tabs value={tab} onChange={setTab} options={[["open", `Offene Positionen (${d.open.length})`], ["closed", "Abgeschlossen"], ["alerts", "Einstiege und Signale"], ["wallets", "Ergebnis je Wal"], ["events", "Protokoll"]]} />
        </div>
        {tab === "open" && (
          <Table rows={d.open} rowKey={(r) => r.id} empty="Keine offenen Positionen" cols={[
            { k: "symbol", l: "Coin", fmt: (r) => <>{<CoinLink mint={r.mint} symbol={r.symbol} />}{r.status !== "open" && <> <StatusBadge s={r.status} /></>}</> },
            { k: "strategy", l: "Modus", fmt: (r) => STRAT_LABEL[r.strategy] || r.strategy },
            { k: "managed", l: "Wer verkauft", fmt: (r) => (r.managed === "semi" ? <span className="badge warn">du</span> : <span className="badge">Bot</span>) },
            { k: "opened_at", l: "Seit", fmt: (r) => ago(r.opened_at) },
            { k: "entry_mcap_usd", l: "Einstieg MC", r: true, fmt: (r) => mc(r.entry_mcap_usd) },
            { k: "last_mcap_usd", l: "MC jetzt", r: true, fmt: (r) => mc(r.last_mcap_usd) },
            { k: "size_sol", l: "Größe", r: true, fmt: (r) => sol(r.size_sol) },
            { k: "unrealized_pct", l: "Ergebnis", r: true, fmt: (r) => <Delta v={r.unrealized_pct} /> },
            { k: "take_profit", l: "Ziel / Stop", r: true, fmt: (r) => `+${pct(r.take_profit)} / −${pct(r.stop_loss)}` },
            { k: "param_version", l: "Version", title: "Welche gelernte Parameter-Version die Ausstiege festlegt", fmt: ver },
            { k: "safety_score", l: "Rug-Risiko", title: "Rug-Risiko beim Kauf (0–100)", fmt: (r) => <PosRisk r={r} /> },
            { k: "exit_hint", l: "Hinweis", fmt: (r) => (r.exit_hint ? <span className="badge good">{EXIT_LABEL[r.exit_hint] || r.exit_hint}: verkaufen</span> : "–") },
            { k: "sell", l: "", sort: false, fmt: (r) => (r.status === "open" ? <SellButton p={r} /> : null) },
          ]} />
        )}
        {tab === "open" && d.failed?.length > 0 && (
          <>
            <div className="card-h"><div><div className="card-t">Fehlgeschlagene Käufe ({d.failed.length})</div>
              <div className="card-s">Echtgeld-Käufe, bei denen nichts gekauft wurde. Kein Ergebnis, nur zur Nachverfolgung.</div></div></div>
            <Table rows={d.failed} rowKey={(r) => r.id} cols={[
              { k: "opened_at", l: "Zeit", fmt: (r) => dt(r.opened_at) },
              { k: "symbol", l: "Coin", fmt: (r) => <CoinLink mint={r.mint} symbol={r.symbol} /> },
              { k: "strategy", l: "Modus", fmt: (r) => STRAT_LABEL[r.strategy] || r.strategy },
              { k: "status", l: "Status", fmt: (r) => <StatusBadge s={r.status} /> },
              { k: "exit_reason", l: "Grund", fmt: (r) => <span className="t2">{r.exit_reason || "–"}</span> },
            ]} />
          </>
        )}
        {tab === "closed" && (
          <Table rows={d.closed} rowKey={(r) => r.id} tall max={500} cols={[
            { k: "closed_at", l: "Verkauft", fmt: (r) => dt(r.closed_at) },
            { k: "symbol", l: "Coin", fmt: (r) => <CoinLink mint={r.mint} symbol={r.symbol} /> },
            { k: "strategy", l: "Modus", fmt: (r) => STRAT_LABEL[r.strategy] || r.strategy },
            { k: "wallet", l: "Wal", fmt: (r) => (r.wallet ? <WalletLink a={r.wallet} /> : "–") },
            { k: "entry_delay_s", l: "Verzögerung", r: true, fmt: (r) => (r.entry_delay_s != null ? `${r.entry_delay_s} s` : "–") },
            { k: "entry_mcap_usd", l: "Einstieg MC", r: true, fmt: (r) => mc(r.entry_mcap_usd) },
            { k: "hold_min", l: "Haltedauer", r: true, fmt: (r) => dur(r.hold_min) },
            { k: "exit_reason", l: "Ausstieg", fmt: (r) => EXIT_LABEL[r.exit_reason] || r.exit_reason },
            { k: "param_version", l: "Version", title: "Welche gelernte Parameter-Version die Ausstiege festlegte", fmt: ver },
            { k: "safety_score", l: "Rug-Risiko", title: "Rug-Risiko beim Kauf (0–100)", fmt: (r) => <PosRisk r={r} /> },
            { k: "pnl_pct", l: "Ergebnis", r: true, fmt: (r) => <Delta v={r.pnl_pct} /> },
            { k: "pnl_sol", l: "SOL", r: true, fmt: (r) => <span className={r.pnl_sol >= 0 ? "up" : "down"}>{solSigned(r.pnl_sol)}</span> },
          ]} />
        )}
        {tab === "alerts" && (
          <Table rows={d.alerts} rowKey={(r) => r.id} tall empty="Noch keine Einstiege" cols={[
            { k: "ts", l: "Zeit", fmt: (r) => dt(r.ts) },
            { k: "account", l: "Konto", fmt: (r) => (r.account === "explore" ? <span className="badge warn">Erkundung</span> : r.account === "live" ? <span className="badge live">Echtgeld</span> : <span className="dim">streng</span>) },
            { k: "action", l: "", fmt: (r) => (r.action === "bought" ? <span className="badge good">gekauft</span> : r.action === "signal" ? <span className="badge accent">Kaufsignal</span> : <span className="badge">{r.action}</span>) },
            { k: "symbol", l: "Coin", fmt: (r) => <CoinLink mint={r.mint} symbol={r.symbol} /> },
            { k: "strategy", l: "Modus", fmt: (r) => STRAT_LABEL[r.strategy] || r.strategy },
            { k: "mcap_usd", l: "MC", r: true, fmt: (r) => mc(r.mcap_usd) },
            { k: "score", l: "Score", r: true, fmt: (r) => n(r.score, 0) },
            { k: "safety", l: "Rug-Risiko", sort: (r) => r.safety?.score ?? -1, fmt: (r) => <RiskBadge s={r.safety} /> },
            { k: "reason", l: "Grund", fmt: (r) => <span className="t2">{r.reason}</span> },
          ]} />
        )}
        {tab === "wallets" && (
          <Table rows={d.per_wallet} rowKey={(r) => r.wallet} tall empty="Noch keine kopierten Wale" cols={[
            { k: "wallet", l: "Wal", fmt: (r) => <WalletLink a={r.wallet} /> },
            { k: "tier", l: "Tier", fmt: (r) => <Tier t={r.tier} /> },
            { k: "n", l: "Kopien", r: true },
            { k: "win_rate", l: "Gewinner", r: true, fmt: (r) => pct(r.win_rate) },
            { k: "avg_pct", l: "Ø Trade", r: true, fmt: (r) => <Delta v={r.avg_pct} arrow={false} /> },
            { k: "pnl_sol", l: "Ergebnis", r: true, fmt: (r) => <span className={r.pnl_sol >= 0 ? "up" : "down"}>{solSigned(r.pnl_sol)}</span> },
          ]} />
        )}
        {tab === "events" && (
          <Table rows={d.events} rowKey={(r, i) => i} tall cols={[
            { k: "ts", l: "Zeit", fmt: (r) => dt(r.ts) },
            { k: "kind", l: "Art", fmt: (r) => <span className={`badge ${r.level === "error" ? "bad" : r.kind === "buy" ? "good" : r.kind === "hint" ? "warn" : ""}`}>{r.kind}</span> },
            { k: "message", l: "Meldung", fmt: (r) => <span className="t2" style={{ whiteSpace: "normal" }}>{r.message}</span> },
          ]} />
        )}
      </Card>

      <Learn />

      <Card title="Wal-Ranking: wer löst die besten Copy-Pumps aus?" sub="Gemessen an Minutenkerzen nach jedem Wal-Kauf · Vorsprung = Minuten bis der Copy-Ansturm kommt · Für uns drin = was wir mit unserer Verzögerung nach Kosten verdient hätten" flush>
        <Table rows={d.whale_rank} rowKey={(r) => r.address} onRow={(r) => go(`/wallet/${r.address}`)} tall empty="Noch keine bewerteten Wale" cols={[
          { k: "address", l: "Wal", fmt: (r) => <WalletLink a={r.address} label={r.label || undefined} /> },
          { k: "tier", l: "Tier", fmt: (r) => <Tier t={r.tier} /> },
          { k: "samples", l: "Käufe", r: true },
          { k: "hit_rate", l: "Pump-Quote", r: true, fmt: (r) => pct(r.hit_rate) },
          { k: "median_first_pump", l: "Erster Pump", r: true, fmt: (r) => pct(r.median_first_pump) },
          { k: "median_pump_min", l: "bis Gipfel", r: true, fmt: (r) => `${n(r.median_pump_min, 0)} min` },
          { k: "median_lead_min", l: "Vorsprung", r: true, fmt: (r) => (r.median_lead_min != null ? `${n(r.median_lead_min, 1)} min` : "–") },
          { k: "median_achievable", l: "Für uns drin", r: true, fmt: (r) => <Delta v={r.median_achievable} d={0} arrow={false} /> },
          { k: "expected_value", l: "Erwartung", r: true, fmt: (r) => <Delta v={r.expected_value} d={1} arrow={false} /> },
          { k: "take_profit", l: "Gelerntes Ziel", r: true, fmt: (r) => `+${pct(r.take_profit)}` },
          { k: "score", l: "Score", r: true, fmt: (r) => n((r.score || 0) * 100, 1) },
          { k: "active", l: "", fmt: (r) => (r.active === 0 ? <span className="badge">abgeschaltet</span> : "") },
        ]} />
      </Card>

      <div className="grid g4">
        <Card title="Wie groß ist der erste Pump?" sub={`${d.timing.pumped} von ${d.timing.n} Wal-Käufen pumpten`}><HistBars data={d.timing.first_pump_pct} fmtX={(b) => b.replace("–10000", "+") + " %"} label="Käufe" height={180} /></Card>
        <Card title="Minuten bis zum Gipfel" sub="Wann verkaufen?"><HistBars data={d.timing.minutes_to_peak} fmtX={(b) => b.replace("–10000", "+")} label="Käufe" height={180} /></Card>
        <Card title="Vorsprung vor dem Copy-Ansturm" sub="Minuten zwischen Wal-Kauf und Ansturm"><HistBars data={d.timing.lead_minutes} fmtX={(b) => b.replace("–10000", "+")} label="Käufe" height={180} /></Card>
        <Card title="Was für uns drin war" sub="Ergebnis mit unserer Verzögerung, %"><HistBars data={d.timing.achievable_pct} fmtX={(b) => b.replace("–10000", "+")} label="Käufe" height={180} /></Card>
      </div>

      <Backtest />
    </div>
  );
}
