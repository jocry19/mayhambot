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
const hhmm = (ts) => { const d = new Date(ts * 1000); return `${String(d.getHours()).padStart(2, "0")}:00`; };
const fx = (v, d = 2) => (v == null ? "–" : `×${n(v, d)}`);
const regimeText = (x) => `Regime gewechselt: die letzten ${n(x.n)} Ergebnisse lagen Ø ${pctSigned(x.err, 1)} neben der Vorhersage, altes Wissen halbiert`;

function EquityChart({ eq, start, regimes = [], split = null }) {
  if (!eq || eq.length < 2) return <Empty>Noch keine Equity-Kurve: der Dienst schreibt sie jede Minute, sobald er läuft</Empty>;
  return (
    <EChart height={220} deps={[eq]} ariaLabel="Equity des Entscheider-Papierkontos"
      build={(c) => baseOption(c, {
        tooltip: {
          ...baseOption(c).tooltip,
          formatter: (ps) => {
            const p = eq[ps[0].dataIndex];
            return `<b>${dt(p.ts)}</b>${tipRow(c.s[0], "Leistungskonto", sol(p.equity_sol, 3))}${tipRow("transparent", "davon offen", sol(p.open_value_sol, 3))}${p.learn_equity_sol != null ? tipRow(c.s[1], "Lernkonto", sol(p.learn_equity_sol, 3)) : ""}`;
          },
        },
        xAxis: timeAxis(c), yAxis: valueAxis(c, (v) => n(v, 2), { scale: true }),
        series: [{
          type: "line", showSymbol: false, lineStyle: { width: 2, color: c.s[0] }, itemStyle: { color: c.s[0] },
          data: eq.map((p) => [p.ts * 1000, p.equity_sol]),
          markLine: { symbol: "none", lineStyle: { color: c.muted, type: "dashed", width: 1 },
            label: { color: c.muted, formatter: "Start", position: "insideEndTop" },
            tooltip: { trigger: "item", formatter: (p) => p.data.tip || "" },
            data: [{ yAxis: start }, ...(split?.at && split.at * 1000 >= eq[0].ts * 1000 ? [{
              xAxis: split.at * 1000, tip: `<b>${dt(split.at)}</b><br/>Lernkonto abgetrennt: ${sol(split.moved_sol, 2)} umgebucht (${n(split.positions)} Positionen), kein Verlust`,
              lineStyle: { color: c.muted, type: "solid", width: 1 }, label: { color: c.muted, formatter: "Trennung", position: "insideEndTop" },
            }] : []), ...regimes.filter((x) => x.at * 1000 >= eq[0].ts * 1000).map((x) => ({
              xAxis: x.at * 1000, tip: `<b>${dt(x.at)}</b><br/>${regimeText(x)}`,
              lineStyle: { color: c.warn, type: "solid", width: 1 }, label: { color: c.warn, formatter: "Regime", position: "insideEndTop" },
            }))] },
        }],
      })} />
  );
}

/** The learning account's own equity (from 0 at the split), its own scale: on one axis with the main account it
 *  would flatten both lines. */
