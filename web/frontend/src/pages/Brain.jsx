import { useState } from "react";
import { LoadBar } from "./System.jsx";
import { EChart, baseOption, catAxis, timeAxis, tipRow, valueAxis } from "../chart.jsx";
import { ago, dt, dur, n, pct, pctSigned, sign, sol, solSigned, usePoll } from "../lib.js";
import { Callout, Card, Empty, Seg, Table, Tile } from "../ui.jsx";

const shortMint = (m) => (m ? `${m.slice(0, 4)}…${m.slice(-4)}` : "–");
const CoinLink = ({ r }) => (
  <a href={`#/coin/${r.mint}`} className="b" onClick={(e) => e.stopPropagation()}>{r.symbol || shortMint(r.mint)}</a>
);

const dayLabel = (ts) => { const d = new Date(ts * 1000); return `${String(d.getUTCDate()).padStart(2, "0")}.${String(d.getUTCMonth() + 1).padStart(2, "0")}.`; };
const fx = (v, d = 2) => (v == null ? "–" : `×${n(v, d)}`);
const regimeText = (x) => `Regime gewechselt: die letzten ${n(x.n)} Ergebnisse lagen Ø ${pctSigned(x.err, 1)} neben der Vorhersage, altes Wissen halbiert`;

function EquityChart({ eq, start, regimes = [] }) {
  if (!eq || eq.length < 2) return <Empty>Noch keine Equity-Kurve: der Dienst schreibt sie jede Minute, sobald er läuft</Empty>;
  return (
    <EChart height={220} deps={[eq]} ariaLabel="Equity des Entscheider-Papierkontos"
      build={(c) => baseOption(c, {
        tooltip: {
          ...baseOption(c).tooltip,
          formatter: (ps) => {
            const p = eq[ps[0].dataIndex];
            return `<b>${dt(p.ts)}</b>${tipRow(c.s[0], "Equity", sol(p.equity_sol, 3))}${tipRow("transparent", "davon offen", sol(p.open_value_sol, 3))}`;
          },
        },
        xAxis: timeAxis(c), yAxis: valueAxis(c, (v) => n(v, 2), { scale: true }),
        series: [{
          type: "line", showSymbol: false, lineStyle: { width: 2, color: c.s[0] }, itemStyle: { color: c.s[0] },
          data: eq.map((p) => [p.ts * 1000, p.equity_sol]),
          markLine: { symbol: "none", lineStyle: { color: c.muted, type: "dashed", width: 1 },
            label: { color: c.muted, formatter: "Start", position: "insideEndTop" },
            tooltip: { trigger: "item", formatter: (p) => p.data.tip || "" },
            data: [{ yAxis: start }, ...regimes.filter((x) => x.at * 1000 >= eq[0].ts * 1000).map((x) => ({
              xAxis: x.at * 1000, tip: `<b>${dt(x.at)}</b><br/>${regimeText(x)}`,
              lineStyle: { color: c.warn, type: "solid", width: 1 }, label: { color: c.warn, formatter: "Regime", position: "insideEndTop" },
            }))] },
        }],
      })} />
  );
}

/** Taken vs passed vs copy-everything, average net per opportunity after costs, as diverging bars around 0. */
function Honest({ h }) {
  const rows = [
    ["Genommen", h.taken_mean, h.n_taken, "was er gekauft hat oder kaufen wollte"],
    ["Ausgelassen", h.passed_mean, h.n_passed, "was er liegen ließ, nachgespielt"],
    ["Alles stumpf kopiert", h.copy_all_mean, h.n, "jede Gelegenheit mit „Pump mitnehmen“"],
  ];
  const max = Math.max(0.01, ...rows.map((r) => Math.abs(r[1] || 0)));
  if (!h.n) return <Empty>Noch nichts nachgeprüft: eine Gelegenheit wird bewertet, wenn ihr längster Ausstiegsplan vorbei ist (etwa 4 h)</Empty>;
  return (
    <div className="col" style={{ gap: 10 }}>
      {rows.map(([l, v, cnt, what]) => (
        <div key={l} className="hrow">
          <div className="hrow-l"><b>{l}</b><span className="dim">{n(cnt)} · {what}</span></div>
          <div className="hbar" aria-hidden="true">
            <span className="hbar-zero" />
            {v != null && <span className={`hbar-fill ${v >= 0 ? "pos" : "neg"}`}
              style={{ width: `${(Math.abs(v) / max) * 50}%`, [v >= 0 ? "left" : "right"]: "50%" }} />}
          </div>
          <div className={`hrow-v b ${sign(v)}`}>{pctSigned(v, 1)}</div>
        </div>
      ))}
      <div className="help" style={{ margin: 0 }}>
        Ø Netto je Gelegenheit nach Kosten, mit der Vorhersage von damals verglichen (out of sample). Gut ist er, wenn „Genommen“
        klar über „Ausgelassen“ und „Alles stumpf kopiert“ liegt.{h.taken_pred != null && <> Vorhergesagt hatte er für das Genommene Ø {pctSigned(h.taken_pred, 1)}.</>}
      </div>
    </div>
  );
}

