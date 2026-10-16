import { useState } from "react";
import { EChart, baseOption, catAxis, tipRow, valueAxis } from "../chart.jsx";
import { dt, n, pct, pctSigned, sign, solSigned, usePoll } from "../lib.js";
import { Bars, Callout, Card, Delta, Empty, Seg, Table, Tile } from "../ui.jsx";

const FEATURE = { tier: "Wal-Tier", grade: "Modell-Note", mcap: "Market Cap", liq: "Liquidität", score: "Signal-Score",
  buyers: "Smart Wallets", size: "Wal-Kauf (SOL)", ultra: "Ultra dabei" };

export function paramText(p) {
  if (!p) return "Preset";
  const parts = [];
  if (p.take_profit != null) parts.push(`Ziel +${pct(p.take_profit)}`);
  if (p.stop_loss != null) parts.push(`Stop −${pct(p.stop_loss)}`);
  if (p.trail_activate) parts.push(`Trailing ab +${pct(p.trail_activate)} (−${pct(p.trail_drawdown)})`);
  if (p.max_hold_min != null) parts.push(`max. ${n(p.max_hold_min)} min`);
  if (p.inflow_exit) parts.push("Verkauf am Ansturm-Ende");
  if (p.delay_s) parts.push(`Einstieg +${p.delay_s} s`);
  return parts.join(" · ");
}

function blockText(b) {
  const e = Object.entries(b || {}).filter(([, v]) => v?.length);
  return e.length ? e.map(([k, v]) => `${FEATURE[k] || k}: ${v.join(", ")}`).join(" · ") : "";
}

/** Grid of mean net gain per (take profit, stop loss); diverging around 0, value printed in every cell. */
function Surface({ rows }) {
  if (!rows?.length) return <Empty>Noch keine ausgewerteten Einstiege</Empty>;
  const tps = [...new Set(rows.map((r) => r.take_profit))].sort((a, b) => a - b);
  const sls = [...new Set(rows.map((r) => r.stop_loss))].sort((a, b) => a - b);
  const at = new Map(rows.map((r) => [`${r.take_profit}|${r.stop_loss}`, r.gain]));
  const m = Math.max(0.01, ...rows.map((r) => Math.abs(r.gain)));
  const best = rows.reduce((a, b) => (b.gain > a.gain ? b : a), rows[0]);
  return (
    <div className="heat" style={{ gridTemplateColumns: `auto repeat(${tps.length}, minmax(0, 1fr))` }}>
      <div className="heat-h">Stop ↓ / Ziel →</div>
      {tps.map((t) => <div key={t} className="heat-h">+{pct(t)}</div>)}
      {sls.map((s) => (
        <div key={s} style={{ display: "contents" }}>
          <div className="heat-h">−{pct(s)}</div>
          {tps.map((t) => {
            const g = at.get(`${t}|${s}`);
            const a = g == null ? 0 : Math.min(1, Math.abs(g) / m);
            const isBest = best.take_profit === t && best.stop_loss === s;
            return (
              <div key={t} className={`heat-c ${isBest ? "best" : ""}`} title={`Ziel +${pct(t)}, Stop −${pct(s)}: Ø ${g != null ? pctSigned(g, 1) : "–"} pro Trade`}
                style={{ background: g == null ? "transparent" : `color-mix(in srgb, var(${g >= 0 ? "--good" : "--bad"}) ${Math.round(a * 55) + 6}%, var(--surface))` }}>
                {g != null ? pctSigned(g, 0) : "–"}
              </div>
            );
          })}
        </div>
      ))}
    </div>
  );
}

