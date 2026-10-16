import { useState } from "react";
import { EChart, baseOption, catAxis, tierColor, timeAxis, tipRow, valueAxis } from "./chart.jsx";
import { TIER_LABEL, dt, go, mc, n, pct, pctSigned, sign, usd, usdSigned, ago, short } from "./lib.js";
import { Addr, Bars, Delta, Empty, Legend, Score, Table, Tier, TierCounts } from "./ui.jsx";
import { BuyButton } from "./buy.jsx";
import { RiskBadge } from "./safety.jsx";

export const CoinLink = ({ mint, symbol }) => (
  <a href={`#/coin/${mint}`} onClick={(e) => e.stopPropagation()} className="b">{symbol || short(mint)}</a>
);
export const WalletLink = ({ a, label }) => (
  <a href={`#/wallet/${a}`} onClick={(e) => e.stopPropagation()} className="mono">{label || short(a, 5)}</a>
);

/** Net inflow per time bucket, stacked by tier (+ everybody else in gray). One unit (USD), one axis. */
export function FlowChart({ flows, height = 240, group, withOther: allowOther = true, crowdDefault = true, markers = [] }) {
  const [crowd, setCrowd] = useState(crowdDefault);
  if (!flows || !flows.length) return <Empty>Noch keine Trades im Zeitraum</Empty>;
  const withOther = allowOther && crowd;
  const keys = withOther ? ["ultra", "smart", "good", "other"] : ["ultra", "smart", "good"];
  return (
    <>
      <EChart height={height} group={group} deps={[flows, withOther, markers]} ariaLabel="Netto-Inflow nach Wallet-Tier"
        build={(c) => baseOption(c, {
          tooltip: {
            ...baseOption(c).tooltip,
            formatter: (ps) => {
              const t = ps[0]?.value?.[0];
              const rows = ps.map((p) => tipRow(p.color, p.seriesName, usdSigned(p.value[1])));
              const tot = ps.filter((p) => p.seriesId !== "other").reduce((a, p) => a + (p.value[1] || 0), 0);
              return `<div style="margin-bottom:4px;color:${c.muted}">${dt(t / 1000)}</div>${rows.join("")}<div style="margin-top:4px;border-top:1px solid ${c.grid};padding-top:4px">${tipRow("transparent", "Tier-Wallets gesamt", usdSigned(tot))}</div>`;
            },
          },
          xAxis: timeAxis(c),
          yAxis: valueAxis(c, (v) => usd(v, Math.abs(v) >= 1000 && v % 1000 ? 1 : 0)),
          series: keys.map((k) => ({
            id: k, name: k === "other" ? "Alle anderen" : TIER_LABEL[k], type: "bar", stack: k === "other" ? "o" : "t",
            barMaxWidth: 14, itemStyle: { color: tierColor(c, k), borderRadius: 2, borderColor: c.surface, borderWidth: 0.5 },
            data: flows.map((f) => [f.ts * 1000, f[k]]),
            ...(k === "ultra" && markers.length ? {
              markLine: { symbol: "none", silent: true, label: { color: c.text2, formatter: (p) => p.data.name, position: "insideEndTop" },
                lineStyle: { color: c.s[1], type: "solid", width: 1 }, data: markers.map((m) => ({ xAxis: m.ts * 1000, name: m.label })) },
            } : {}),
          })),
        })} />
      <div className="row" style={{ justifyContent: "space-between", alignItems: "center", gap: 8 }}>
        <Legend items={keys.map((k) => ({ label: k === "other" ? "Alle anderen (Crowd)" : TIER_LABEL[k], color: `var(--tier-${k})` }))} />
        {allowOther && <label className="dim" style={{ fontSize: 12, display: "flex", gap: 6, alignItems: "center", cursor: "pointer" }}>
          <input type="checkbox" checked={crowd} onChange={(e) => setCrowd(e.target.checked)} /> Crowd zeigen</label>}
      </div>
    </>
  );
}

