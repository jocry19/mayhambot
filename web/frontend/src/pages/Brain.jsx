import { useState } from "react";
import { LoadBar } from "./System.jsx";
import { EChart, baseOption, timeAxis, tipRow, valueAxis } from "../chart.jsx";
import { ago, dt, dur, n, pct, pctSigned, sign, sol, solSigned, usePoll } from "../lib.js";
import { Callout, Card, Empty, Seg, Table, Tile } from "../ui.jsx";

const shortMint = (m) => (m ? `${m.slice(0, 4)}…${m.slice(-4)}` : "–");
const CoinLink = ({ r }) => (
  <a href={`#/coin/${r.mint}`} className="b" onClick={(e) => e.stopPropagation()}>{r.symbol || shortMint(r.mint)}</a>
);

function EquityChart({ eq, start }) {
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
          markLine: { silent: true, symbol: "none", lineStyle: { color: c.muted, type: "dashed", width: 1 },
            label: { color: c.muted, formatter: "Start", position: "insideEndTop" }, data: [{ yAxis: start }] },
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
      <div className="tiles">
        <Tile label="Equity (Papier)" value={sol(a.equity_sol, 3)} sub={`Start ${sol(a.start_sol, 1)} · ${n(a.open)} offen, ${sol(a.open_value_sol, 2)} im Markt`} />
        <Tile label="Ergebnis" value={solSigned(a.result_sol, 3)} cls={sign(a.result_sol)}
          sub={a.closed ? `${n(a.closed)} Trades · ${pct(a.win_rate)} im Plus` : "noch kein Trade abgeschlossen"} />
        <Tile label="Entscheidungen 24 h" value={n(l.n)} sub={`${n(l.entered)} gekauft · ${n(l.explored)} zum Erkunden · ${n(l.blocked)} von Grenzen gestoppt`} />
        <Tile label="Nachgeprüft" value={`${n(h.n_taken)} / ${n(h.target)}`} sub={<LoadBar v={h.progress} />} title="genommene Gelegenheiten mit bekanntem Ergebnis, Ziel 100" />
      </div>

      <div className="grid g-wide-left">
        <Card title="Equity über Zeit" sub="Guthaben plus offene Positionen zum letzten Preis, jede Minute">
          <EquityChart eq={d.equity} start={a.start_sol} />
        </Card>
        <Card title="Ehrliche Kennzahl" sub="Ø netto je Gelegenheit: genommen, ausgelassen, alles kopiert">
          <Honest h={h} />
          {h.by_kind?.length > 0 && (
            <div className="kvs" style={{ marginTop: 10 }}>
              {h.by_kind.map((k) => (
                <div key={k.kind}><span className="dim">{k.label} ({n(k.n_taken)} von {n(k.n)})</span>
                  <b className={sign(k.taken_mean)}>{pctSigned(k.taken_mean, 1)}</b>
                  <span className="dim"> gegen {pctSigned(k.copy_all_mean, 1)}</span></div>
              ))}
            </div>
          )}
        </Card>
      </div>

      <Card title="Offene Positionen" sub="mit dem Plan, den er beim Kauf gewählt hat" flush>
        <Table rows={d.open} rowKey={(r) => r.id} empty="Keine offene Position" cols={[
          { k: "symbol", l: "Coin", fmt: (r) => <CoinLink r={r} /> },
          { k: "kind_label", l: "Quelle" },
          { k: "plan_label", l: "Plan", fmt: (r) => <span title={`Ziel +${n(r.take_profit * 100)} %, Stop −${n(r.stop_loss * 100)} %, max. ${n(r.max_hold_min)} min`}>{r.plan_label}</span> },
          { k: "size_sol", l: "Einsatz", r: true, fmt: (r) => sol(r.size_sol, 2) },
          { k: "unrealized_pct", l: "Jetzt", r: true, fmt: (r) => <b className={sign(r.unrealized_pct)}>{pctSigned(r.unrealized_pct, 1)}</b> },
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
            { k: "exit_label", l: "Ausstieg" },
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
