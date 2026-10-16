import { useState } from "react";
import { EChart, baseOption, catAxis, tipRow, valueAxis } from "../chart.jsx";
import { mc, n, pct, pctSigned, usePoll } from "../lib.js";
import { HistBars, SignalTable } from "../parts.jsx";
import { Callout, Card, Empty, Seg, Tile } from "../ui.jsx";

function HitBars({ data, label, height = 200 }) {
  if (!data || !data.some((d) => d.n)) return <Empty>Noch keine ausgewerteten Signale</Empty>;
  return (
    <EChart height={height} deps={[data]} ariaLabel={label}
      build={(c) => baseOption(c, {
        tooltip: {
          ...baseOption(c).tooltip, axisPointer: { type: "shadow" },
          formatter: (ps) => {
            const d = data[ps[0].dataIndex];
            return `<b>${d.bucket || d.group}</b>${tipRow(c.s[0], "Trefferquote", d.hit_rate != null ? pct(d.hit_rate) : "–")}${tipRow("transparent", "Signale", d.n)}${tipRow("transparent", "Median Max 1 h", d.median_max_1h != null ? pctSigned(d.median_max_1h, 0) : "–")}`;
          },
        },
        xAxis: catAxis(c, data.map((d) => d.bucket || d.group)),
        yAxis: valueAxis(c, (v) => `${Math.round(v * 100)} %`, { max: 1 }),
        series: [{
          type: "bar", barMaxWidth: 34, name: "Trefferquote",
          data: data.map((d) => ({ value: d.hit_rate, itemStyle: { color: c.s[0], borderRadius: [4, 4, 0, 0], opacity: d.n < 5 ? 0.45 : 1 } })),
          label: { show: true, position: "top", color: c.muted, fontSize: 11, formatter: (p) => (data[p.dataIndex].n ? `n=${data[p.dataIndex].n}` : "") },
        }],
      })} />
  );
}

/** For trading signals by hand: how many calls reached +50/+100/+150 % at their high, per window after the call. */
function Reach({ m }) {
  if (!m?.reach) return null;
  const W = [["max_ret_15m", "15 min"], ["max_ret_1h", "1 h"], ["max_ret_4h", "4 h"], ["max_ret_24h", "24 h"]];
  const any = m.reach.some((r) => W.some(([k]) => r[k]?.n));
  return (
    <Card title="Hoch nach dem Call" sub="Wie viele Signale haben ihr Hoch über +50 / +100 / +150 % gebracht, je Zeitfenster nach dem Call. So bewertest du Signale für den Handel von Hand.">
      {!any ? <Empty>Noch keine ausgewerteten Calls</Empty> : (
        <>
          <table className="tbl reach">
            <thead><tr><th>Hoch erreicht</th>{W.map(([, l]) => <th key={l} className="r">{l}</th>)}</tr></thead>
            <tbody>
              {m.reach.map((r) => (
                <tr key={r.level}>
                  <td className="b">+{n(r.level * 100, 0)} %</td>
                  {W.map(([k]) => {
                    const c = r[k] || {};
                    return <td key={k} className="r">{c.n ? <><span className="b">{pct(c.rate, 0)}</span> <span className="dim">{c.hits}/{c.n}</span></> : <span className="dim">–</span>}</td>;
                  })}
                </tr>
              ))}
            </tbody>
          </table>
          <div className="row t2" style={{ gap: 18, marginTop: 10, flexWrap: "wrap" }}>
            <span>Ziel erreicht: <b className="up">{n(m.target_hits)}</b> · Stopp erreicht: <b className="down">{n(m.stop_hits)}</b> von {n(m.planned)} Calls mit Ziel/Stopp</span>
            {m.median_peak_min_1h != null && <span>Median bis zum Hoch: <b>{n(m.median_peak_min_1h, 0)} min</b></span>}
            {m.median_dd_before_peak != null && <span>Median Rücksetzer davor: <b>{pctSigned(m.median_dd_before_peak, 0)}</b></span>}
            {m.recalls > 0 && <span>{n(m.recalls)} erneute Calls</span>}
          </div>
        </>
      )}
    </Card>
  );
}

