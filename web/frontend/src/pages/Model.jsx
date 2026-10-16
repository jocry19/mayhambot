import { useState } from "react";
import { EChart, baseOption, catAxis, tierColor, tipRow, valueAxis } from "../chart.jsx";
import { ALGO_LABEL, DROP_LABEL, TIER_LABEL, TIER_SOURCE, dt, mc, n, pct, pctSigned, sign, solSigned, usePoll } from "../lib.js";
import { Bars, Callout, Card, Delta, Empty, Legend, Markdown, Seg, Table, Tile } from "../ui.jsx";

const COMP = { win: "Trefferquote", early: "Frühe Einstiege", roi: "ROI", consistency: "Konstanz", timing: "Timing", influence: "Einfluss", rug_avoid: "Rugs meiden" };

function RateBars({ rows, keyName = "bucket", height = 210 }) {
  if (!rows?.length) return <Empty />;
  return (
    <EChart height={height} deps={[rows]} ariaLabel="Pump-Quote"
      build={(c) => baseOption(c, {
        tooltip: {
          ...baseOption(c).tooltip, axisPointer: { type: "shadow" },
          formatter: (ps) => { const r = rows[ps[0].dataIndex]; return `<b>${r[keyName]}</b>${tipRow(c.s[0], "Pump-Quote", pct(r.rate))}${tipRow("transparent", "sicher mind. (95 %)", pct(r.ci_low))}${tipRow("transparent", "Fälle", `${r.pumps} / ${r.n}`)}${tipRow("transparent", "Median Max-Anstieg", pctSigned(r.median_max_ret, 0))}`; },
        },
        xAxis: catAxis(c, rows.map((r) => r[keyName])), yAxis: valueAxis(c, (v) => `${Math.round(v * 100)} %`, { min: 0 }),
        series: [{ type: "bar", barMaxWidth: 34, name: "Pump-Quote",
          data: rows.map((r) => ({ value: r.rate, itemStyle: { color: c.s[0], borderRadius: [4, 4, 0, 0], opacity: r.n < 8 ? 0.5 : 1 } })),
          label: { show: true, position: "top", color: c.muted, fontSize: 11, formatter: (p) => `n=${rows[p.dataIndex].n}` } }],
      })} />
  );
}

function PaperVsSignals({ p }) {
  if (!p || (!p.signals?.length && !p.paper?.length)) return <Empty>Noch keine ausgewerteten Prognosen oder Papier-Trades</Empty>;
  return (
    <>
      {p.signals?.length > 0 && (
        <Table rows={p.signals} rowKey={(r) => r.grade} cols={[
          { k: "grade", l: "Note" },
          { k: "n", l: "Prognosen", r: true },
          { k: "hit_50", l: "+50 % in 1 h", r: true, fmt: (r) => pct(r.hit_50) },
          { k: "median_max_ret_1h", l: "Median Max 1 h", r: true, fmt: (r) => <Delta v={r.median_max_ret_1h} d={0} arrow={false} /> },
          { k: "median_ret_1h", l: "Median nach 1 h", r: true, fmt: (r) => <Delta v={r.median_ret_1h} d={0} arrow={false} /> },
        ]} />
      )}
      {p.paper?.length > 0 && (
        <Table rows={p.paper} rowKey={(r) => `${r.mode}-${r.grade}`} cols={[
          { k: "mode", l: "Konto", fmt: (r) => (r.mode === "live" ? "Echtgeld" : r.mode === "paper" ? "Papier" : r.mode) },
          { k: "grade", l: "Note beim Kauf" },
          { k: "n", l: "Trades", r: true },
          { k: "win_rate", l: "Gewinner", r: true, fmt: (r) => pct(r.win_rate) },
          { k: "avg_pnl_pct", l: "Ø Trade", r: true, fmt: (r) => <Delta v={r.avg_pnl_pct} arrow={false} /> },
          { k: "pnl_sol", l: "Summe", r: true, fmt: (r) => <span className={sign(r.pnl_sol)}>{solSigned(r.pnl_sol, 3)}</span> },
        ]} />
      )}
    </>
  );
}