/** Day factor (equity at the UTC day's end / its start) as bars on a log scale, with the goal band x2 to x4. */
function DayFactor({ days, goal, regimes }) {
  const ds = (days || []).filter((x) => x.mult != null);
  if (!ds.length) return <Empty>Noch kein voller Tag mit Equity-Punkten</Empty>;
  const regDays = new Set((regimes || []).map((x) => x.at - (x.at % 86400)));
  const [lo, hi] = goal.mult;
  return (
    <EChart height={220} deps={[days]} ariaLabel="Tagesfaktor des Papierkontos gegen das Ziel"
      build={(c) => baseOption(c, {
        tooltip: {
          ...baseOption(c).tooltip, axisPointer: { type: "shadow" },
          formatter: (ps) => {
            const x = ds[ps[0].dataIndex];
            const reg = (x.regimes || []).map((r) => `<br/><span style="color:${c.warn}">${regimeText(r)}</span>`).join("");
            return `<b>${dayLabel(x.ts)} (UTC-Tag)</b>${tipRow(x.mult >= 1 ? c.good : c.bad, "Tagesfaktor", fx(x.mult))}${tipRow("transparent", "Konto", `${sol(x.start_sol, 3)} → ${sol(x.end_sol, 3)}`)}${tipRow("transparent", "Ziel", `×${lo} bis ×${hi}`)}${reg}`;
          },
        },
        xAxis: catAxis(c, ds.map((x) => dayLabel(x.ts) + (regDays.has(x.ts) ? " ⚑" : ""))),
        yAxis: valueAxis(c, (v) => `×${n(v, v < 1 ? 2 : 1)}`, { type: "log", logBase: 2, min: (v) => Math.min(0.5, v.min), max: (v) => Math.max(hi * 1.1, v.max) }),
        series: [{
          type: "bar", barMaxWidth: 22, data: ds.map((x) => ({ value: x.mult, itemStyle: { color: x.mult >= 1 ? c.good : c.bad, borderRadius: x.mult >= 1 ? [4, 4, 0, 0] : [0, 0, 4, 4] } })),
          markArea: { silent: true, itemStyle: { color: c.good, opacity: 0.1 }, label: { color: c.muted, position: "insideTopLeft", formatter: `Ziel ×${lo}–×${hi}` }, data: [[{ yAxis: lo }, { yAxis: hi }]] },
          markLine: { silent: true, symbol: "none", lineStyle: { color: c.muted, type: "dashed", width: 1 }, label: { color: c.muted, formatter: "×1", position: "insideEndTop" }, data: [{ yAxis: 1 }] },
        }],
      })} />
  );
}