export default function Signals() {
  const [since, setSince] = useState(72);
  const [minScore, setMinScore] = useState(0);
  const [tier, setTier] = useState("");
  const { data: rows } = usePoll(`/api/signals?since_h=${since}&min_score=${minScore}&tier=${tier}&limit=1000`, 15000);
  const { data: perf } = usePoll("/api/signals/performance", 60000);
  const { data: ctl } = usePoll("/api/control", 10000);
  return (
    <div className="col" style={{ gap: 16 }}>
      <div className="row" style={{ gap: 12 }}>
        <Seg value={since} onChange={setSince} options={[[24, "24 h"], [72, "3 Tage"], [168, "7 Tage"], [0, "Alle"]]} />
        <Seg value={minScore} onChange={setMinScore} options={[[0, "Alle Scores"], [60, "≥ 60"], [70, "≥ 70"], [80, "≥ 80"]]} />
        <Seg value={tier} onChange={setTier} options={[["", "Alle Tiers"], ["ultra", "mit Ultra"], ["smart", "mit Smart"]]} />
        <span className="sp" />
        <span className="dim">{rows ? `${rows.length} Signale` : ""}</span>
      </div>

      {perf && (
        <div className="tiles">
          <Tile label="Ausgewertete Signale" value={n(perf.n)} sub={`Treffer = +${pct(perf.pump_threshold)} innerhalb 1 h`} />
          <Tile label="Trefferquote gesamt" value={perf.hit_rate != null ? pct(perf.hit_rate) : "–"} sub={`${perf.hits} Treffer`} />
          <Tile label="Median Max-Anstieg 1 h" value={perf.median_max_1h != null ? pctSigned(perf.median_max_1h, 0) : "–"} cls={perf.median_max_1h > 0 ? "up" : ""} sub="nach dem Signal" />
          <Tile label="Median Ergebnis 1 h" value={perf.median_ret_1h != null ? pctSigned(perf.median_ret_1h, 0) : "–"} cls={perf.median_ret_1h > 0 ? "up" : perf.median_ret_1h < 0 ? "down" : ""} sub="Preis genau 1 h danach" />
        </div>
      )}

      {perf?.manual && <Reach m={perf.manual} />}

      {ctl?.paused && <Callout kind="warn" icon="pause">Käufe sind pausiert. Die Kaufen-Knöpfe sind gesperrt, bis du die Notbremse unter Steuerung löst.</Callout>}
      <Card title="Signal-Feed" sub={`Klick auf eine Zeile zeigt Gewichtung, Wallets und Ergebnis · Kaufen = Modus 2 mit Preset „${ctl?.active_presets?.signal || "standard"}“, ${ctl?.live_ready && ctl?.live?.signal ? "Echtgeld" : "Papier"}`} flush>
        <SignalTable rows={rows} paused={!!ctl?.paused} />
      </Card>

      {perf && (
        <>
          <div className="grid g3">
            <Card title="Trefferquote nach Score" sub="Wird ein höherer Score häufiger zum Pump?"><HitBars data={perf.by_bucket} label="Trefferquote nach Score" /></Card>
            <Card title="Trefferquote nach Ultra-Wallets" sub="Wie viel bringen Ultra-Smart-Wallets im Signal?"><HitBars data={perf.by_ultra} label="Trefferquote nach Ultra-Anzahl" /></Card>
            <Card title="Trefferquote nach Market Cap" sub="In welcher Größe funktionieren Signale am besten?"><HitBars data={perf.by_mcap} label="Trefferquote nach Market Cap" /></Card>
          </div>
          <div className="grid g-wide-left">
            <Card title="Score gegen Ergebnis" sub="Jeder Punkt ein Signal: Score beim Signal und höchster Anstieg in der Stunde danach">
              {perf.scatter.length ? (
                <EChart height={280} deps={[perf.scatter]} ariaLabel="Score gegen Max-Anstieg"
                  onClick={(p) => p.data?.mint && (window.location.hash = `/coin/${p.data.mint}`)}
                  build={(c) => baseOption(c, {
                    tooltip: { ...baseOption(c).tooltip, trigger: "item", formatter: (p) => `<b>${p.data.symbol}</b>${tipRow(c.s[0], "Score", n(p.data.value[0], 0))}${tipRow(c.s[0], "Max 1 h", pctSigned(p.data.value[1], 0))}${tipRow("transparent", "MC", mc(p.data.mcap))}` },
                    xAxis: valueAxis(c, (v) => v, { min: 0, max: 100 }),
                    yAxis: valueAxis(c, (v) => `${Math.round(v * 100)} %`, { name: "Max-Anstieg 1 h", nameTextStyle: { color: c.muted } }),
                    series: [true, false].map((hit) => ({
                      name: hit ? "Treffer" : "kein Treffer", type: "scatter", symbolSize: 9,
                      itemStyle: { color: hit ? c.good : c.muted, borderColor: c.surface, borderWidth: 1.5, opacity: 0.9 },
                      data: perf.scatter.filter((s) => s.hit === hit).map((s) => ({ value: [s.score, s.max_ret_1h], symbol: s.symbol, mint: s.mint, mcap: s.mcap_usd })),
                    })),
                    legend: { show: true, top: 0, right: 0, textStyle: { color: c.text2 }, icon: "circle" },
                    grid: { left: 8, right: 14, top: 30, bottom: 8, containLabel: true },
                  })} />
              ) : <Empty>Noch keine ausgewerteten Signale</Empty>}
            </Card>
            <Card title="Trefferquote nach Wallet-Anzahl" sub="Wie viele Tier-Wallets müssen dabei sein?">
              <HitBars data={perf.by_wallets} label="Trefferquote nach Wallet-Anzahl" height={280} />
            </Card>
          </div>
          {perf.daily?.length > 1 && (
            <Card title="Trefferquote pro Tag" sub="Werden die Signale mit der Zeit besser?">
              <HistBars data={perf.daily.map((d) => ({ bucket: new Date(d.ts * 1000).toLocaleDateString("de-DE", { day: "2-digit", month: "2-digit" }), n: d.hit_rate, cnt: d.n }))}
                label="Trefferquote" height={200} fmtY={(v) => `${Math.round(v * 100)} %`} />
            </Card>
          )}
        </>
      )}
    </div>
  );
}