function VersionChart({ vs }) {
  const xs = vs.filter((v) => v.trades);
  if (!xs.length) return <Empty>Noch keine Trades mit gelernten Einstellungen</Empty>;
  return (
    <EChart height={190} deps={[xs]} ariaLabel="Papier-Ergebnis je Parameter-Version"
      build={(c) => baseOption(c, {
        tooltip: {
          ...baseOption(c).tooltip, trigger: "axis", axisPointer: { type: "shadow" },
          formatter: (ps) => {
            const v = xs[ps[0].dataIndex];
            return `<b>Version ${v.id}</b> · ${dt(v.ts)}${tipRow(ps[0].color, "Ergebnis", solSigned(v.pnl_sol, 3))}${tipRow("transparent", "Trades", n(v.trades))}${tipRow("transparent", "Gewinner", pct(v.win_rate))}`;
          },
        },
        xAxis: catAxis(c, xs.map((v) => `v${v.id}`)), yAxis: valueAxis(c, (x) => n(x, 1)),
        series: [{ name: "Ergebnis (SOL)", type: "bar", barMaxWidth: 28,
          data: xs.map((v) => ({ value: v.pnl_sol, itemStyle: { color: v.pnl_sol >= 0 ? c.good : c.bad, borderRadius: v.pnl_sol >= 0 ? [4, 4, 0, 0] : [0, 0, 4, 4] } })) }],
      })} />
  );
}