export default function Model() {
  const { data: d } = usePoll("/api/model", 60000);
  const [h, setH] = useState("1h");
  if (!d) return <div className="empty">Lade …</div>;
  const v = d.active;
  const vs = d.versions;
  const rep = d.report?.data || {};
  const m = v?.metrics?.[h] || {};
  return (
    <div className="col" style={{ gap: 16 }}>
      {!v && <Callout icon="info">Das Forschungsmodell hat noch nicht trainiert. Es läuft mit der App (python -m model service) und erscheint hier nach dem ersten Lauf. Bis dahin gilt die Signal-Formel unten.</Callout>}
      {d.tier_source !== "model" && v && <Callout>{TIER_SOURCE[d.tier_source]}.</Callout>}
      {v && (
        <div className="tiles">
          <Tile label="Aktives Modell" value={`v${v.id}`} sub={`trainiert ${dt(v.ts)}`} />
          <Tile label="Trennschärfe 1 h (AUC)" value={v.auc_1h != null ? n(v.auc_1h, 2) : "–"} sub={v.auc_baseline_1h != null ? `einfache Formel: ${n(v.auc_baseline_1h, 2)} · 0,5 = Zufall` : "noch zu wenige Testfälle"} />
          <Tile label="Top 10 % der Prognosen" value={v.top10_1h != null ? pct(v.top10_1h) : "–"} sub={v.base_rate_1h != null ? `pumpen, sonst nur ${pct(v.base_rate_1h)}` : "–"} />
          <Tile label="Lern-Events" value={n(v.n_events)} sub={`${n(v.n_test_1h)} davon im Test`} />
          <Tile label="Stufen im Einsatz" value={d.tier_info ? n((d.tier_info.counts.ultra || 0) + (d.tier_info.counts.smart || 0) + (d.tier_info.counts.good || 0)) : n(v.n_wallets)}
            title="Ultra · Smart · Good, wie Signale und Copy-Trading sie gerade nutzen (Tabelle wallets)"
            sub={d.tier_info ? `${n(d.tier_info.counts.ultra)} Ultra · ${n(d.tier_info.counts.smart)} Smart · ${n(d.tier_info.counts.good)} Good${d.tier_info.source === "model" ? ` · aus v${d.tier_info.version ?? v.id}${d.tier_info.synced ? "" : " (noch nicht übernommen)"}` : " · Heuristik"}`
              : `${v.tier_counts.ultra || 0} Ultra · ${v.tier_counts.smart || 0} Smart · ${v.tier_counts.good || 0} Good`} />
          <Tile label="Nächstes Retrain" value={d.retrain?.next_ts ? (d.retrain.due ? "fällig" : dt(d.retrain.next_ts)) : "–"} cls={d.retrain?.failed ? "down" : ""}
            sub={d.retrain ? `${d.retrain.failed ? "letzter Versuch fehlgeschlagen, neuer Versuch im nächsten Zyklus · " : d.retrain.due ? "läuft im nächsten Zyklus · " : ""}alle ${n(d.retrain.every_h)} h${d.retrain.source === "berechnet" ? " (berechnet)" : ""}` : ""} />
          <Tile label="Algorithmus 1 h" value={<span style={{ fontSize: 17 }}>{ALGO_LABEL[v.algo_1h] || v.algo_1h || "–"}</span>}
            sub={v.selection?.["1h"]?.why || "gewählt nach Test auf ungesehenen Daten"} />
          {Object.values(d.research).filter((r) => r.n > 0).slice(0, 1).map((r) => <Tile key={r.label} label={r.label} value={n(r.n)} />)}
        </div>
      )}

      {vs.length > 0 && (
        <div className="grid g3">
          <Card title="Wird das Modell besser?" sub="Trennschärfe je Version (AUC, 1 h)">
            <EChart height={210} deps={[vs]} ariaLabel="AUC je Version"
              build={(c) => baseOption(c, {
                xAxis: catAxis(c, vs.map((x) => `v${x.id}`)), yAxis: valueAxis(c, (x) => n(x, 2), { min: 0.4, max: 1 }),
                series: [
                  { name: "Modell", type: "line", symbolSize: 7, lineStyle: { width: 2, color: c.s[0] }, itemStyle: { color: c.s[0] }, data: vs.map((x) => x.auc_1h) },
                  { name: "einfache Formel", type: "line", symbolSize: 6, lineStyle: { width: 1.5, color: c.muted }, itemStyle: { color: c.muted }, data: vs.map((x) => x.auc_baseline_1h) },
                ],
              })} />
            <Legend items={[{ label: "Modell", color: "var(--series-1)" }, { label: "einfache Formel", color: "var(--muted)" }]} />
          </Card>
          <Card title="Tiers je Version" sub="Wie viele Wallets das Modell als Ultra/Smart/Good führt">
            <EChart height={210} deps={[vs]} ariaLabel="Tiers je Version"
              build={(c) => baseOption(c, {
                tooltip: { ...baseOption(c).tooltip, axisPointer: { type: "shadow" } },
                xAxis: catAxis(c, vs.map((x) => `v${x.id}`)), yAxis: valueAxis(c),
                series: ["ultra", "smart", "good"].map((k) => ({ name: TIER_LABEL[k], type: "bar", stack: "t", barMaxWidth: 26,
                  itemStyle: { color: tierColor(c, k), borderColor: c.surface, borderWidth: 1 }, data: vs.map((x) => x.tier_counts[k] || 0) })),
              })} />
            <Legend items={["ultra", "smart", "good"].map((k) => ({ label: TIER_LABEL[k], color: `var(--tier-${k})` }))} />
          </Card>
          <Card title="Was macht eine gute Wallet aus?" sub="Gelernte Gewichte des Wallet-Scores je Version">
            <EChart height={210} deps={[vs]} ariaLabel="Gewichte je Version"
              build={(c) => {
                const keys = Object.keys(vs[vs.length - 1].wallet_weights || {});
                return baseOption(c, {
                  tooltip: { ...baseOption(c).tooltip, valueFormatter: (x) => pct(x) },
                  xAxis: catAxis(c, vs.map((x) => `v${x.id}`)), yAxis: valueAxis(c, (x) => `${Math.round(x * 100)} %`),
                  series: keys.map((k, i) => ({ name: COMP[k] || k, type: "line", symbolSize: 5, lineStyle: { width: 1.8, color: c.s[i % 8] }, itemStyle: { color: c.s[i % 8] }, data: vs.map((x) => x.wallet_weights[k]) })),
                });
              }} />
            <Legend items={Object.keys(vs[vs.length - 1].wallet_weights || {}).map((k, i) => ({ label: COMP[k] || k, color: `var(--series-${(i % 8) + 1})` }))} />
          </Card>
        </div>
      )}

      {rep.combos && (
        <Card title="Welche Kombination führt zum Pump?" sub={`Forschung über alle vergangenen Coin-Momente · Pump = +50 % in 1 h · blasse Balken = wenige Fälle`}>
          {!Object.values(rep.combos).some((r) => r?.length) && <Empty>Noch zu wenige Coin-Momente mit Ergebnis. Die Forschung füllt das bei jedem Modell-Lauf.</Empty>}
          <div className="grid g3">
            {Object.entries(rep.combos).filter(([k, r]) => k !== "Beste Kombinationen" && r?.length).map(([k, rows]) => (
              <div key={k}><div className="b" style={{ marginBottom: 4 }}>{k}</div><RateBars rows={rows} /></div>
            ))}
          </div>
          {rep.combos["Beste Kombinationen"]?.length > 0 && (
            <>
              <div className="b" style={{ margin: "12px 0 4px" }}>Beste Kombinationen (sortiert nach sicherer Untergrenze)</div>
              <Table rows={rep.combos["Beste Kombinationen"]} rowKey={(r) => r.combo} cols={[
                { k: "combo", l: "Tier-Mix | Inflow | Market Cap", fmt: (r) => <span className="mono">{r.combo}</span> },
                { k: "n", l: "Fälle", r: true },
                { k: "rate", l: "Pump-Quote", r: true, fmt: (r) => pct(r.rate) },
                { k: "ci_low", l: "mind. (95 %)", r: true, fmt: (r) => pct(r.ci_low) },
                { k: "median_max_ret", l: "Median Max", r: true, fmt: (r) => pctSigned(r.median_max_ret, 0) },
              ]} />
            </>
          )}
        </Card>
      )}

      <div className="grid g2">
        <Card title="Welche Info sagt den Pump voraus?" sub="Einzel-Vorhersagekraft je Eingang (0 = nichts, 1 = perfekt)">
          {rep.univariate?.length ? (
            <Bars max={Math.max(...rep.univariate.map((u) => u.power || 0), 0.01)} items={rep.univariate.map((u) => ({ label: u.label, value: u.power, title: `AUC ${n(u.auc, 2)} · ${u.direction}` }))} fmt={(x) => n(x, 2)} />
          ) : <Empty>Erst nach dem ersten Forschungslauf</Empty>}
        </Card>
        <Card title="Wie das Modell gewichtet" sub="Koeffizient je Eingang (pro Standardabweichung); negativ = spricht gegen den Pump"
          actions={<Seg value={h} onChange={setH} options={[["15m", "15 min"], ["1h", "1 h"], ["6h", "6 h"]]} />}>
          {v?.coefs?.[h]?.length ? (
            <>
              <Bars items={v.coefs[h].map((x) => ({ label: x.label, value: x.weight }))} fmt={(x) => n(x, 2)} />
              <div className="dim" style={{ fontSize: 12, marginTop: 8 }}>
                {v.coefs._kind?.[h]?.kind === "prior" ? "Noch Startwerte (zu wenige Lern-Events)." : `${ALGO_LABEL[v.coefs._kind?.[h]?.algo] || "Modell"}${v.coefs._kind?.[h]?.drop ? ` ${DROP_LABEL[v.coefs._kind[h].drop] || "ohne " + v.coefs._kind[h].drop}` : ""} · gelernt aus ${n(v.coefs._kind?.[h]?.n)} Events, ${n(v.coefs._kind?.[h]?.pos)} Pumps.`}
                {v.coefs._kind?.[h]?.algo === "gbm" && " Bei Gradient Boosting: Wichtigkeit je Eingang, die Richtung zeigt die Live-Prognose je Coin."}
              </div>
            </>
          ) : (
            <>
              <div className="help">Bis das Modell gelernt hat, gilt die Signal-Formel:</div>
              <Bars max={0.4} items={d.signal_formula.map((f) => ({ label: f.label, value: f.weight }))} fmt={(x) => pct(x)} />
            </>
          )}
        </Card>
      </div>

      {v && m.quintiles?.length > 0 && (
        <div className="grid g3">
          <Card title="Treffen die Prognosen?" sub={`Prognosen in 5 Gruppen sortiert (1 = höchste), echte Pump-Quote · Horizont ${h}`}>
            <RateBars rows={m.quintiles.map((q) => ({ bucket: `Q${q.quintile}`, n: q.n, pumps: Math.round(q.n * q.pump_rate), rate: q.pump_rate, ci_low: null, median_max_ret: q.median_max_ret }))} />
          </Card>
          <Card title="Pump-Quote nach Tier" sub="Wenn eine Wallet dieses Tiers kauft: wie oft pumpt der Coin danach?">
            {v.metrics.tier_forward && Object.keys(v.metrics.tier_forward).length ? (
              <RateBars rows={["ultra", "smart", "good", "none"].filter((t) => v.metrics.tier_forward[t]).map((t) => ({ bucket: t === "none" ? "ohne Tier" : TIER_LABEL[t], ...v.metrics.tier_forward[t], pumps: Math.round(v.metrics.tier_forward[t].n * v.metrics.tier_forward[t].pump_rate), rate: v.metrics.tier_forward[t].pump_rate }))} />
            ) : <Empty />}
          </Card>
          <Card title="Kalibrierung" sub="Vorhergesagte Pump-Chance gegen echte Quote">
            {d.calibration.length ? (
              <EChart height={210} deps={[d.calibration]} ariaLabel="Kalibrierung"
                build={(c) => baseOption(c, {
                  tooltip: { ...baseOption(c).tooltip, axisPointer: { type: "shadow" }, valueFormatter: (x) => pct(x) },
                  xAxis: catAxis(c, d.calibration.map((x) => x.bucket)), yAxis: valueAxis(c, (x) => `${Math.round(x * 100)} %`, { min: 0, max: 1 }),
                  series: [
                    { name: "vorhergesagt", type: "line", symbolSize: 6, lineStyle: { color: c.muted, width: 1.5 }, itemStyle: { color: c.muted }, data: d.calibration.map((x) => x.predicted) },
                    { name: "eingetreten", type: "bar", barMaxWidth: 24, itemStyle: { color: c.s[0], borderRadius: [4, 4, 0, 0] }, data: d.calibration.map((x) => x.realized) },
                  ],
                })} />
            ) : <Empty>Noch keine ausgewerteten Prognosen</Empty>}
          </Card>
        </div>
      )}

      <div className="grid g2">
        <Card title="Welches Modell gewinnt?" sub="Jeder Kandidat wird auf ungesehenen Daten getestet; der bisherige Sieger bleibt, bis einer klar besser ist" flush>
          <Table rows={Object.entries(v?.selection || {}).filter(([, x]) => x && x.chosen).map(([hz, x]) => ({ hz, ...x }))} rowKey={(r) => r.hz}
            empty="Noch keine Modellauswahl (braucht genug Lern-Events)" cols={[
              { k: "hz", l: "Horizont" },
              { k: "chosen", l: "Gewählt", fmt: (r) => `${ALGO_LABEL[r.chosen.algo] || r.chosen.algo}${r.chosen.drop ? ` ${DROP_LABEL[r.chosen.drop] || "ohne " + r.chosen.drop}` : ""}`, sort: false },
              { k: "why", l: "Warum", fmt: (r) => <span className="t2">{r.why}</span> },
              { k: "n_test", l: "Testfälle", r: true, fmt: (r) => n(r.n_test) },
              { k: "ll", l: "Fehler (Logloss)", r: true, title: "kleiner ist besser; Vergleich: ohne Modell", sort: (r) => r.holdout?.logloss,
                fmt: (r) => (r.holdout?.logloss != null ? `${n(r.holdout.logloss, 3)} / ${n(r.base_logloss, 3)}` : "–") },
              { k: "auc", l: "AUC", r: true, sort: (r) => r.holdout?.auc, fmt: (r) => (r.holdout?.auc != null ? n(r.holdout.auc, 2) : "–") },
            ]} />
        </Card>
        <Card title="Signale gegen Papier-Trading" sub="Was die Noten der Prognosen gebracht haben, und was der Copy-Trader auf denselben Coins verdient hat" flush>
          <PaperVsSignals p={rep.paper} />
        </Card>
      </div>

      {rep.tier_profiles && Object.keys(rep.tier_profiles).length > 0 && (
        <Card title="Tier-Profile aus der Forschung" sub="Median je Tier" flush>
          <Table rows={Object.entries(rep.tier_profiles).map(([k, x]) => ({ k, ...x }))} rowKey={(r) => r.k} cols={[
            { k: "k", l: "Tier", fmt: (r) => TIER_LABEL[r.k] || r.k },
            { k: "n", l: "Wallets", r: true },
            { k: "win_rate", l: "Trefferquote", r: true, fmt: (r) => pct(r.win_rate) },
            { k: "roi", l: "ROI", r: true, fmt: (r) => pct(r.roi) },
            { k: "entry_mcap", l: "Einstieg MC", r: true, fmt: (r) => mc(r.entry_mcap) },
            { k: "followers", l: "Nachkäufer", r: true, fmt: (r) => n(r.followers, 0) },
            { k: "min_to_2x", l: "min bis 2x", r: true, fmt: (r) => n(r.min_to_2x, 0) },
          ]} />
        </Card>
      )}

      {d.report?.markdown && (
        <Card title={d.report.title || "Forschungsbericht"} sub={`Modell v${d.report.version} · ${dt(d.report.ts)}`}>
          <Markdown text={d.report.markdown} />
        </Card>
      )}

      {vs.length > 0 && (
        <Card title="Alle Modell-Versionen" flush>
          <Table rows={[...vs].reverse()} rowKey={(r) => r.id} cols={[
            { k: "id", l: "Version", fmt: (r) => <span>v{r.id} {r.active && <span className="badge accent">aktiv</span>}</span> },
            { k: "ts", l: "Trainiert", fmt: (r) => dt(r.ts) },
            { k: "n_events", l: "Events", r: true, fmt: (r) => n(r.n_events) },
            { k: "auc_1h", l: "AUC 1 h", r: true, fmt: (r) => n(r.auc_1h, 2) },
            { k: "top10_1h", l: "Top 10 %", r: true, fmt: (r) => pct(r.top10_1h) },
            { k: "tiers", l: "Ultra / Smart / Good", r: true, sort: false, fmt: (r) => `${r.tier_counts.ultra || 0} / ${r.tier_counts.smart || 0} / ${r.tier_counts.good || 0}` },
            { k: "notes", l: "Notiz", fmt: (r) => <span className="t2">{r.notes}</span> },
          ]} />
        </Card>
      )}
    </div>
  );
}
