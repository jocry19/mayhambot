import { useState } from "react";
import { LoadBar } from "./System.jsx";
import { EChart, baseOption, timeAxis, tipRow, valueAxis } from "../chart.jsx";
import { ago, dt, dur, n, pct, pctSigned, sign, sol, solSigned, usd, usePoll } from "../lib.js";
import { Callout, Card, Delta, Empty, Icon, Seg, Table, Tabs, Tile } from "../ui.jsx";

const GROUPS = [["entered", "Gekauft"], ["skipped_deep", "Tief geprüft, übersprungen"], ["skipped_stage1", "Sofort übersprungen"], ["all", "Alle"]];

const shortMint = (m) => `${m.slice(0, 4)}…${m.slice(-4)}`;
const CoinCell = ({ r }) => (
  <span>
    <a href={`https://pump.fun/coin/${r.mint}`} target="_blank" rel="noreferrer" onClick={(e) => e.stopPropagation()} className="b">{r.symbol || shortMint(r.mint)}</a>
    {r.symbol && <span className="dim mono" style={{ marginLeft: 6, fontSize: 11 }}>{shortMint(r.mint)}</span>}
  </span>
);

/** Deep check incomplete: some lookups failed (features.errors), so the score rests on less data. */
function CheckErrors({ e }) {
  if (!e?.length) return null;
  const title = `Prüfung unvollständig, ${e.length} ${e.length === 1 ? "Abfrage" : "Abfragen"} fehlgeschlagen:\n${e.map((x) => `• ${x}`).join("\n")}`;
  return <span className="warn-ink check-err" title={title} aria-label={title}><Icon name="alert" size={14} /></span>;
}

function Decision({ r }) {
  const err = <CheckErrors e={r.check_errors} />;
  if (r.decision === "enter") return <span className="row" style={{ gap: 6 }}><span className="badge good">gekauft</span>{err}</span>;
  if (r.decision === "skip") return <span className="row" style={{ gap: 6 }}><span className="t2" title={r.skip_reason}>{r.skip_label || r.skip_reason}</span>{err}</span>;
  return <span className="row" style={{ gap: 6 }}><span className="badge">{r.decision || "offen"}</span>{err}</span>;
}

function Bundle({ r }) {
  const p = r.bundle_pct, w = r.bundle_wallets, u = r.unseen_pct;
  if (p == null && u == null) return <span className="dim">–</span>;
  const cls = r.bundle_flag ? "bad" : (p || 0) >= 5 ? "warn" : "";
  return (
    <span title={`Im Erstellungs-Slot gekauft: ${n(p, 1)} % von ${n(w)} Wallets · Curve-Schätzung ungesehen gekauft: ${n(u, 1)} %`}>
      <span className={`badge ${cls}`}>{r.bundle_flag ? "Bundle " : ""}{p != null ? `${n(p, 1)} %` : `~${n(u, 0)} %`}</span>
      {w ? <span className="dim" style={{ marginLeft: 4 }}>{w} W.</span> : null}
    </span>
  );
}

function LaunchDetail({ r }) {
  return (
    <div className="grid g2" style={{ gap: 16 }}>
      <div>
        <div className="b" style={{ marginBottom: 6 }}>Risiken</div>
        {r.reasons?.length ? (
          <ul className="risk-list">{r.reasons.map((x, i) => (
            <li key={i}><span className={`badge ${x.risk >= 60 ? "bad" : x.risk >= 30 ? "warn" : ""}`}>{n(x.risk)}</span><span>{x.text}</span></li>
          ))}</ul>
        ) : <div className="dim">Keine gefunden{r.stage === 1 ? " (nur Datenstrom geprüft)" : ""}.</div>}
        {r.check_errors?.length > 0 && (
          <Callout kind="warn" icon="alert">Prüfung unvollständig: {r.check_errors.join(" · ")}</Callout>
        )}
      </div>
      <div>
        <div className="b" style={{ marginBottom: 6 }}>Was dafür sprach</div>
        {r.positives?.length ? <ul className="risk-list pos">{r.positives.map((x, i) => <li key={i}><span>✓</span><span>{x}</span></li>)}</ul> : <div className="dim">–</div>}
        <div className="kvs" style={{ marginTop: 10 }}>
          <span className="t2">Käufer {n(r.n_buyers)} · Netto {n(r.net_sol, 1)} SOL · Top-10 {n(r.top10_pct, 0)} % · Sniper-Bots {n(r.fast_buyers)} · Curve {pct(r.curve_progress)}</span>
        </div>
        <div className="row" style={{ gap: 10, marginTop: 8, fontSize: 12.5 }}>
          <a href={`https://pump.fun/coin/${r.mint}`} target="_blank" rel="noreferrer">pump.fun</a>
          <a href={`https://dexscreener.com/solana/${r.mint}`} target="_blank" rel="noreferrer">DexScreener</a>
          <a href={`https://solscan.io/token/${r.mint}`} target="_blank" rel="noreferrer">Solscan</a>
        </div>
      </div>
    </div>
  );
}