function LearnEquity({ eq }) {
  const pts = (eq || []).filter((p) => p.learn_equity_sol != null);
  if (pts.length < 2) return null;
  return (
    <>
      <div className="help" style={{ marginTop: 8 }}>Lernkonto, eigenes Buch seit der Trennung</div>
      <EChart height={130} deps={[eq]} ariaLabel="Equity des Lernkontos"
        build={(c) => baseOption(c, {
          tooltip: { ...baseOption(c).tooltip, formatter: (ps) => { const p = pts[ps[0].dataIndex]; return `<b>${dt(p.ts)}</b>${tipRow(c.s[1], "Lernkonto", sol(p.learn_equity_sol, 3))}`; } },
          xAxis: timeAxis(c), yAxis: valueAxis(c, (v) => n(v, 2), { scale: true, splitNumber: 2 }),
          series: [{ type: "line", showSymbol: false, lineStyle: { width: 2, color: c.s[1] }, itemStyle: { color: c.s[1] }, data: pts.map((p) => [p.ts * 1000, p.learn_equity_sol]) }],
        })} />
    </>
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
            return `<b>${dayLabel(x.ts)} (UTC-Tag)</b>${tipRow(x.mult >= 1 ? c.good : c.bad, "Tagesfaktor", fx(x.mult))}${tipRow("transparent", "Konto", `${sol(x.start_sol, 3)} → ${sol(x.end_sol, 3)}`)}${x.total_mult != null ? tipRow("transparent", "beide Konten", `${fx(x.total_mult)} (${sol(x.total_start_sol, 3)} → ${sol(x.total_end_sol, 3)})`) : ""}${tipRow("transparent", "Ziel", `×${lo} bis ×${hi}`)}${reg}`;
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

/** Patterns (plan 18.10., "Richtung neu"): estimate and uncertainty per pattern; the release badge only while brain still sends one. */
const pauseSince = (g) => {
  if (g.decided_at) return <>Hauptkonto pausiert seit {dt(g.decided_at)} (Entscheidung des Nutzers){g.live_since && g.live_since > g.decided_at + 60 ? `, wirksam seit ${dt(g.live_since)}` : ""}</>;
  if (g.live_since) return <>Hauptkonto pausiert seit {dt(g.live_since)} (vom Prüfstand ausgelöst)</>;
  return "Hauptkonto pausiert seit 18.10. 17:06 (Entscheidung des Nutzers)";
};
const VERDICT = { traegt: ["good", "trägt"], kandidat: ["warn", "Kandidat"], "traegt nicht": ["", "trägt nicht"] };
const thirdsCell = (x) => (
  <span className="row mono" style={{ gap: 6, justifyContent: "flex-end" }} title="Ø netto je Zeitdrittel des Fensters, alt nach neu">
    {(x.thirds || []).map((v, i) => <span key={i} className={v > 0 ? "up" : v < 0 ? "down" : "dim"}>{pctSigned(v, 1)}</span>)}
  </span>
);

function Pruefstand() {
  const [all, setAll] = useState("top");
  const { data: L } = usePoll("/api/lab", 60000);
  if (!L) return null;
  if (!L.available) return <Card title="Prüfstand"><Empty>{L.note}</Empty></Card>;
  const r = L.rule || {};
  const shown = all === "all" ? L.results : L.results.filter((x) => x.verdict !== "traegt nicht").concat(L.results.filter((x) => x.verdict === "traegt nicht").slice(0, 5));
  return (
    <Card flush title="Prüfstand"
      sub={`Feste Hypothesen auf der echten Datenbank, alle ${n(L.every_h)} h über die letzten ${n(L.window_h)} h nachgerechnet, nach Kosten bei ${sol(L.stake_sol, 2)} Einsatz (Börsengebühr, Preiswirkung, Transaktionskosten, Verzug). „Trägt“ heißt: in allen drei Zeitdritteln positiv, n ≥ ${n(r.min_n)}, ≥ ${n(r.min_coins)} Coins und t ≥ ${n(r.min_t)} über die Coin-Mittel. Nur eine tragende Tasche soll Einsatz im Hauptkonto bekommen.${L.at ? ` Stand ${ago(L.at)}.` : ""}`}
      actions={<Seg value={all} onChange={setAll} options={[["top", "Tragend und Kandidaten"], ["all", `Alle ${n(L.n)}`]]} />}>
      <div style={{ padding: "0 16px" }}>
        {L.stale && <Callout kind="warn" icon="alert">Der letzte Bericht ist {n(L.age_h, 1)} h alt, der Prüfstand rechnet sonst alle {n(L.every_h)} h: läuft der Dienst lab?</Callout>}
        {Object.keys(L.errors || {}).length > 0 && <Callout kind="warn" icon="alert">Nicht berechnet: {Object.entries(L.errors).map(([k, v]) => `${k} (${v})`).join("; ")}</Callout>}
        <div className="tiles" style={{ margin: "8px 0 12px" }}>
          <Tile label="Tragen" value={n(L.carries)} cls={L.carries > 0 ? "up" : ""} sub={L.carries > 0 ? L.carrying.join(", ") : "keine Hypothese trägt nach Kosten"} />
          <Tile label="Kandidaten" value={n(L.candidates)} sub="im Mittel positiv, zwei von drei Dritteln" />
          <Tile label="Geprüft" value={n(L.n)} sub={`${n(L.coins)} Coins mit Kursreihe`} />
        </div>
      </div>
      <Table rows={shown} rowKey={(x) => x.hyp}
        empty="Der Bericht enthält noch keine Hypothese mit Einstiegen"
        expand={(x) => <div className="col" style={{ gap: 4 }}><span className="mono">{x.hyp}</span>
          <span className="dim">Ø brutto {pctSigned(x.gross, 1)} · Kosten {pct(x.cost, 1)} · netto je Einstieg {pctSigned(x.net, 1)} · Standardfehler {pct(x.se, 1)} · {dt(x.from)} bis {dt(x.to)}</span></div>}
        cols={[
          { k: "hyp", l: "Hypothese", fmt: (x) => <span><b className="mono">{x.hyp}</b> <span className="dim">{x.family}</span></span> },
          { k: "verdict", l: "Urteil", fmt: (x) => <span className={`badge ${(VERDICT[x.verdict] || [""])[0]}`}>{(VERDICT[x.verdict] || ["", x.verdict])[1]}</span> },
          { k: "net_per_coin", l: "netto je Coin", r: true, fmt: (x) => <b className={sign(x.net_per_coin)}>{pctSigned(x.net_per_coin, 1)}</b> },
          { k: "t", l: "t", r: true, fmt: (x) => <span className={x.t >= (r.min_t || 3) ? "up b" : "dim"}>{x.t == null ? "–" : n(x.t, 1)}</span> },
          { k: "thirds_pos", l: "Drittel", r: true, fmt: thirdsCell },
          { k: "n", l: "Einstiege", r: true, fmt: (x) => <span className={x.n >= (r.min_n || 200) ? "" : "dim"}>{n(x.n)}</span> },
          { k: "coins", l: "Coins", r: true, fmt: (x) => <span className={x.coins >= (r.min_coins || 50) ? "" : "dim"}>{n(x.coins)}</span> },
          { k: "cost", l: "Kosten", r: true, fmt: (x) => pct(x.cost, 1) },
          { k: "rug", l: "Rug", r: true, fmt: (x) => pct(x.rug, 0) },
        ]} />
    </Card>
  );
}

function Pockets({ p }) {
  const r = p.rule || {};
  const status = (x) => (x.released ? <span className="badge good">freigegeben</span>
    : x.demoted_at ? <span className="badge bad" title={`zurückgestuft ${dt(x.demoted_at)}`}>zurückgestuft</span>
      : (x.n || 0) < (r.min_n || 100) ? <span className="badge">sammelt</span> : <span className="badge warn">nicht frei</span>);
  return (
    <Card flush title="Muster" sub={`Verteilung des Netto-Ergebnisses je Muster und Quelle nach Kosten${r.window_days ? ` über ${n(r.window_days)} Tage` : ""}: Schätzung und ${pct(r.conf ?? 0.95)}-Untergrenze, getrennt nach älterer und neuerer Hälfte. Vertrauen = Wahrscheinlichkeit, dass der Schnitt über 0 liegt; der Einsatz gleitet damit, ein Tor gibt es nicht mehr.${p.at ? ` Stand ${ago(p.at)}.` : ""}`}
      actions={<span className="dim" style={{ fontSize: 12 }}>{n(p.rows?.length)} Muster · {n(p.trusted)} mit Vertrauen ≥ 80 %</span>}>
      <Table rows={p.rows} rowKey={(x) => x.key} initialSort={{ k: "trust", d: "desc" }}
        empty="Noch keine Muster berechnet (sie erscheinen ab 20 Fällen, der Entscheider rechnet alle 10 Minuten)"
        expand={(x) => <div className="col" style={{ gap: 4 }}><span>{x.reason || "keine Begründung"}</span>{x.released != null && <span>Etikett: {status(x)}</span>}
          {x.released_at && <span className="dim">freigegeben {dt(x.released_at)}</span>}{x.demoted_at && <span className="dim">zurückgestuft {dt(x.demoted_at)}</span>}
          <span className="dim mono">{x.key}</span></div>}
        cols={[
          { k: "label", l: "Muster", fmt: (x) => <span><b>{x.label}</b> <span className="dim">{x.source_label}</span></span> },
          { k: "trust", l: "Vertrauen", r: true, fmt: (x) => (x.trust == null ? <span className="dim">–</span>
            : <span className="row" style={{ gap: 8, justifyContent: "flex-end" }} title="Wahrscheinlichkeit, dass der Schnitt nach Kosten über 0 liegt (neuere Hälfte doppelt gewichtet); der Einsatz gleitet damit">
              <span style={{ width: 56, height: 6, borderRadius: 3, background: "var(--border)", overflow: "hidden", display: "inline-block" }}><span style={{ display: "block", height: "100%", width: `${x.trust * 100}%`, background: "var(--series-1)" }} /></span><b className={x.trust >= 0.8 ? "up" : x.trust < 0.5 ? "down" : ""}>{pct(x.trust, 0)}</b></span>) },
          { k: "n", l: "Fälle", r: true, fmt: (x) => n(x.n) },
          { k: "mean", l: "Ø netto", r: true, fmt: (x) => <b className={sign(x.mean)}>{pctSigned(x.mean, 1)}</b> },
          { k: "lower95", l: "Untergrenze 95 %", r: true, fmt: (x) => <span className={x.lower95 > 0 ? "up b" : "dim"}>{pctSigned(x.lower95, 1)}</span> },
          { k: "mean_first_half", l: "1. Hälfte", r: true, fmt: (x) => halfCell(x.mean_first_half, x.lower95_first_half, x.n_first_half) },
          { k: "mean_second_half", l: "2. Hälfte", r: true, fmt: (x) => halfCell(x.mean_second_half, x.lower95_second_half, x.n_second_half) },
          { k: "last50_mean", l: `Letzte ${n(r.last_n)}`, r: true, fmt: (x) => <span className={sign(x.last50_mean)}>{pctSigned(x.last50_mean, 1)}</span> },
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
        {d.size_sol != null && d.decision === "enter" && <span>Einsatz <b>{sol(d.size_sol, 2)}</b>{d.capped_from != null && d.capped_from > d.size_sol && <span className="dim" title="Hauptkonto: höchstens 3 % des Kontos je Trade"> (gekappt von {sol(d.capped_from, 2)})</span>}</span>}
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

/** Learning curve (plan 18.10., "Richtung neu"): is the decider getting better day by day, measured out of sample. */
function Learning({ L, days }) {
  const ds = L.days || [];
  const scored = (x) => Math.max(x.n_scored || 0, x.n_label || 0);  // labels cover every opportunity, not only buys
  const sc = ds.filter((x) => scored(x) > 0);
  const last = sc[sc.length - 1] || ds[ds.length - 1] || {};
  const ic = ds.filter((x) => x.rank_ic != null || x.label_ic != null);
  const tr = ds.filter((x) => x.trade_mean_7d != null || x.learn_trade_mean_7d != null);
  const lastTr = tr[tr.length - 1];
  const dl = (x) => dayLabel(x.ts);
  const firstName = L.source === "brain_opps" ? "Gelegenheiten des Entscheiders (Ergebnis des Ausstiegsplans)" : "Käufe (Ergebnis nach Verkauf)";
  return (
    <Card title="Lernkurve" sub={`Wird der Entscheider besser? Jede Zahl vergleicht die Vorhersage zum Zeitpunkt der Entscheidung mit dem später gemessenen Ergebnis, also außerhalb der Trainingsdaten. Quelle: ${L.source === "opportunities" ? "gemeinsame Gelegenheits-Tabelle aller Quellen" : L.seen_source === "opportunities" ? "Vorhersagen des Entscheiders (die gemeinsame Tabelle hat noch keine bewerteten Vorhersagen)" : "Gelegenheiten des Entscheiders"}.`}>
      <div className="tiles wrap-sub" style={{ marginBottom: 12 }}>
        <Tile label="Rangkorrelation" value={L.rank_ic == null ? "–" : n(L.rank_ic, 2)} cls={sign(L.rank_ic)}
          sub={<>{firstName}, letzter Tag mit ≥ 10 Fällen; 0 = rät, 1 = perfekte Reihenfolge
            {L.labels && <><br />Alle Gelegenheiten (1 h verfolgt): <b className={sign(L.label_ic)}>{L.label_ic == null ? "–" : n(L.label_ic, 2)}</b> · {n(L.label_n)} Fälle</>}</>} />
        <Tile label="Netto je Trade, 7 Tage" value={pctSigned(lastTr?.trade_mean_7d, 1)} cls={sign(lastTr?.trade_mean_7d)}
          sub={lastTr ? <>Leistungskonto, 95 %: {pctSigned(lastTr.trade_lo_7d, 1)} bis {pctSigned(lastTr.trade_hi_7d, 1)} · {n(lastTr.n_trades_7d)} Trades
            <br />Lernkonto: <span className={sign(lastTr.learn_trade_mean_7d)}>{pctSigned(lastTr.learn_trade_mean_7d, 1)}</span> · {n(lastTr.learn_n_trades_7d)} Trades</> : "noch kein Trade abgeschlossen"} />
        <Tile label="Kalibrierung, 7 Tage" value={L.cal_gap_7d == null ? "–" : `±${pct(L.cal_gap_7d, 1)}`}
          sub={`Ø Abstand Vorhersage zu Ergebnis je Zehntel · ${n(L.n_7d)} Fälle`} />
        <Tile label={last.ts ? `Bewertet am ${dayLabel(last.ts)}` : "Bewertet je Tag"} value={`${n(scored(last))} / ${n(L.opps_goal)}`}
          sub={<LoadBar v={Math.min(1, scored(last) / (L.opps_goal || 1))} />} title={`${n(last.n_opps)} Gelegenheiten an diesem Tag gesehen; das Ergebnis steht erst nach dem längsten Plan fest. Ziel ${n(L.opps_goal)} bewertete am Tag`} />
      </div>
      <div className="grid g2">
        <div>
          <div className="help">Rangkorrelation je Entscheidungstag</div>
          {ic.length ? <EChart height={180} deps={[L]} ariaLabel="Rangkorrelation je Tag"
            build={(c) => baseOption(c, {
              tooltip: { ...baseOption(c).tooltip, axisPointer: { type: "shadow" },
                formatter: (ps) => { const x = ic[ps[0].dataIndex]; return `<b>${dl(x)}</b>${tipRow(c.s[0], L.source === "brain_opps" ? "Plan-Ergebnis" : "Käufe (Ergebnis)", `${x.rank_ic == null ? "–" : n(x.rank_ic, 3)} · ${n(x.n_scored)} Fälle`)}${L.labels ? tipRow(c.s[1], "alle Gelegenheiten (1 h)", `${x.label_ic == null ? "–" : n(x.label_ic, 3)} · ${n(x.n_label)} Fälle`) : ""}`; } },
              xAxis: catAxis(c, ic.map(dl)), yAxis: valueAxis(c, (v) => n(v, 1), { min: (v) => Math.min(-0.2, v.min), max: (v) => Math.max(0.4, v.max) }),
              series: [["rank_ic", 0], ...(L.labels ? [["label_ic", 1]] : [])].map(([k, i]) => ({ type: "bar", barMaxWidth: 14, barGap: "20%",
                data: ic.map((x) => ({ value: x[k], itemStyle: { color: c.s[i], borderRadius: (x[k] ?? 0) >= 0 ? [3, 3, 0, 0] : [0, 0, 3, 3] } })),
                ...(i === 0 ? { markLine: { silent: true, symbol: "none", lineStyle: { color: c.muted, type: "dashed", width: 1 }, label: { show: false }, data: [{ yAxis: 0 }] } } : {}) })),
            })} /> : <Empty>Noch kein Tag mit 10 nachgeprüften Vorhersagen</Empty>}
          {ic.length > 0 && L.labels && <div className="legend-row">{[firstName, "alle Gelegenheiten (1 h verfolgt)"].map((l, i) => <span key={l}><i style={{ background: `var(--series-${i + 1})` }} />{l}</span>)}</div>}
        </div>
        <div>
          <div className="help">Netto je Trade, gleitend 7 Tage: Leistungskonto mit 95 %-Band, Lernkonto getrennt</div>
          {tr.length ? <EChart height={180} deps={[L]} ariaLabel="Netto je Trade, 7-Tage-Mittel mit Unsicherheit"
            build={(c) => baseOption(c, {
              tooltip: { ...baseOption(c).tooltip, formatter: (ps) => { const x = tr[ps[0].dataIndex]; return `<b>${dl(x)}</b>${tipRow(c.s[0], "Leistungskonto Ø 7 Tage", pctSigned(x.trade_mean_7d, 1))}${tipRow("transparent", "95 %", `${pctSigned(x.trade_lo_7d, 1)} bis ${pctSigned(x.trade_hi_7d, 1)}`)}${tipRow("transparent", "Trades", `${n(x.n_trades_7d)} (an diesem Tag ${n(x.n_trades)})`)}${tipRow(c.s[1], "Lernkonto Ø 7 Tage", pctSigned(x.learn_trade_mean_7d, 1))}${tipRow("transparent", "Trades", `${n(x.learn_n_trades_7d)} (an diesem Tag ${n(x.learn_n_trades)})`)}`; } },
              xAxis: catAxis(c, tr.map(dl), { boundaryGap: false }), yAxis: valueAxis(c, (v) => pctSigned(v, 0)),
              series: [
                { type: "line", stack: "band", symbol: "none", lineStyle: { opacity: 0 }, data: tr.map((x) => x.trade_lo_7d ?? x.trade_mean_7d) },
                { type: "line", stack: "band", symbol: "none", lineStyle: { opacity: 0 }, areaStyle: { color: c.s[0], opacity: 0.15 }, data: tr.map((x) => (x.trade_hi_7d ?? x.trade_mean_7d) - (x.trade_lo_7d ?? x.trade_mean_7d)) },
                { type: "line", symbolSize: 8, lineStyle: { width: 2, color: c.s[1] }, itemStyle: { color: c.s[1] }, connectNulls: true, data: tr.map((x) => x.learn_trade_mean_7d) },
                { type: "line", symbolSize: 8, lineStyle: { width: 2, color: c.s[0] }, itemStyle: { color: c.s[0] }, connectNulls: true, data: tr.map((x) => x.trade_mean_7d),
                  markLine: { silent: true, symbol: "none", lineStyle: { color: c.muted, type: "dashed", width: 1 }, label: { color: c.muted, formatter: "0", position: "insideEndTop" }, data: [{ yAxis: 0 }] } },
              ],
            })} /> : <Empty>Noch kein abgeschlossener Trade</Empty>}
          {tr.length > 0 && <div className="legend-row">{["Leistungskonto", "Lernkonto"].map((l, i) => <span key={l}><i style={{ background: `var(--series-${i + 1})` }} />{l}</span>)}</div>}
        </div>
        <div>
          <div className="help">Kalibrierung je Zehntel der Vorhersage, letzte 7 Tage (Ergebnis gedeckelt bei {pctSigned(L.cap?.[1], 0)})</div>
          {L.deciles?.length ? <><EChart height={180} deps={[L]} ariaLabel="Vorhergesagtes gegen tatsächliches Netto je Zehntel"
            build={(c) => baseOption(c, {
              tooltip: { ...baseOption(c).tooltip, formatter: (ps) => { const x = L.deciles[ps[0].dataIndex]; return `<b>${x.decile}. Zehntel</b>${tipRow(c.s[0], "vorhergesagt", pctSigned(x.pred, 1))}${tipRow(c.s[1], "tatsächlich", pctSigned(x.real, 1))}${tipRow("transparent", "Fälle", n(x.n))}`; } },
              xAxis: catAxis(c, L.deciles.map((x) => `${x.decile}.`), { boundaryGap: false }), yAxis: valueAxis(c, (v) => pctSigned(v, 0)),
              series: [["pred", "vorhergesagt"], ["real", "tatsächlich"]].map(([k, l], i) => ({ name: l, type: "line", symbolSize: 8, lineStyle: { width: 2, color: c.s[i] }, itemStyle: { color: c.s[i] }, data: L.deciles.map((x) => x[k]) })),
            })} />
            <div className="legend-row">{["vorhergesagt", "tatsächlich"].map((l, i) => <span key={l}><i style={{ background: `var(--series-${i + 1})` }} />{l}</span>)}</div></>
            : <Empty>Erst ab 50 nachgeprüften Vorhersagen in 7 Tagen</Empty>}
        </div>
        <div>
          <div className="help">Gelegenheiten je Tag: gesehen und mit Ergebnis bewertet (log, Ziel {n(L.opps_goal)})</div>
          <EChart height={180} deps={[L]} ariaLabel="Gelegenheiten je Tag"
            build={(c) => baseOption(c, {
              tooltip: { ...baseOption(c).tooltip, axisPointer: { type: "shadow" }, formatter: (ps) => { const x = ds[ps[0].dataIndex]; return `<b>${dl(x)}</b>${tipRow(c.s[0], "gesehen", n(x.n_opps))}${tipRow(c.s[1], "bewertet", n(scored(x)))}${tipRow("transparent", "Tagesfaktor", fx(x.mult))}`; } },
              xAxis: catAxis(c, ds.map(dl)), yAxis: valueAxis(c, (v) => n(v), { type: "log", min: 1, max: (v) => Math.max((L.opps_goal || 1) * 1.5, v.max) }),
              series: [
                { name: "gesehen", type: "bar", barMaxWidth: 10, barGap: "20%", data: ds.map((x) => (x.n_opps > 0 ? x.n_opps : null)), itemStyle: { color: c.s[0], borderRadius: [3, 3, 0, 0] } },
                { name: "bewertet", type: "bar", barMaxWidth: 10, data: ds.map((x) => (scored(x) > 0 ? scored(x) : null)), itemStyle: { color: c.s[1], borderRadius: [3, 3, 0, 0] },
                  markLine: { silent: true, symbol: "none", lineStyle: { color: c.good, width: 1 }, label: { color: c.good, formatter: `Ziel ${n(L.opps_goal)}`, position: "insideEndTop" }, data: [{ yAxis: L.opps_goal }] } },
              ],
            })} />
          <div className="legend-row">{["gesehen", "bewertet"].map((l, i) => <span key={l}><i style={{ background: `var(--series-${i + 1})` }} />{l}</span>)}</div>
        </div>
      </div>
      {L.label_hours?.length > 0 && (
        <div style={{ marginTop: 12 }}>
          <div className="help">Labels je Stunde, letzte 24 h: jede Gelegenheit 1 h nach der Meldung bewertet, gekauft oder nicht (Balken = Anzahl, Linie = Rangkorrelation der Vorhersage mit dem 1-h-Ergebnis)</div>
          <div className="grid g2">
            <EChart height={150} deps={[L]} ariaLabel="Gelabelte Gelegenheiten je Stunde"
              build={(c) => baseOption(c, {
                tooltip: { ...baseOption(c).tooltip, axisPointer: { type: "shadow" }, formatter: (ps) => { const x = L.label_hours[ps[0].dataIndex]; return `<b>${hhmm(x.ts)}</b>${tipRow(c.s[1], "gelabelt", n(x.n))}${tipRow("transparent", "davon Rug", n(x.rug))}${tipRow("transparent", "Ø 1-h-Ergebnis", pctSigned(x.mean_ret60, 1))}`; } },
                xAxis: catAxis(c, L.label_hours.map((x) => hhmm(x.ts))), yAxis: valueAxis(c, (v) => n(v)),
                series: [{ type: "bar", barMaxWidth: 12, data: L.label_hours.map((x) => x.n), itemStyle: { color: c.s[1], borderRadius: [3, 3, 0, 0] } }],
              })} />
            <EChart height={150} deps={[L]} ariaLabel="Rangkorrelation je Stunde"
              build={(c) => baseOption(c, {
                tooltip: { ...baseOption(c).tooltip, formatter: (ps) => { const x = L.label_hours[ps[0].dataIndex]; return `<b>${hhmm(x.ts)}</b>${tipRow(c.s[1], "Rangkorrelation", x.ic == null ? "–" : n(x.ic, 3))}${tipRow("transparent", "Fälle", n(x.n))}`; } },
                xAxis: catAxis(c, L.label_hours.map((x) => hhmm(x.ts)), { boundaryGap: false }), yAxis: valueAxis(c, (v) => n(v, 1), { min: (v) => Math.min(-0.2, v.min), max: (v) => Math.max(0.4, v.max) }),
                series: [{ type: "line", connectNulls: true, symbolSize: 6, lineStyle: { width: 2, color: c.s[1] }, itemStyle: { color: c.s[1] }, data: L.label_hours.map((x) => x.ic),
                  markLine: { silent: true, symbol: "none", lineStyle: { color: c.muted, type: "dashed", width: 1 }, label: { show: false }, data: [{ yAxis: 0 }] } }],
              })} />
          </div>
        </div>
      )}
      {L.discrimination?.length > 0 && (
        <div style={{ marginTop: 12 }}>
          <div className="help">Trennschärfe je Quelle und der Einsatzfaktor daraus: der Entscheider setzt nur so viel, wie seine Vorhersagen für diese Quelle über 7 Tage Gewinner von Verlierern getrennt haben, gemessen am Ergebnis des gewählten Ausstiegsplans (ab {n(L.disc_rule?.min_n)} Ergebnissen, voll ab einer Rangkorrelation von {n(L.disc_rule?.full, 1)}; bei 0 bleibt der kleine Einsatz). * = nachgerechnet, solange der Entscheider den Wert nicht meldet</div>
          <Table rows={L.discrimination} rowKey={(x) => x.kind} initialSort={{ k: "n", d: "desc" }}
            cols={[
              { k: "label", l: "Quelle", fmt: (x) => <b>{x.label}</b> },
              { k: "n", l: "Ergebnisse", r: true, fmt: (x) => n(x.n) },
              { k: "rank_corr", l: "Rangk. gegen Plan-Ergebnis", r: true, fmt: (x) => <b className={sign(x.rank_corr)} title={x.from_brain ? "vom Entscheider gemeldet" : "aus den Gelegenheiten des Entscheiders nachgerechnet"}>{x.rank_corr == null ? "–" : n(x.rank_corr, 2)}{x.from_brain ? "" : "*"}</b> },
              { k: "factor", l: "Einsatzfaktor", r: true, fmt: (x) => (
                <span className="row" style={{ gap: 8, justifyContent: "flex-end" }} title={x.factor > 0 ? `Einsatz zu ${pct(x.factor, 0)} aus dem Vertrauen` : x.n < (L.disc_rule?.min_n || 30) ? "noch zu wenige Ergebnisse: kleiner Einsatz" : "trennt noch nicht: kleiner Einsatz"}>
                  <span style={{ width: 56, height: 6, borderRadius: 3, background: "var(--border)", overflow: "hidden", display: "inline-block" }}><span style={{ display: "block", height: "100%", width: `${x.factor * 100}%`, background: "var(--series-1)" }} /></span>
                  <b>{pct(x.factor, 0)}</b></span>) },
              { k: "why", l: "", fmt: (x) => <span className="dim">{x.factor >= 1 ? "voller Einsatz möglich" : x.factor > 0 ? "Einsatz gedrosselt" : x.n < (L.disc_rule?.min_n || 30) ? "sammelt noch, kleiner Einsatz" : "trennt nicht, kleiner Einsatz"}</span> },
              { k: "shared_rank_ic", l: "gemeinsame Tabelle", r: true, fmt: (x) => (x.shared_rank_ic == null ? <span className="dim">–</span>
                : <span className={sign(x.shared_rank_ic)} title={`${n(x.shared_n)} Fälle in der Gelegenheits-Tabelle`}>{n(x.shared_rank_ic, 2)}</span>) },
            ]} />
        </div>
      )}
      {L.sources?.length > 0 && (
        <div style={{ marginTop: 12 }}>
          <div className="help">Gelegenheiten je Quelle, letzte 7 Tage (Netto nach Kosten, ehrlich nachgespielt, Rug = −100 %)</div>
          <Table rows={L.sources} rowKey={(x) => x.source} initialSort={{ k: "n_7d", d: "desc" }}
            cols={[
              { k: "label", l: "Quelle", fmt: (x) => <b>{x.label}</b> },
              { k: "n_24h", l: "24 h", r: true, fmt: (x) => n(x.n_24h) },
              { k: "n_7d", l: "7 Tage", r: true, fmt: (x) => n(x.n_7d) },
              { k: "n_eval", l: "mit Ergebnis", r: true, fmt: (x) => n(x.n_eval) },
              { k: "mean_net", l: "Ø netto", r: true, fmt: (x) => <b className={sign(x.mean_net)}>{pctSigned(x.mean_net, 1)}</b> },
              { k: "mean_source", l: "eigene Messung", r: true, fmt: (x) => (x.n_source ? <span className={sign(x.mean_source)} title={`${n(x.n_source)} Fälle, von der Quelle selbst gemessen`}>{pctSigned(x.mean_source, 1)}</span> : <span className="dim">–</span>) },
              { k: "rank_ic", l: "Rangkorrelation", r: true, fmt: (x) => (x.rank_ic == null ? <span className="dim">–</span> : <span className={sign(x.rank_ic)} title={`${n(x.n_pred)} Vorhersagen mit Ergebnis`}>{n(x.rank_ic, 2)}</span>) },
              ...(L.labels ? [
                { k: "label_ic", l: "Rangk. alle (1 h)", r: true, fmt: (x) => (x.label_ic == null ? <span className="dim">–</span> : <span className={sign(x.label_ic)} title={`${n(x.n_label)} Gelegenheiten 1 h verfolgt`}>{n(x.label_ic, 2)} <span className="dim">({n(x.n_label)})</span></span>) },
                { k: "rug_rate", l: "Rug-Quote", r: true, fmt: (x) => (x.rug_rate == null ? <span className="dim">–</span> : <span className={x.rug_rate > 0.2 ? "down" : ""} title={`${n(x.n_rug)} verfolgt; Tief unter 10 % des Einstiegs in der ersten Stunde`}>{pct(x.rug_rate, 1)}</span>) },
              ] : []),
              { k: "taken", l: "gekauft", r: true, fmt: (x) => n(x.taken) },
            ]} />
        </div>
      )}
    </Card>
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
        <Tile label="Leistungskonto (Papier)" value={sol(a.equity_sol, 3)} sub={`Start ${sol(a.start_sol, 1)} · ${n(a.open)} offen, ${sol(a.open_value_sol, 2)} im Markt`} />
        <Tile label="Ergebnis Leistungskonto" value={solSigned(a.result_sol, 3)} cls={sign(a.result_sol)}
          sub={a.closed ? `${n(a.closed)} Trades · ${pct(a.win_rate)} im Plus${a.moved_sol ? ` · Umbuchung ${solSigned(a.moved_sol, 2)} nicht mitgezählt` : ""}` : "noch kein Trade abgeschlossen"} />
        {d.learn && (d.learn.closed > 0 || d.learn.open > 0 || d.learn.split) && (
          <Tile label="Lernkonto (Papier)" value={sol(d.learn.equity_sol, 3)}
            sub={<>Ergebnis <span className={sign(d.learn.result_sol)}>{solSigned(d.learn.result_sol, 3)}</span> · {n(d.learn.open)} offen · {n(d.learn.closed)} Trades{d.learn.closed ? `, ${pct(d.learn.win_rate)} im Plus` : ""}</>}
            title="kleine Einsätze auf alles, was die Ziehung durchlässt; eigenes Buch, getrennt vom Leistungskonto" />
        )}
        {d.learn && d.total?.equity_sol != null && (d.learn.closed > 0 || d.learn.open > 0 || d.learn.split) && (
          <Tile label="Beide Konten zusammen" value={sol(d.total.equity_sol, 3)}
            sub={<>Ergebnis <span className={sign(d.total.result_sol)}>{solSigned(d.total.result_sol, 3)}</span> seit Start {sol(a.start_sol, 1)}; Umbuchungen heben sich auf</>}
            title="Leistungskonto plus Lernkonto: das ehrliche Gesamtergebnis" />
        )}
        <Tile label="Entscheidungen 24 h" value={n(l.n)} sub={`${n(l.entered)} gekauft · ${n(l.explored)} zum Erkunden · ${n(l.blocked)} von Grenzen gestoppt`} />
        <Tile label="Nachgeprüft" value={`${n(h.n_taken)} / ${n(h.target)}`} sub={<LoadBar v={h.progress} />} title="genommene Gelegenheiten mit bekanntem Ergebnis, Ziel 100" />
        <Tile label="Tagesfaktor heute" value={fx(d.goal.today)} cls={d.goal.today == null ? "" : d.goal.today >= d.goal.mult[0] ? "up" : d.goal.today < 1 ? "down" : ""}
          sub={<>Leistungskonto, Umbuchung herausgerechnet{d.goal.today_total != null && <>; beide Konten zusammen <b className={d.goal.today_total < 1 ? "down" : d.goal.today_total >= d.goal.mult[0] ? "up" : ""}>{fx(d.goal.today_total)}</b></>}. Ziel ×{d.goal.mult[0]} bis ×{d.goal.mult[1]} am Tag (UTC-Tag bisher)</>} />
        <Tile label="Auswahl-Vorsprung" value={pctSigned(d.alpha, 1)} cls={sign(d.alpha)} sub="genommen minus alles kopiert, Ø je Trade, nachgeprüft" />
        <Tile label="Einfangquote" value={pct(lastOf(d.days, "capture_own") ?? lastOf(d.days, "capture_organic"))}
          sub={lastOf(d.days, "capture_own") != null ? "Anteil am besten Anstieg, eigene Trades (letzter Tag)" : "Anteil am besten Anstieg, organisch nachgespielt"} />
      </div>
      {d.main_gate && d.main_gate.mode !== "on" && (d.main_gate.paused
        ? <Callout kind="warn" icon="alert">{pauseSince(d.main_gate)}: es kauft erst wieder, wenn im Prüfstand eine Tasche das Urteil „trägt“ hat, und dann nur deren Art. {d.main_gate.report ? `Letzter Bericht ${ago(d.main_gate.report_at)}: keine tragende Tasche.` : "Der Prüfstand hat noch keinen Bericht."} Das Lernkonto handelt weiter{d.main_gate.stopped_24h ? `; ${n(d.main_gate.stopped_24h)} Hauptkonto-Käufe in 24 h deshalb gestoppt` : ""}.</Callout>
        : <Callout kind="good" icon="info">Hauptkonto kauft nur, wo der Prüfstand eine tragende Tasche sieht: {d.main_gate.open_labels.join(", ")} ({d.main_gate.carrying.join(", ")}).</Callout>)}
      {d.challenge?.paused_until && (
        <Callout kind="warn" icon="alert">Stunden-Challenge (×2 je Stunde) pausiert bis {dt(d.challenge.paused_until)}: die letzten 3 vollen Stunden zusammen lagen bei {fx(d.challenge.pauses_24h?.[d.challenge.pauses_24h.length - 1]?.factor, 3)}. Bis dahin gelten die normalen Einsatzgrößen{d.challenge.pauses_24h?.length > 1 ? ` (${n(d.challenge.pauses_24h.length)} Pausen in 24 h)` : ""}.</Callout>
      )}
      {d.regimes?.length > 0 && d.regimes[d.regimes.length - 1].at > d.now - 86400 && (
        <Callout kind="warn" icon="alert">{regimeText(d.regimes[d.regimes.length - 1])} ({ago(d.regimes[d.regimes.length - 1].at)}).</Callout>
      )}

      <div className="grid g-wide-left">
        <Card title="Equity über Zeit" sub="Guthaben plus offene Positionen zum letzten Preis, jede Minute; oben das Leistungskonto, darunter das Lernkonto">
          <EquityChart eq={d.equity} start={a.start_sol} regimes={d.regimes} split={d.learn?.split} />
          <LearnEquity eq={d.equity} />
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

      <Pruefstand />
      {d.learning && <Learning L={d.learning} days={d.days} />}
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