/** Hour factor of the last 24 h (the challenge: double every hour), log scale, line at the goal. */
function HourFactor({ hours, goal }) {
  const hs = hours || [];
  if (!hs.length) return <Empty>Noch keine Stunde mit Equity-Punkten</Empty>;
  const hit = hs.filter((x) => !x.partial && x.mult >= goal).length, full = hs.filter((x) => !x.partial).length;
  const hh = (ts) => { const d = new Date(ts * 1000); return `${String(d.getHours()).padStart(2, "0")}:00`; };
  return (
    <>
      <EChart height={220} deps={[hours]} ariaLabel="Stundenfaktor der letzten 24 Stunden"
        build={(c) => baseOption(c, {
          tooltip: { ...baseOption(c).tooltip, axisPointer: { type: "shadow" },
            formatter: (ps) => { const x = hs[ps[0].dataIndex]; return `<b>${hh(x.ts)}${x.partial ? " (läuft noch)" : ""}</b>${tipRow(x.mult >= 1 ? c.good : c.bad, "Stundenfaktor", fx(x.mult, 3))}${tipRow("transparent", "Konto", `${sol(x.start_sol, 3)} → ${sol(x.end_sol, 3)}`)}`; } },
          xAxis: catAxis(c, hs.map((x) => hh(x.ts))),
          yAxis: valueAxis(c, (v) => `×${n(v, v < 1 ? 2 : 1)}`, { type: "log", logBase: 2, min: (v) => Math.min(0.5, v.min), max: (v) => Math.max(goal * 1.2, v.max) }),
          series: [{
            type: "bar", barMaxWidth: 14,
            data: hs.map((x) => ({ value: x.mult, itemStyle: { color: x.mult >= 1 ? c.good : c.bad, opacity: x.partial ? 0.45 : 1, borderRadius: x.mult >= 1 ? [3, 3, 0, 0] : [0, 0, 3, 3] } })),
            markLine: { silent: true, symbol: "none", data: [
              { yAxis: goal, lineStyle: { color: c.good, type: "solid", width: 1 }, label: { color: c.good, formatter: `Ziel ×${goal}`, position: "insideEndTop" } },
              { yAxis: 1, lineStyle: { color: c.muted, type: "dashed", width: 1 }, label: { color: c.muted, formatter: "×1", position: "insideEndTop" } },
            ] },
          }],
        })} />
      <div className="help" style={{ margin: "6px 0 0" }}>×{goal} erreicht in {n(hit)} von {n(full)} vollen Stunden; die laufende Stunde ist blass.</div>
    </>
  );
}

/** Selection lead per day: taken minus copy-everything, average net per trade, out of sample. */
function AlphaChart({ days }) {
  const ds = (days || []).filter((x) => x.alpha != null);
  if (!ds.length) return <Empty>Noch kein Tag mit nachgeprüften Gelegenheiten</Empty>;
  return (
    <EChart height={190} deps={[days]} ariaLabel="Auswahl-Vorsprung je Tag"
      build={(c) => baseOption(c, {
        tooltip: { ...baseOption(c).tooltip, axisPointer: { type: "shadow" },
          formatter: (ps) => { const x = ds[ps[0].dataIndex]; return `<b>${dayLabel(x.ts)}</b>${tipRow(x.alpha >= 0 ? c.good : c.bad, "Vorsprung", pctSigned(x.alpha, 1))}${tipRow("transparent", "nachgeprüft", `${n(x.n_eval)}, davon ${n(x.n_taken)} genommen`)}`; } },
        xAxis: catAxis(c, ds.map((x) => dayLabel(x.ts))), yAxis: valueAxis(c, (v) => pctSigned(v, 0)),
        series: [{ type: "bar", barMaxWidth: 22, data: ds.map((x) => ({ value: x.alpha, itemStyle: { color: x.alpha >= 0 ? c.good : c.bad, borderRadius: x.alpha >= 0 ? [4, 4, 0, 0] : [0, 0, 4, 4] } })) }],
      })} />
  );
}