function BalanceChart({ curve, start }) {
  if (!curve || curve.length < 2) return <Empty>Noch keine abgeschlossenen Papier-Trades</Empty>;
  return (
    <EChart height={220} deps={[curve]} ariaLabel="Papier-Bilanz des Snipers"
      build={(c) => baseOption(c, {
        tooltip: {
          ...baseOption(c).tooltip,
          formatter: (ps) => {
            const p = curve[ps[0].dataIndex];
            return `<b>${dt(p.ts)}</b>${tipRow(c.s[0], "Bilanz", sol(p.balance_sol, 3))}${p.symbol ? tipRow("transparent", p.symbol, solSigned(p.pnl_sol, 3)) : ""}`;
          },
        },
        xAxis: timeAxis(c), yAxis: valueAxis(c, (v) => n(v, 2), { scale: true }),
        series: [{
          type: "line", step: "end", showSymbol: false, lineStyle: { width: 2, color: c.s[0] }, itemStyle: { color: c.s[0] },
          data: curve.map((p) => [p.ts * 1000, p.balance_sol]),
          markLine: { silent: true, symbol: "none", lineStyle: { color: c.muted, type: "dashed", width: 1 },
            label: { color: c.muted, formatter: "Start", position: "insideEndTop" }, data: [{ yAxis: start }] },
        }],
      })} />
  );
}

const BUDGET = { rpc: "Solana-RPC", api: "pump.fun-API", deep: "Tiefe Prüfungen" };

function streamState(s) {
  if (s.source === "heartbeat") return s.connected ? ["verbunden", "up"] : ["Dienst läuft, Websocket getrennt", "down"];
  if (s.source === "stale") return ["Dienst steht", "down"];
  return ["Dienst läuft nicht", "down"];
}

function Stream({ s }) {
  const [label, cls] = streamState(s);
  const fromCounters = s.rate_source === "counters";
  const rateSub = fromCounters ? `aus den Zählern des Dienstes, gemessen über ${dur(s.rate_span_s / 60)}` : "aus den gespeicherten Launches der letzten Stunde";
  return (
    <div className="tiles wrap-sub">
      <Tile label="Datenstrom (PumpPortal)" value={<span className={cls}>{label}</span>}
        sub={s.source === "heartbeat" ? `Meldung ${ago(s.heartbeat_ts)} · ${n(s.subscribed)} Coins im Trade-Abo · Warteschlange ${n(s.queue)}`
          : s.heartbeat_ts ? `letzte Meldung ${ago(s.heartbeat_ts)} (meldet sonst alle 10 s)` : "noch keine Meldung vom Dienst sniper"} />
      <TradeSource s={s} />
      <Tile label="Launches pro Stunde" value={n(s.launches_1h)} sub={rateSub} />
      <Tile label="Tief geprüft pro Stunde" value={n(s.deep_1h)}
        sub={s.deep_per_hour_cap ? `höchstens ${n(s.deep_per_hour_cap)} pro Stunde` : "–"} />
      <Tile label="Käufe pro Stunde (Papier)" value={n(s.entered_1h)}
        sub={s.source === "heartbeat" ? `${n(s.open_positions)} offen · pump.fun-API ${s.pump_api_ok ? "erreichbar" : "nicht erreichbar"}` : ""} />
    </div>
  );
}