/** How strongly each input was weighted for this score. */
export function Breakdown({ items, score }) {
  if (!items || !items.length) return <Empty>Keine Aufschlüsselung gespeichert</Empty>;
  const hasPoints = items.some((i) => i.points != null);
  const all = items;
  items = all.filter((i) => Math.abs((hasPoints ? i.points : i.contribution) || 0) >= 0.005 || (i.share || 0) >= 0.005);
  const silent = all.length - items.length;
  return (
    <div className="col" style={{ gap: 10 }}>
      <Bars max={hasPoints ? Math.max(...items.map((i) => Math.abs(i.points || 0)), 1) : undefined}
        items={items.map((i) => ({
          label: i.label || i.feature, value: hasPoints ? i.points : i.contribution,
          title: `${i.why || ""}\nGewicht ${i.weight ?? "–"} · Wert ${i.value ?? "–"}${i.share != null ? ` · Anteil ${pct(i.share)}` : ""}`,
        }))}
        fmt={(v, it) => (hasPoints ? `${n(v, 1)} P` : n(v, 2))} />
      <table className="tbl" style={{ fontSize: 12 }}>
        <thead><tr><th>Eingang</th><th>Messwert</th><th className="r">Gewicht</th><th className="r">Anteil</th></tr></thead>
        <tbody>
          {items.map((i) => (
            <tr key={i.feature}>
              <td>{i.label || i.feature}</td>
              <td className="t2" style={{ whiteSpace: "normal" }}>{i.why && i.why !== i.label ? i.why : i.value != null ? n(i.value, 3) : "–"}</td>
              <td className="r">{i.weight != null ? n(i.weight, 2) : "–"}</td>
              <td className="r">{i.share != null ? pct(i.share) : "–"}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="dim" style={{ fontSize: 12 }}>
        {hasPoints && score != null ? `Punkte addieren sich zum Score ${n(score, 1)}.` : "Beitrag zur Pump-Chance (log-odds): positiv spricht für einen Pump, negativ dagegen."}
        {silent > 0 && ` ${silent} weitere Eingänge ohne Beitrag (Messwert 0).`}
      </div>
    </div>
  );
}

export function Outcome({ o }) {
  if (!o) return null;
  const k = [["ret_5m", "5 min"], ["ret_15m", "15 min"], ["ret_1h", "1 h"], ["ret_6h", "6 h"], ["max_ret_1h", "Max 1 h"], ["max_ret_24h", "Max 24 h"]]
    .filter(([key]) => key in o);
  return (
    <div className="row" style={{ gap: 16 }}>
      {k.map(([key, l]) => (
        <div className="kv" key={key}><span className="k">{l}</span><span className={`v ${sign(o[key])}`}>{o[key] == null ? <span className="dim">offen</span> : pctSigned(o[key], 0)}</span></div>
      ))}
    </div>
  );
}

export function SignalDetail({ s }) {
  return (
    <div className="sig-body">
      <div>
        <div className="b" style={{ marginBottom: 6 }}>Wie stark welche Info gewichtet wurde</div>
        <div className="dim" style={{ fontSize: 12, marginBottom: 8 }}>{s.is_model ? "Forschungsmodell" : "Signal-Formel"} · {s.model_version}</div>
        <Breakdown items={s.breakdown} score={s.score} />
      </div>
      <div className="col" style={{ gap: 12 }}>
        <div>
          <div className="b" style={{ marginBottom: 6 }}>Ergebnis danach</div>
          <Outcome o={s.outcome} />
        </div>
        <div>
          <div className="b" style={{ marginBottom: 6 }}>Wallets, die das Signal ausgelöst haben ({s.wallets.length})</div>
          <Table rows={s.wallets} max={30} rowKey={(r) => r.address} onRow={(r) => go(`/wallet/${r.address}`)} cols={[
            { k: "tier", l: "Tier", fmt: (r) => <Tier t={r.tier} />, sort: (r) => ({ ultra: 3, smart: 2, good: 1 }[r.tier] || 0) },
            { k: "address", l: "Wallet", fmt: (r) => <WalletLink a={r.address} /> },
            { k: "score", l: "Score", r: true, fmt: (r) => n(r.score, 0) },
            { k: "buy_usd", l: "Kauf", r: true, fmt: (r) => usd(r.buy_usd ?? r.net_usd) },
            { k: "first_ts", l: "Erster Kauf", r: true, fmt: (r) => dt(r.first_ts) },
          ]} />
        </div>
      </div>
    </div>
  );
}

export function SignalTable({ rows, showCoin = true, max = 300, initialSort, paused, buy = true }) {
  const cols = [
    { k: "ts", l: "Zeit", fmt: (r) => <span title={ago(r.ts)}>{dt(r.ts)}</span> },
    ...(showCoin ? [{ k: "symbol", l: "Coin", fmt: (r) => <CoinLink mint={r.mint} symbol={r.symbol} /> }] : []),
    { k: "mcap_usd", l: "MC beim Signal", r: true, fmt: (r) => mc(r.mcap_usd) },
    { k: "score", l: "Score", fmt: (r) => <Score v={r.score} g={r.grade} /> },
    { k: "safety", l: "Rug-Risiko", title: "Rug- und Insider-Risiko beim Signal (0–100); gesperrt = Copy-Trading kauft nicht", sort: (r) => r.safety?.score ?? -1, fmt: (r) => <RiskBadge s={r.safety || r.safety_now} now={r.safety_now} /> },
    { k: "tiers", l: "Wallets U/S/G", fmt: (r) => <TierCounts tiers={r.tiers} />, sort: (r) => r.tiers.ultra * 100 + r.tiers.smart * 10 + r.tiers.good },
    { k: "inflow_usd", l: "Smart-Inflow", r: true, fmt: (r) => <span title={r.inflow_sol != null ? `${n(r.inflow_sol, 2)} SOL` : ""}>{usd(r.inflow_usd)}</span> },
    { k: "prediction", l: "Prognose", r: true, title: "Chance auf +50 % innerhalb 1 h", fmt: (r) => (r.prediction != null ? pct(r.prediction) : "–") },
    { k: "r15", l: "15 min", r: true, sort: (r) => r.outcome.ret_15m, fmt: (r) => <Delta v={r.outcome.ret_15m} d={0} arrow={false} /> },
    { k: "r1", l: "1 h", r: true, sort: (r) => r.outcome.ret_1h, fmt: (r) => <Delta v={r.outcome.ret_1h} d={0} arrow={false} /> },
    { k: "m1", l: "Max 1 h", r: true, sort: (r) => r.outcome.max_ret_1h, fmt: (r) => <Delta v={r.outcome.max_ret_1h} d={0} arrow={false} /> },
    { k: "m24", l: "Max 24 h", r: true, sort: (r) => r.outcome.max_ret_24h, fmt: (r) => <Delta v={r.outcome.max_ret_24h} d={0} arrow={false} /> },
    ...(buy ? [{ k: "buy", l: "", sort: false, fmt: (r) => <BuyButton mint={r.mint} symbol={r.symbol} signal={r} heldId={r.open_position_id} pendingId={r.pending_buy_id} paused={paused} blocked={r.safety_now?.blocked} /> }] : []),
  ];
  return <Table rows={rows} cols={cols} max={max} rowKey={(r) => r.id} expand={(r) => <SignalDetail s={r} />} initialSort={initialSort}
    empty="Noch keine Signale. Sobald genug Smart Money in einen Coin fließt, erscheinen sie hier." />;
}

export function PnlCurve({ points, height = 220, key_ = "pnl_sol", label = "Gewinn kumuliert", unit = "SOL" }) {
  points = (points || []).filter((p) => p.ts);
  if (!points || points.length < 2) return <Empty>Noch zu wenige abgeschlossene Trades für eine Kurve</Empty>;
  return (
    <EChart height={height} deps={[points]} ariaLabel={label}
      build={(c) => baseOption(c, {
        tooltip: { ...baseOption(c).tooltip, valueFormatter: (v) => `${n(v, 3)} ${unit}` },
        xAxis: timeAxis(c), yAxis: valueAxis(c, unit === "USD" ? (v) => usd(v, 0) : (v) => n(v, 1), { scale: true }),
        series: [{ name: label, type: "line", showSymbol: false, step: false, lineStyle: { width: 2, color: c.s[0] },
          areaStyle: { color: c.s[0], opacity: 0.08 }, data: points.map((p) => [p.ts * 1000, p[key_]]) }],
      })} />
  );
}

export function HistBars({ data, x = "bucket", y = "n", height = 200, fmtX = (v) => v, fmtY, color, label = "Anzahl", tip }) {
  if (!data || !data.some((d) => d[y])) return <Empty />;
  return (
    <EChart height={height} deps={[data]} ariaLabel={label}
      build={(c) => baseOption(c, {
        tooltip: { ...baseOption(c).tooltip, trigger: "axis", axisPointer: { type: "shadow" }, ...(tip ? { formatter: tip } : {}) },
        xAxis: catAxis(c, data.map((d) => fmtX(d[x]))), yAxis: valueAxis(c, fmtY),
        ...(fmtY ? { tooltip: { ...baseOption(c).tooltip, trigger: "axis", axisPointer: { type: "shadow" }, valueFormatter: fmtY, ...(tip ? { formatter: tip } : {}) } } : {}),
        series: [{ name: label, type: "bar", barMaxWidth: 28, data: data.map((d) => ({ value: d[y], itemStyle: { color: typeof color === "function" ? color(d, c) : color || c.s[0], borderRadius: [4, 4, 0, 0] } })) }],
      })} />
  );
}

export { Addr };