/** Capture rate per day: share of each coin's best move after the buy that the exit took home. */
function CaptureChart({ days }) {
  const ds = (days || []).filter((x) => x.capture_organic != null || x.capture_plan != null || x.capture_own != null);
  if (!ds.length) return <Empty>Noch keine nachgespielten Ausstiege</Empty>;
  const ser = [["capture_organic", "organisch (nachgespielt)"], ["capture_plan", "feste Pläne (nachgespielt)"], ["capture_own", "eigene Trades"]];
  return (
    <>
      <EChart height={190} deps={[days]} ariaLabel="Einfangquote je Tag"
        build={(c) => baseOption(c, {
          tooltip: { ...baseOption(c).tooltip, formatter: (ps) => `<b>${dayLabel(ds[ps[0].dataIndex].ts)}</b>` + ps.map((p) => tipRow(p.color, p.seriesName, pct(p.value))).join("") },
          xAxis: catAxis(c, ds.map((x) => dayLabel(x.ts)), { boundaryGap: false }), yAxis: valueAxis(c, (v) => pct(v)),
          series: ser.map(([k, l], i) => ({ name: l, type: "line", connectNulls: true, symbolSize: 8, lineStyle: { width: 2, color: c.s[i] }, itemStyle: { color: c.s[i] }, data: ds.map((x) => x[k]) })),
        })} />
      <div className="legend-row">{ser.map(([k, l], i) => <span key={k}><i style={{ background: `var(--series-${i + 1})` }} />{l}</span>)}</div>
    </>
  );
}

function SegTable({ rows }) {
  return (
    <Table rows={rows} rowKey={(r) => r.key} empty="Noch keine nachgeprüften Gelegenheiten" cols={[
      { k: "label", l: "Segment" },
      { k: "n", l: "Gelegenheiten", r: true, fmt: (r) => n(r.n) },
      { k: "copy_all_mean", l: "Alles kopiert Ø", r: true, fmt: (r) => <span className={sign(r.copy_all_mean)}>{pctSigned(r.copy_all_mean, 1)}</span> },
      { k: "n_taken", l: "Genommen", r: true, fmt: (r) => n(r.n_taken) },
      { k: "taken_mean", l: "Genommen Ø", r: true, fmt: (r) => <b className={sign(r.taken_mean)}>{pctSigned(r.taken_mean, 1)}</b> },
    ]} />
  );
}

function Exits({ x }) {
  const rp = x.replays || {};
  const rows = [
    ...(x.by_exit || []).map((e) => ({ k: e.mode, l: `Eigene Trades, ${e.label}`, n: e.n, mean: e.mean, capture: e.capture, sol: e.sol })),
    ...(rp.n ? [
      { k: "r-org", l: "Nachgespielt: organisch", n: rp.n, mean: rp.organic, capture: rp.capture_organic },
      { k: "r-plan", l: "Nachgespielt: feste Pläne", n: rp.n, mean: rp.plan, capture: rp.capture_plan },
      { k: "r-hold", l: "Nachgespielt: 1 h halten", n: rp.n, mean: rp.hold_1h },
    ] : []),
  ];
  return (
    <Table rows={rows} rowKey={(r) => r.k} empty="Noch kein Ausstieg zum Vergleichen" cols={[
      { k: "l", l: "Ausstieg" },
      { k: "n", l: "Anzahl", r: true, fmt: (r) => n(r.n) },
      { k: "mean", l: "Ø netto", r: true, fmt: (r) => <b className={sign(r.mean)}>{pctSigned(r.mean, 1)}</b> },
      { k: "capture", l: "Vom Hoch mitgenommen", r: true, title: "Anteil am besten Anstieg nach dem Kauf (nur Coins mit +10 % und mehr)", fmt: (r) => pct(r.capture) },
      { k: "sol", l: "SOL", r: true, fmt: (r) => (r.sol == null ? "" : <span className={sign(r.sol)}>{solSigned(r.sol, 3)}</span>) },
    ]} />
  );
}

const ExitMode = ({ r }) => (r.exit_mode === "organic"
  ? <span className="badge good" title="Ausstieg nach dem Zustand des Coins, gelernt aus jeder nachgespielten Minute">organisch</span>
  : <span className="badge" title="Ausstieg nach den festen Regeln des Plans">Plan</span>);

const halfCell = (m, lo, k) => (m == null ? <span className="dim">–</span>
  : <span title={`${n(k)} Fälle, Untergrenze ${pctSigned(lo, 1)}`}><span className={sign(m)}>{pctSigned(m, 1)}</span> <span className={`dim ${lo > 0 ? "up" : ""}`}>({pctSigned(lo, 0)})</span></span>);