const TRADE_SRC = { rpc: "Solana-RPC (Logs)", pumpportal: "PumpPortal" };

/** Where per-coin trades come from; with the RPC source a second websocket carries up to 150 log subscriptions. */
function TradeSource({ s }) {
  if (s.source !== "heartbeat" || !s.trade_source) {
    return <Tile label="Trade-Quelle" value={<span className="dim">–</span>} sub={s.source === "heartbeat" ? "der Dienst meldet keine Trade-Quelle" : "kommt aus der Meldung des Dienstes"} />;
  }
  const rpc = s.trade_source === "rpc";
  const max = s.rpc_ws_max_subs || 150;
  const full = rpc && (s.rpc_ws_subs || 0) >= max;
  return (
    <Tile label="Trade-Quelle" value={<span style={{ fontSize: 17 }}>{TRADE_SRC[s.trade_source] || s.trade_source}</span>}
      sub={rpc
        ? <>2. Websocket <span className={s.rpc_ws_connected ? "up" : "down"}>{s.rpc_ws_connected ? "verbunden" : "getrennt"}</span> · <span className={full ? "warn-ink" : ""}>{n(s.rpc_ws_subs)} von {n(max)} Abos</span></>
        : "Trades kommen mit über den PumpPortal-Websocket"} />
  );
}

function BudgetTable({ s }) {
  const b = s.budget || {};
  const rows = Object.keys(b).map((k) => ({ k, ...b[k] }));
  if (s.source !== "heartbeat") return <div className="empty">Budgetauslastung kommt aus der Meldung des Dienstes, die gerade fehlt.</div>;
  return (
    <Table rows={rows} rowKey={(r) => r.k} empty="Der Dienst meldet kein Budget" cols={[
      { k: "k", l: "Budget", fmt: (r) => BUDGET[r.k] || r.k },
      { k: "used_last_min", l: "Letzte Minute", r: true, fmt: (r) => n(r.used_last_min) },
      { k: "per_min", l: "Erlaubt je Minute", r: true, fmt: (r) => n(r.per_min) },
      { k: "load", l: "Auslastung", fmt: (r) => <LoadBar v={r.load} /> },
      { k: "total", l: "Seit Dienststart", r: true, fmt: (r) => n(r.total) },
    ]} />
  );
}