export default function Learn() {
  const [st, setSt] = useState("whale");
  const { data: d, error } = usePoll(`/api/copytrade/learn?strategy=${st}`, 60000);
  const head = (
    <Seg value={st} onChange={setSt} options={[["whale", "Modus 1 · Wale"], ["signal", "Modus 2 · Signale"]]} />
  );
  if (error) return <Card title="Selbstlernen" actions={head}><Callout kind="bad" icon="alert">{error}</Callout></Card>;
  if (!d) return <Card title="Selbstlernen" actions={head}><div className="dim">Lade …</div></Card>;
  if (!d.available) return <Card title="Selbstlernen" actions={head}><Empty>{d.note}</Empty></Card>;
  const s = d.status?.[st] || {};
  const vs = d.versions || [];
  const act = d.params || s.active_version;
  const actual = (d.exit_rules || []).find((r) => r.style === "tatsächlich");
  return (
    <div className="col" style={{ gap: 16 }}>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <div>
          <div className="b" style={{ fontSize: 15 }}>Selbstlernen</div>
          <div className="dim" style={{ fontSize: 12 }}>Jeder Einstieg wird gegen 1.296 Varianten nachgespielt; neue Einstellungen gelten nur, wenn sie auf ungesehenen Daten besser sind</div>
        </div>
        {head}
      </div>
      <div className="tiles">
        <Tile label="Lernen" value={s.autotune ? "an" : "aus"} cls={s.autotune ? "up" : ""} sub={s.autotune ? "alle 10 min im Copy-Trading-Dienst" : "unter Steuerung einschaltbar"} />
        <Tile label="Ausgewertete Einstiege" value={n(s.total)} sub={Object.entries(s.evaluated || {}).map(([k, v]) => `${v} ${{ trade: "eigene", whale_buy: "Wal-Käufe", signal: "Signale" }[k] || k}`).join(" · ") || "–"} />
        <Tile label="Bis zum ersten Tuning" value={s.needed_for_tuning ? n(s.needed_for_tuning) : "erreicht"} sub={s.needed_for_tuning ? "Einstiege fehlen noch" : "es wird getunt"} />
        <Tile label="Aktive Einstellungen" value={act?.version ? `Version ${act.version}` : "Preset"} sub={paramText(act)} />
        <Tile label="Wale mit eigener Version" value={n(s.wallet_versions)} sub="gelernt je Wal (ab 20 Einstiegen)" />
      </div>
      {act?.block && blockText(act.block) && <Callout icon="info">Gelernter Filter, diese Einstiege werden übersprungen: {blockText(act.block)}</Callout>}

      <div className="grid g2">
        <Card title="Papier-Ergebnis je Parameter-Version" sub="Summe der Trades, deren Ausstiege von dieser Version kamen">
          <VersionChart vs={vs} />
        </Card>
        <Card title="Welche Ausstiegsregel hätte am meisten gebracht?" sub={`Eigene Trades nachgespielt, Ø Netto-Gewinn pro Trade${actual ? ` · ${actual.n} Trades` : ""}`}>
          {d.exit_rules?.length ? (
            <Bars items={d.exit_rules.map((r) => ({ label: r.style === "tatsächlich" ? "tatsächlich (wir)" : r.style, value: r.avg_gain, title: r.best_variant ? `beste Variante: ${r.best_variant}` : "was unsere Trades wirklich gebracht haben" }))}
              fmt={(v) => pctSigned(v, 1)} />
          ) : <Empty>Noch keine eigenen abgeschlossenen Trades ausgewertet</Empty>}
        </Card>
      </div>

      <div className="grid g2">
        <Card title="Ziel gegen Stop" sub="Ø Netto-Gewinn pro Einstieg je Kombination (beste übrige Einstellungen); Rahmen = bester Wert">
          <Surface rows={d.surface} />
        </Card>
        <Card title="Wo verdient der Bot?" sub="Ø Netto-Gewinn mit den aktuellen Einstellungen je Merkmal" flush>
          <Table rows={(d.attribution || []).map((r) => ({ ...r, key: `${r.feature}|${r.bucket}` }))} rowKey={(r) => r.key} tall
            initialSort={{ k: "avg_gain", d: "desc" }} empty="Noch keine Daten" cols={[
              { k: "feature", l: "Merkmal", fmt: (r) => FEATURE[r.feature] || r.feature },
              { k: "bucket", l: "Gruppe", fmt: (r) => <span className="mono">{r.bucket}</span> },
              { k: "n", l: "Fälle", r: true },
              { k: "win_rate", l: "Gewinner", r: true, fmt: (r) => pct(r.win_rate) },
              { k: "avg_gain", l: "Ø Gewinn", r: true, fmt: (r) => <Delta v={r.avg_gain} d={1} arrow={false} /> },
            ]} />
        </Card>
      </div>

      <Card title="Alle Parameter-Versionen" sub="Jede Prüfung: Training auf den älteren 70 %, Test auf den neueren 30 % (nie gesehen)" flush>
        <Table rows={[...vs].reverse()} rowKey={(r) => r.id} empty="Noch keine Prüfung gelaufen" cols={[
          { k: "id", l: "Version", fmt: (r) => <span className="b">v{r.id}{r.active ? <span className="badge good" style={{ marginLeft: 6 }}>aktiv</span> : null}</span> },
          { k: "ts", l: "Geprüft", fmt: (r) => dt(r.ts) },
          { k: "scope", l: "Für", fmt: (r) => (r.scope === "mode" ? "ganzer Modus" : <span className="mono">{String(r.scope).slice(0, 6)}…</span>) },
          { k: "params", l: "Einstellungen", sort: false, fmt: (r) => <span className="t2" style={{ whiteSpace: "normal" }}>{paramText(r.params)}{blockText(r.params?.block) ? ` · Filter: ${blockText(r.params.block)}` : ""}</span> },
          { k: "test_gain", l: "Test / vorher", r: true, title: "Ø Netto-Gewinn pro Trade auf ungesehenen Daten: neue Variante / bisherige Einstellungen",
            fmt: (r) => <span><span className={sign(r.test_gain)}>{pctSigned(r.test_gain, 1)}</span> <span className="dim">/ {pctSigned(r.baseline_test_gain, 1)}</span></span> },
          { k: "accepted", l: "Übernommen", fmt: (r) => <span className={`badge ${r.accepted ? "good" : ""}`}>{r.accepted ? "ja" : "nein"}</span> },
          { k: "trades", l: "Trades", r: true, fmt: (r) => n(r.trades || 0) },
          { k: "pnl_sol", l: "Ergebnis", r: true, fmt: (r) => (r.trades ? <span className={sign(r.pnl_sol)}>{solSigned(r.pnl_sol, 3)}</span> : "–") },
          { k: "win_rate", l: "Gewinner", r: true, fmt: (r) => (r.trades ? pct(r.win_rate) : "–") },
        ]} />
      </Card>
    </div>
  );
}