/** Pockets (plan 18.10., step 5): stake only where the 95 % lower bound is above zero in both halves of the window. */
function Pockets({ p }) {
  const r = p.rule || {};
  const status = (x) => (x.released ? <span className="badge good">freigegeben</span>
    : x.demoted_at ? <span className="badge bad" title={`zurückgestuft ${dt(x.demoted_at)}`}>zurückgestuft</span>
      : (x.n || 0) < (r.min_n || 100) ? <span className="badge">sammelt</span> : <span className="badge warn">nicht frei</span>);
  return (
    <Card flush title="Taschen" sub={`Muster je Quelle, netto nach Kosten über ${n(r.window_days)} Tage. Einsatz nur, wenn ab ${n(r.min_n)} Fällen die ${pct(r.conf)}-Untergrenze in beiden Hälften über 0 liegt und die letzten ${n(r.last_n)} im Plus sind.${p.at ? ` Stand ${ago(p.at)}.` : ""}`}
      actions={<span className="dim" style={{ fontSize: 12 }}>{n(p.released)} freigegeben · {n(p.positive)} mit Untergrenze &gt; 0</span>}>
      <Table rows={p.rows} rowKey={(x) => x.key} initialSort={{ k: "lower95", d: "desc" }}
        empty="Noch keine Tasche mit genug Fällen (sie erscheinen ab 50, das Hirn rechnet alle 10 Minuten)"
        expand={(x) => <div className="col" style={{ gap: 4 }}><span>{x.reason || "keine Begründung"}</span>
          {x.released_at && <span className="dim">freigegeben {dt(x.released_at)}</span>}{x.demoted_at && <span className="dim">zurückgestuft {dt(x.demoted_at)}</span>}
          <span className="dim mono">{x.key}</span></div>}
        cols={[
          { k: "label", l: "Tasche", fmt: (x) => <span><b>{x.label}</b> <span className="dim">{x.source_label}</span></span> },
          { k: "n", l: "Fälle", r: true, fmt: (x) => n(x.n) },
          { k: "mean", l: "Ø netto", r: true, fmt: (x) => <b className={sign(x.mean)}>{pctSigned(x.mean, 1)}</b> },
          { k: "lower95", l: "Untergrenze 95 %", r: true, fmt: (x) => <span className={x.lower95 > 0 ? "up b" : "dim"}>{pctSigned(x.lower95, 1)}</span> },
          { k: "mean_first_half", l: "1. Hälfte", r: true, fmt: (x) => halfCell(x.mean_first_half, x.lower95_first_half, x.n_first_half) },
          { k: "mean_second_half", l: "2. Hälfte", r: true, fmt: (x) => halfCell(x.mean_second_half, x.lower95_second_half, x.n_second_half) },
          { k: "last50_mean", l: `Letzte ${n(r.last_n)}`, r: true, fmt: (x) => <span className={sign(x.last50_mean)}>{pctSigned(x.last50_mean, 1)}</span> },
          { k: "released", l: "Status", fmt: status },
        ]} />
    </Card>
  );
}

function Decision({ d }) {
  const [open, setOpen] = useState(false);
  const lines = d.note || [];
  const shown = open ? lines : lines.slice(0, 2);
  return (
    <div className="dec">
      <div className="dec-h">
        <span className={`badge ${d.tone}`}>{d.verdict}</span>
        <span className="dim">{d.kind_label}</span>
        <CoinLink r={d} />
        <span className="dim" style={{ marginLeft: "auto" }} title={dt(d.ts)}>{ago(d.ts)}</span>
      </div>
      <div className="dec-k">
        <span>Plan <b>{d.plan_label || "–"}</b></span>
        <span>erwartet <b className={sign(d.pred_mean)}>{pctSigned(d.pred_mean, 1)}</b></span>
        <span>sicher zu <b>{pct(d.pred_p)}</b></span>
        {d.size_sol != null && d.decision === "enter" && <span>Einsatz <b>{sol(d.size_sol, 2)}</b></span>}
      </div>
      {shown.length > 0 && (
        <ul className="dec-note" onClick={() => setOpen(!open)}>
          {shown.map((l, i) => <li key={i}>{l}</li>)}
          {lines.length > 2 && <li className="dim">{open ? "weniger" : `+ ${lines.length - 2} Zeilen Begründung`}</li>}
        </ul>
      )}
      {(d.evaluated || d.position) && (
        <div className="dec-o">
          {d.position && (d.position.status === "open"
            ? <span><span className="badge">offen</span></span>
            : <span>Papier-Trade <b className={sign(d.position.pnl_sol)}>{solSigned(d.position.pnl_sol, 3)}</b> ({pctSigned(d.position.pnl_pct, 1)}{d.position.exit_label ? `, ${d.position.exit_label}` : ""})</span>)}
          {d.evaluated && <span>nachgespielt: sein Plan <b className={sign(d.realized_plan)}>{pctSigned(d.realized_plan, 1)}</b> · stumpf kopiert {pctSigned(d.realized_base, 1)} · bester Plan {pctSigned(d.realized_best, 1)}</span>}
        </div>
      )}
    </div>
  );
}