export default function Sniper() {
  const [hours, setHours] = useState(24);
  const [tab, setTab] = useState("open");
  const [only, setOnly] = useState("");
  const { data: d, error } = usePoll(`/api/sniper?hours=${hours}`, 8000);
  if (error) return <Callout kind="bad" icon="alert">{error}</Callout>;
  if (!d) return <div className="empty">Lade …</div>;
  if (!d.available) return <Callout icon="info">{d.note}</Callout>;
  const st = d.stats;
  const pp = st.paper || {};
  const open = d.positions.filter((p) => p.status === "open");
  const closed = d.positions.filter((p) => p.status === "closed");
  const launches = d.launches.filter((r) => (only === "enter" ? r.decision === "enter" : only === "skip" ? r.decision === "skip" : only === "deep" ? r.stage === 2 : true));
  const ft = st.filter_total || {};
  return (
    <div className="col" style={{ gap: 16 }}>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <Callout icon="info">Nur Papiermodus: der Sniper kauft nichts mit echtem Geld. Ergebnisse sind ab dem Market Cap im Entscheidungsmoment gemessen, auch für übersprungene Launches.</Callout>
        <Seg value={hours} onChange={setHours} options={[[6, "6 h"], [24, "24 h"], [168, "7 Tage"]]} />
      </div>
      <div className="tiles">
        <Tile label="Launches erkannt" value={n(st.launches)} sub={`${n(st.stage2)} tief geprüft · ${n(st.bundle_flagged)} Bundles · ${n(st.insider_flagged)} Insider`} />
        <Tile label="Gekauft (Papier)" value={n(st.entered)} sub={`Kauf ab Score ${n(d.settings.min_score)} und Risiko bis ${n(d.settings.max_risk)}`} />
        <Tile label="Papier-Bilanz" value={sol(pp.balance_sol, 3)} sub={`Start ${sol(d.paper_start_sol, 1)} · ${n(pp.n_open)} offen`} />
        <Tile label={`Ergebnis ${hours < 168 ? `${hours} h` : "7 Tage"}`} value={solSigned(pp.pnl_sol, 3)} cls={sign(pp.pnl_sol)}
          sub={pp.n_closed ? `${n(pp.n_closed)} Trades · Ø ${pctSigned(pp.avg_pnl_pct, 1)}` : "noch keine Trades"} />
        <Tile label="Gewinner-Quote" value={pp.n_closed ? pct(pp.win_rate) : "–"} sub={pp.n_closed ? `bester ${pctSigned(pp.best_pct, 0)} · schlechtester ${pctSigned(pp.worst_pct, 0)}` : ""} />
      </div>
      <Stream s={d.stream} />

      <div className="grid g-wide-left">
        <Card title="Papier-Bilanz über Zeit" sub={`Start ${sol(d.paper_start_sol, 1)} plus Ergebnis jedes abgeschlossenen Trades · ${sol(d.settings.size_sol)} je Kauf`}>
          <BalanceChart curve={d.balance_curve} start={d.paper_start_sol} />
        </Card>
        <Card title="Gekauft gegen übersprungen" sub="Median-Rendite ab Entscheidung; Treffer = Hoch ≥ +100 % in 60 min; Rug = ≤ −80 % nach 60 min" flush>
          <Table rows={GROUPS.map(([k, l]) => ({ k, l, ...(st.groups[k] || {}) }))} rowKey={(r) => r.k} cols={[
            { k: "l", l: "Gruppe", sort: false },
            { k: "n", l: "Launches", r: true, fmt: (r) => n(r.n) },
            { k: "median_ret_5m", l: "5 min", r: true, fmt: (r) => <Delta v={r.median_ret_5m} d={0} arrow={false} /> },
            { k: "median_ret_60m", l: "60 min", r: true, fmt: (r) => <Delta v={r.median_ret_60m} d={0} arrow={false} /> },
            { k: "hit_rate_2x", l: "Treffer", r: true, fmt: (r) => pct(r.hit_rate_2x) },
            { k: "rug_rate", l: "Rugs", r: true, fmt: (r) => pct(r.rug_rate) },
          ]} />
        </Card>
      </div>

      <Card title="Was bringt jeder Filter?" flush
        sub={`Übersprungene Launches nachgemessen. „Ohne Filter“ = gleicher Einsatz (${sol(ft.size_sol)}), 60 min gehalten, ohne Stop und Gebühren. Positiver Filterwert = der Filter hat Geld gespart.`}>
        <Table rows={st.filters} rowKey={(r) => r.code} initialSort={{ k: "n", d: "desc" }} empty="Noch nichts übersprungen" cols={[
          { k: "label", l: "Filter" },
          { k: "n", l: "Übersprungen", r: true, fmt: (r) => n(r.n) },
          { k: "missed_winners", l: "Verpasste Gewinner", r: true, title: "Hoch ≥ +100 % innerhalb von 60 min", fmt: (r) => <span className={r.missed_winners ? "warn-ink" : "dim"}>{n(r.missed_winners)}</span> },
          { k: "avoided_rugs", l: "Vermiedene Rugs", r: true, title: "≤ −80 % nach 60 min", fmt: (r) => <span className={r.avoided_rugs ? "up" : "dim"}>{n(r.avoided_rugs)}</span> },
          { k: "hit_rate_2x", l: "Treffer", r: true, fmt: (r) => pct(r.hit_rate_2x) },
          { k: "avg_ret_60m", l: "Ø nach 60 min", r: true, fmt: (r) => <Delta v={r.avg_ret_60m} d={0} arrow={false} /> },
          { k: "pnl_without_sol", l: "Ohne Filter", r: true, title: "Ergebnis, wenn wir diese Launches trotzdem gekauft hätten", fmt: (r) => <span className={sign(r.pnl_without_sol)}>{solSigned(r.pnl_without_sol, 2)}</span> },
          { k: "filter_value_sol", l: "Filterwert", r: true, fmt: (r) => <span className={`b ${sign(r.filter_value_sol)}`}>{solSigned(r.filter_value_sol, 2)}</span> },
        ]} />
        <div className="help" style={{ padding: "8px 14px" }}>
          Netto mit Filtern (echte Papier-Trades): <b className={sign(ft.with_sol)}>{solSigned(ft.with_sol, 3)}</b> · ohne Filter (zusätzlich alle {n(ft.measured)} nachgemessenen übersprungenen Launches gekauft): <b className={sign(ft.without_sol)}>{solSigned(ft.without_sol, 2)}</b>
        </div>
      </Card>

      <Card flush>
        <div style={{ padding: "4px 10px 0" }}>
          <Tabs value={tab} onChange={setTab} options={[["open", `Offene Papier-Positionen (${open.length})`], ["closed", `Abgeschlossen (${closed.length})`]]} />
        </div>
        {tab === "open" && (
          <Table rows={open} rowKey={(r) => r.id} empty="Keine offene Position" cols={[
            { k: "symbol", l: "Coin", fmt: (r) => <CoinCell r={r} /> },
            { k: "opened_ts", l: "Seit", fmt: (r) => ago(r.opened_ts) },
            { k: "entry_mcap_usd", l: "Einstieg MC", r: true, fmt: (r) => usd(r.entry_mcap_usd) },
            { k: "score", l: "Score", r: true, fmt: (r) => n(r.score) },
            { k: "entry_why", l: "Einstiegsgrund", sort: false, fmt: (r) => <span className="t2" style={{ whiteSpace: "normal" }}>{(r.entry_why || []).join(" · ") || "–"}</span> },
            { k: "tp1_done", l: "Teilverkauf", fmt: (r) => (r.tp1_done ? <span className="badge good">TP1 erledigt</span> : <span className="dim">noch nicht</span>) },
            { k: "unrealized_pct", l: "Ergebnis", r: true, fmt: (r) => <Delta v={r.unrealized_pct} /> },
          ]} />
        )}
        {tab === "closed" && (
          <Table rows={closed} rowKey={(r) => r.id} tall cols={[
            { k: "closed_ts", l: "Verkauft", fmt: (r) => dt(r.closed_ts) },
            { k: "symbol", l: "Coin", fmt: (r) => <CoinCell r={r} /> },
            { k: "entry_mcap_usd", l: "Einstieg MC", r: true, fmt: (r) => usd(r.entry_mcap_usd) },
            { k: "score", l: "Score", r: true, fmt: (r) => n(r.score) },
            { k: "entry_why", l: "Einstiegsgrund", sort: false, fmt: (r) => <span className="t2" style={{ whiteSpace: "normal" }}>{(r.entry_why || [])[0] || "–"}</span> },
            { k: "hold", l: "Haltedauer", r: true, sort: (r) => r.closed_ts - r.opened_ts, fmt: (r) => dur((r.closed_ts - r.opened_ts) / 60) },
            { k: "exit_label", l: "Ausstieg", fmt: (r) => <span title={(r.sells || []).map((x) => `${dt(x.ts)} ${x.reason}`).join("\n")}>{r.exit_label || "–"}</span> },
            { k: "peak_pct", l: "Hoch", r: true, fmt: (r) => <Delta v={r.peak_pct} d={0} arrow={false} /> },
            { k: "pnl_pct", l: "Ergebnis", r: true, fmt: (r) => <Delta v={r.pnl_pct} /> },
            { k: "pnl_sol", l: "SOL", r: true, fmt: (r) => <span className={sign(r.pnl_sol)}>{solSigned(r.pnl_sol, 4)}</span> },
          ]} />
        )}
      </Card>

      <Card title="Erkannte Launches" sub="Neueste zuerst · Klick zeigt Risiken und was dafür sprach · Ergebnis ab Entscheidung" flush
        actions={<Seg value={only} onChange={setOnly} options={[["", "Alle"], ["enter", "Gekauft"], ["deep", "Tief geprüft"], ["skip", "Übersprungen"]]} />}>
        <Table rows={launches} rowKey={(r) => r.mint} tall max={300} expand={(r) => <LaunchDetail r={r} />} empty="Keine Launches im Zeitraum" cols={[
          { k: "decided_ts", l: "Zeit", fmt: (r) => <span title={dt(r.decided_ts)}>{dt(r.decided_ts, false)}</span> },
          { k: "symbol", l: "Coin", fmt: (r) => <CoinCell r={r} /> },
          { k: "source_label", l: "Quelle" },
          { k: "score", l: "Score", r: true, title: "Momentum × (1 − Risiko); nur bei tief geprüften Launches",
            fmt: (r) => (r.score != null ? <span title={`Momentum ${n(r.momentum)} · Risiko ${n(r.risk)}`}>{n(r.score)}</span> : <span className="dim">–</span>) },
          { k: "reasons", l: "Wichtigster Grund", sort: false, fmt: (r) => { const t = r.reasons?.[0]?.text || r.positives?.[0] || "–"; return <span className="t2 clip" style={{ maxWidth: 220 }} title={t}>{t}</span>; } },
          { k: "bundle_pct", l: "Bundle", sort: (r) => r.bundle_pct ?? r.unseen_pct ?? -1, fmt: (r) => <Bundle r={r} /> },
          { k: "dev_buy_pct", l: "Dev-Anteil", r: true, fmt: (r) => (r.dev_buy_pct != null ? <span className={r.dev_buy_pct >= 20 ? "down" : ""}>{n(r.dev_buy_pct, 1)} %{r.dev_sold_pct ? <span className="dim"> · {n(r.dev_sold_pct)} % verk.</span> : ""}</span> : "–") },
          { k: "decision", l: "Entscheidung", fmt: (r) => <Decision r={r} /> },
          { k: "max_ret_60m", l: "Hoch 60 min", r: true, fmt: (r) => <Delta v={r.max_ret_60m} d={0} arrow={false} /> },
          { k: "ret_60m", l: "nach 60 min", r: true, fmt: (r) => (r.ret_60m != null ? <span>{pctSigned(r.ret_60m, 0)}{r.rugged ? <span className="badge bad" style={{ marginLeft: 4 }}>Rug</span> : null}</span> : <span className="dim">läuft</span>) },
        ]} />
      </Card>

      <Card title="Budget des Dienstes" sub={d.stream.source === "heartbeat" ? `Stand ${ago(d.stream.heartbeat_ts)} · wird das Budget knapp, wartet der Sniper vor weiteren Abfragen` : ""} flush>
        <BudgetTable s={d.stream} />
      </Card>

      <Card title="Abfragen nach Anbieter" sub={`Abfragen je tief geprüftem Launch: RPC ${n(st.calls_per_deep_launch?.rpc, 1)} · pump.fun ${n(st.calls_per_deep_launch?.pump_api, 1)}`} flush>
        <Table rows={d.stream.api} rowKey={(r) => r.group} empty="Der Sniper hat noch keine Verbrauchsdaten geschrieben" cols={[
          { k: "group", l: "Anbieter", fmt: (r) => <span className="mono">{r.group}</span> },
          { k: "calls_window", l: "Calls 10 min", r: true, fmt: (r) => n(r.calls_window) },
          { k: "last_min", l: "Letzte Minute", r: true, fmt: (r) => n(r.last_min) },
          { k: "budget_per_min", l: "Budget/min", r: true, fmt: (r) => (r.budget_per_min ? n(r.budget_per_min) : "ohne") },
          { k: "load", l: "Auslastung", fmt: (r) => <LoadBar v={r.load} /> },
          { k: "credits_month", l: "Helius-Credits Monat", r: true, fmt: (r) => (r.credits_cap ? `${n(r.credits_month)} / ${n(r.credits_cap)}` : "–") },
        ]} />
      </Card>
    </div>
  );
}