function Diary({ items }) {
  const [open, setOpen] = useState(0);
  if (!items?.length) return <Empty>Noch kein Eintrag: das Lerntagebuch entsteht einmal am Tag (abends)</Empty>;
  return (
    <div className="col" style={{ gap: 8 }}>
      {items.map((e, i) => (
        <div key={e.day} className="diary">
          <button className="diary-h" onClick={() => setOpen(open === i ? -1 : i)} aria-expanded={open === i}>
            <b>{e.day}</b><span className="dim">{open === i ? "zuklappen" : "lesen"}</span>
          </button>
          {open === i && <div className="diary-t">{e.text}</div>}
        </div>
      ))}
    </div>
  );
}

const lastOf = (days, k) => { for (let i = (days || []).length - 1; i >= 0; i--) if (days[i][k] != null) return days[i][k]; return null; };

export default function Brain() {
  const [kind, setKind] = useState("");
  const [show, setShow] = useState("all");
  const [limit, setLimit] = useState(15);
  const { data: d, error } = usePoll(`/api/brain?show=${show}&limit=${limit}${kind ? `&kind=${kind}` : ""}`, 10000);
  if (error) return <Callout kind="bad" icon="alert">{error}</Callout>;
  if (!d) return <div className="empty">Lade …</div>;
  if (!d.available) return <Callout icon="info">{d.note}</Callout>;
  const a = d.account, h = d.honest, l = d.last24;
  return (
    <div className="col" style={{ gap: 16 }}>
      <Callout icon="info">
        Der Entscheider wägt jede Gelegenheit einzeln ab (Wal-Kauf, Signal, Launch), wählt dazu einen Ausstiegsplan und lernt aus jedem
        Ergebnis, auch aus dem, was er ausgelassen hat. Eigenes Papierkonto mit {sol(a.start_sol, 0)} Start, nie Echtgeld.
      </Callout>
      <div className="tiles wrap-sub">
        <Tile label="Equity (Papier)" value={sol(a.equity_sol, 3)} sub={`Start ${sol(a.start_sol, 1)} · ${n(a.open)} offen, ${sol(a.open_value_sol, 2)} im Markt`} />
        <Tile label="Ergebnis" value={solSigned(a.result_sol, 3)} cls={sign(a.result_sol)}
          sub={a.closed ? `${n(a.closed)} Trades · ${pct(a.win_rate)} im Plus` : "noch kein Trade abgeschlossen"} />
        <Tile label="Entscheidungen 24 h" value={n(l.n)} sub={`${n(l.entered)} gekauft · ${n(l.explored)} zum Erkunden · ${n(l.blocked)} von Grenzen gestoppt`} />
        <Tile label="Nachgeprüft" value={`${n(h.n_taken)} / ${n(h.target)}`} sub={<LoadBar v={h.progress} />} title="genommene Gelegenheiten mit bekanntem Ergebnis, Ziel 100" />
        <Tile label="Tagesfaktor heute" value={fx(d.goal.today)} cls={d.goal.today == null ? "" : d.goal.today >= d.goal.mult[0] ? "up" : d.goal.today < 1 ? "down" : ""}
          sub={`Ziel ×${d.goal.mult[0]} bis ×${d.goal.mult[1]} am Tag (UTC-Tag bisher)`} />
        <Tile label="Auswahl-Vorsprung" value={pctSigned(d.alpha, 1)} cls={sign(d.alpha)} sub="genommen minus alles kopiert, Ø je Trade, nachgeprüft" />
        <Tile label="Einfangquote" value={pct(lastOf(d.days, "capture_own") ?? lastOf(d.days, "capture_organic"))}
          sub={lastOf(d.days, "capture_own") != null ? "Anteil am besten Anstieg, eigene Trades (letzter Tag)" : "Anteil am besten Anstieg, organisch nachgespielt"} />
      </div>
      {d.regimes?.length > 0 && d.regimes[d.regimes.length - 1].at > d.now - 86400 && (
        <Callout kind="warn" icon="alert">{regimeText(d.regimes[d.regimes.length - 1])} ({ago(d.regimes[d.regimes.length - 1].at)}).</Callout>
      )}

      <div className="grid g-wide-left">
        <Card title="Equity über Zeit" sub="Guthaben plus offene Positionen zum letzten Preis, jede Minute">
          <EquityChart eq={d.equity} start={a.start_sol} regimes={d.regimes} />
        </Card>
        <Card title="Stundenfaktor, letzte 24 h" sub={`Konto am Ende jeder Stunde geteilt durch den Anfang; Challenge: ×${d.goal.hour ?? 2} je Stunde`}>
          <HourFactor hours={d.hours} goal={d.goal.hour ?? 2} />
        </Card>
      </div>
      <div className="grid g-wide-left">
        <Card title="Tagesfaktor gegen das Ziel" sub={`Konto am Ende jedes UTC-Tags geteilt durch den Anfang; grünes Band = Ziel ×${d.goal.mult[0]} bis ×${d.goal.mult[1]}; ⚑ = Regime-Wechsel`}>
          <DayFactor days={d.days} goal={d.goal} regimes={d.regimes} />
        </Card>
        <Card title="Ehrliche Kennzahl" sub="Ø netto je Gelegenheit: genommen, ausgelassen, alles kopiert">
          <Honest h={h} />
          {h.by_kind?.length > 0 && (
            <div className="kvs" style={{ marginTop: 10 }}>
              {h.by_kind.map((k) => (
                <div key={k.kind}><div className="dim">{k.label} ({n(k.n_taken)} von {n(k.n)})</div>
                  <b className={sign(k.taken_mean)}>{pctSigned(k.taken_mean, 1)}</b>
                  <span className="dim"> gegen {pctSigned(k.copy_all_mean, 1)} alles kopiert</span></div>
              ))}
            </div>
          )}
        </Card>
      </div>

      {d.pockets && <Pockets p={d.pockets} />}

      <div className="grid g2">
        <Card title="Auswahl-Vorsprung je Tag" sub="Ø netto des Genommenen minus Ø netto von allem, nachgeprüft am Tag des Ergebnisses">
          <AlphaChart days={d.days} />
        </Card>
        <Card title="Einfangquote je Tag" sub="wie viel vom besten Anstieg nach dem Kauf der Ausstieg mitgenommen hat">
          <CaptureChart days={d.days} />
        </Card>
      </div>
      <Card title="Ausstieg im Vergleich" sub="organisch gegen feste Pläne gegen 1 h halten, nachgespielt auf ungesehenen Coins und bei den eigenen Trades" flush>
        <Exits x={d.exits || {}} />
      </Card>
      <div className="grid g2">
        <Card title="Wal-Kopien nach Verzögerung" sub="wie spät der Wal-Kauf erkannt wurde" flush><SegTable rows={d.segments?.by_delay || []} /></Card>
        <Card title="Curve gegen Pool" sub="Coins noch auf der Bonding-Curve oder schon im Pool" flush><SegTable rows={d.segments?.by_liq || []} /></Card>
      </div>

      <Card title="Offene Positionen" sub="mit dem Plan, den er beim Kauf gewählt hat" flush>
        <Table rows={d.open} rowKey={(r) => r.id} empty="Keine offene Position" cols={[
          { k: "symbol", l: "Coin", fmt: (r) => <CoinLink r={r} /> },
          { k: "kind_label", l: "Quelle" },
          { k: "plan_label", l: "Plan", fmt: (r) => <span title={`Ziel +${n(r.take_profit * 100)} %, Stop −${n(r.stop_loss * 100)} %, max. ${n(r.max_hold_min)} min`}>{r.plan_label}</span> },
          { k: "exit_mode", l: "Ausstieg", fmt: (r) => <ExitMode r={r} /> },
          { k: "size_sol", l: "Einsatz", r: true, fmt: (r) => sol(r.size_sol, 2) },
          { k: "partial_sol", l: "Teilverkauf", r: true, title: "die Hälfte schon verkauft, Erlös gebucht", fmt: (r) => (r.partial_sol ? sol(r.partial_sol, 3) : <span className="dim">–</span>) },
          { k: "unrealized_pct", l: "Jetzt", r: true, title: "ganzer Trade: Wert der restlichen Tokens plus Erlös aus dem Teilverkauf", fmt: (r) => <b className={sign(r.unrealized_pct)}>{pctSigned(r.unrealized_pct, 1)}</b> },
          { k: "peak_pct", l: "Hoch", r: true, fmt: (r) => pctSigned(r.peak_pct, 0) },
          { k: "held_min", l: "Seit", r: true, fmt: (r) => dur(r.held_min) },
        ]} />
      </Card>

      <Card title="Entscheidungen" sub="jede mit 2 bis 4 Zeilen Begründung; antippen zeigt alle Zeilen"
        actions={<div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
          <Seg value={kind} onChange={setKind} options={[["", "Alle"], ["whale", "Wal"], ["signal", "Signal"], ["launch", "Launch"]]} />
          <Seg value={show} onChange={setShow} options={[["all", "Alle"], ["enter", "Gekauft"], ["skip", "Ausgelassen"], ["wanted", "Gestoppt"]]} />
        </div>}>
        {d.decisions.length ? <div className="col" style={{ gap: 10 }}>{d.decisions.map((x) => <Decision key={x.id} d={x} />)}</div>
          : <Empty>Keine Entscheidung in dieser Auswahl</Empty>}
        {d.decisions.length >= limit && limit < 300 && (
          <div style={{ marginTop: 10 }}><button className="btn" onClick={() => setLimit(Math.min(300, limit + 30))}>Ältere Entscheidungen zeigen</button></div>
        )}
      </Card>

      <div className="grid g-wide-left">
        <Card title="Abgeschlossene Trades" flush>
          <Table rows={d.closed} rowKey={(r) => r.id} empty="Noch kein Trade abgeschlossen" initialSort={{ k: "closed_at", d: "desc" }} cols={[
            { k: "symbol", l: "Coin", fmt: (r) => <CoinLink r={r} /> },
            { k: "plan_label", l: "Plan" },
            { k: "exit_label", l: "Ausstieg", fmt: (r) => <span>{r.exit_label}{r.partial_sol ? <span className="dim"> · halb vorher {sol(r.partial_sol, 3)}</span> : null}</span> },
            { k: "pnl_sol", l: "Ergebnis", r: true, fmt: (r) => <b className={sign(r.pnl_sol)}>{solSigned(r.pnl_sol, 3)}</b> },
            { k: "pnl_pct", l: "%", r: true, fmt: (r) => pctSigned(r.pnl_pct, 1) },
            { k: "closed_at", l: "Zeit", r: true, fmt: (r) => ago(r.closed_at) },
          ]} />
        </Card>
        <Card title="Lerntagebuch" sub="was er gesehen, gelernt und noch nicht verstanden hat">
          <Diary items={d.diary} />
        </Card>
      </div>
      <div className="help">
        Ausstiegspläne: {d.plans.map((p) => `${p.label} (${p.text})`).join(" · ")}.
        {a.started_at ? ` Läuft seit ${dt(a.started_at)}.` : ""}{a.model_saved_at ? ` Modell gespeichert ${ago(a.model_saved_at)}.` : ""}
      </div>
    </div>
  );
}
