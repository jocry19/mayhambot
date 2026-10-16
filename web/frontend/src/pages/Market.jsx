import { useState } from "react";
import { EChart, baseOption, timeAxis, tipRow, valueAxis } from "../chart.jsx";
import { ago, dt, go, n, pct, pctSigned, sign, usd, usdSigned, usePoll } from "../lib.js";
import { Bars, Callout, Card, Delta, Empty, Seg, Table, Tile } from "../ui.jsx";

const LABEL_CLS = { risk_off: "bad", neutral: "", risk_on: "good", hot: "warn" };
const LENS = [["by_phase", "Nach Marktphase"], ["by_sector_heat", "Nach Sektor-Hitze"], ["by_term", "Nach Narrativ"], ["by_term_stage", "Nach Narrativ-Stufe"], ["new_term", "Brandneues Narrativ"]];
const ORDER = ["risk_off", "neutral", "risk_on", "hot"];
const RLABEL = { risk_off: "Risk-off", neutral: "Neutral", risk_on: "Risk-on", hot: "Heiß" };
const COMP_LABEL = {
  score: "Marktphase gesamt", sol_trend: "SOL 24 h", sol_week: "SOL 7 Tage", meme_sector: "Meme-Sektor 24 h", breadth: "Breite",
  volume: "DEX-Volumen", launch_rate: "Launch-Rate", launch_hit: "2x-Quote Launches", launch_survival: "Launches ohne Rug", smart_flow: "Smart Money netto",
};
const STAGE_CLS = { new: "accent", growing: "good", peak: "warn", mature: "", fading: "bad" };

function Confirmed({ ok }) {
  return ok ? <span className="badge good">bestätigt</span> : <span className="badge">offen</span>;
}

function Regime({ p, tr }) {
  const cur = p.regime || p.label;
  const probs = tr?.p?.[cur];
  return (
    <div className="col" style={{ gap: 10 }}>
      <div className="row" style={{ gap: 10, flexWrap: "wrap" }}>
        <span className={`badge ${LABEL_CLS[cur] || ""}`}>{RLABEL[cur] || "–"}</span>
        <span className="t2">seit {p.regime_since ? ago(p.regime_since).replace("vor ", "") : "–"}</span>
        <span className={p.trend?.startsWith("kippt") ? "warn-ink b" : "t2"}>Trend: {p.trend || "–"}</span>
        <span className="dim">1 h {p.slope_1h == null ? "–" : `${p.slope_1h > 0 ? "+" : ""}${n(p.slope_1h, 1)} Pkt`} · 3 h {p.slope_3h == null ? "–" : `${p.slope_3h > 0 ? "+" : ""}${n(p.slope_3h, 1)} Pkt/h`} · geglättet {n(p.score_ema, 1)}</span>
      </div>
      {probs ? (
        <>
          <div className="dim" style={{ fontSize: 12 }}>Wahrscheinlichkeit für die nächste Stunde, gelernt aus {n(tr.n)} beobachteten Stundenwechseln:</div>
          <Bars items={ORDER.map((k) => ({ label: RLABEL[k], value: probs[k] }))} fmt={(v) => pct(v)} max={1} />
        </>
      ) : <div className="dim">Übergänge werden stündlich gelernt, sobald genug Verlauf da ist.</div>}
    </div>
  );
}

function Power({ dyn, lw, active }) {
  const comp = dyn?.components;
  const lead = dyn?.sector_lead || {};
  const rows = Object.entries(comp?.rows || {}).map(([k, v]) => ({ k, ...v, w: lw?.weights?.[k] }));
  const leads = Object.entries(lead).map(([k, v]) => ({ k, ...v }));
  return (
    <div className="grid g2" style={{ gap: 16 }}>
      <div>
        <div className="b" style={{ margin: "0 14px 6px" }}>Bausteine der Phase gegen die nächste Stunde</div>
        <div className="help" style={{ padding: "0 14px 6px" }}>Rangkorrelation (IC) des Werts zur Snapshot-Zeit mit der 2x-Quote der Launches der folgenden Stunde · {n(comp?.n_hours)} Stunden · bestätigt = gleiche Richtung in älterer und neuerer Hälfte und |t| ≥ 2</div>
        <Table rows={rows} rowKey={(r) => r.k} empty="Noch keine Stunde mit Launch-Ergebnis" initialSort={{ k: "ic", d: "desc" }} cols={[
          { k: "k", l: "Baustein", fmt: (r) => COMP_LABEL[r.k] || r.k },
          { k: "ic", l: "IC", r: true, fmt: (r) => <span className={sign(r.ic)}>{n(r.ic, 2)}</span> },
          { k: "ic_old", l: "älter", r: true, fmt: (r) => n(r.ic_old, 2) },
          { k: "ic_new", l: "neuer", r: true, fmt: (r) => n(r.ic_new, 2) },
          { k: "confirmed", l: "", fmt: (r) => <Confirmed ok={r.confirmed} /> },
          { k: "w", l: "gelerntes Gewicht", r: true, fmt: (r) => (r.w == null ? "–" : pct(r.w)) },
        ]} />
        {lw && (
          <div className="help" style={{ padding: "6px 14px 10px" }}>
            Gelernte Gewichte (nur aus der älteren Hälfte, halb zur Startannahme gezogen): IC in der neueren Hälfte {n(lw.ic_learned_new, 2)} gegen {n(lw.ic_fixed_new, 2)} mit festen Gewichten ·{" "}
            {lw.adopt ? <span className="badge good">besser</span> : <span className="badge">nicht besser</span>}{" "}
            {active ? "· im Einsatz" : "· nicht im Einsatz (MARKET_LEARNED_WEIGHTS aus oder nicht besser)"}
          </div>
        )}
      </div>
      <div>
        <div className="b" style={{ margin: "0 14px 6px" }}>Sektor-Rotation mit Vorlauf</div>
        <div className="help" style={{ padding: "0 14px 6px" }}>Sagt eine Sektor-Größe jetzt den Median-Kurs des Sektors in einer Stunde voraus? Mittlere Querschnitts-IC je Stunde, t-Wert, ältere und neuere Hälfte</div>
        <Table rows={leads} rowKey={(r) => r.k} empty="Noch zu wenig Stunden" cols={[
          { k: "label", l: "Größe" },
          { k: "ic", l: "IC", r: true, fmt: (r) => <span className={sign(r.ic)}>{n(r.ic, 2)}</span> },
          { k: "t", l: "t", r: true, fmt: (r) => n(r.t, 1) },
          { k: "ic_old", l: "älter", r: true, fmt: (r) => n(r.ic_old, 2) },
          { k: "ic_new", l: "neuer", r: true, fmt: (r) => n(r.ic_new, 2) },
          { k: "n_hours", l: "Stunden", r: true, fmt: (r) => n(r.n_hours) },
          { k: "confirmed", l: "", fmt: (r) => <Confirmed ok={r.confirmed} /> },
        ]} />
      </div>
    </div>
  );
}

export function PhaseBadge({ p }) {
  if (!p || p.score == null) return <span className="dim">–</span>;
  return <span className={`badge ${LABEL_CLS[p.label] || ""}`} title={`Marktphase ${n(p.score)}/100`}>{p.label_de} {n(p.score)}</span>;
}

function HeatBar({ v }) {
  if (v == null) return <span className="dim">–</span>;
  const color = v >= 60 ? "var(--good)" : v < 40 ? "var(--bad)" : "var(--warn)";
  return (
    <span className="row nowrap" style={{ gap: 6 }}>
      <span style={{ width: 54, height: 6, borderRadius: 3, background: "var(--surface-3)", display: "inline-block", position: "relative" }}>
        <b style={{ position: "absolute", left: 0, top: 0, bottom: 0, width: `${Math.max(2, v)}%`, borderRadius: 3, background: color }} />
      </span>
      <span className="b" style={{ fontVariantNumeric: "tabular-nums" }}>{n(v)}</span>
    </span>
  );
}

function PhaseChart({ history, th }) {
  if (!history || history.length < 2) return <Empty>Noch zu wenig Verlauf</Empty>;
  return (
    <EChart height={240} deps={[history]} ariaLabel="Marktphase und SOL-Preis über Zeit"
      build={(c) => baseOption(c, {
        grid: { left: 8, right: 8, top: 14, bottom: 8, containLabel: true },
        tooltip: {
          ...baseOption(c).tooltip,
          formatter: (ps) => {
            const p = history[ps[0].dataIndex];
            return `<b>${dt(p.ts)}</b>${p.backfilled ? " <span style='opacity:.6'>(nachgerechnet)</span>" : ""}`
              + tipRow(c.s[0], "Phase", p.score == null ? "–" : n(p.score)) + tipRow(c.s[2], "SOL", p.sol_usd ? `$${n(p.sol_usd, 2)}` : "–")
              + tipRow("transparent", "Launches 1 h", n(p.launches_1h)) + tipRow("transparent", "Launches mit 2x", pct(p.launch_hit2x, 1));
          },
        },
        xAxis: timeAxis(c),
        yAxis: [valueAxis(c, (v) => n(v), { min: 0, max: 100 }), valueAxis(c, (v) => `$${n(v)}`, { scale: true, splitLine: { show: false } })],
        series: [
          {
            type: "line", name: "Phase", showSymbol: false, lineStyle: { width: 2, color: c.s[0] }, itemStyle: { color: c.s[0] },
            data: history.map((p) => [p.ts * 1000, p.score]),
            markArea: {
              silent: true, data: [
                [{ yAxis: 0, itemStyle: { color: c.bad, opacity: 0.07 } }, { yAxis: th.risk_off }],
                [{ yAxis: th.risk_on, itemStyle: { color: c.good, opacity: 0.07 } }, { yAxis: th.hot }],
                [{ yAxis: th.hot, itemStyle: { color: c.warn, opacity: 0.08 } }, { yAxis: 100 }],
              ],
            },
          },
          { type: "line", name: "SOL", yAxisIndex: 1, showSymbol: false, lineStyle: { width: 1.5, color: c.s[2], type: "dashed" },
            itemStyle: { color: c.s[2] }, data: history.filter((p) => p.sol_usd).map((p) => [p.ts * 1000, p.sol_usd]) },
        ],
      })} />
  );
}

function RotationChart({ hist, sectors }) {
  const top = sectors.filter((s) => s.sector !== "other" && s.vol_share != null).slice(0, 6);
  const series = top.map((s) => ({ s, pts: (hist[s.sector] || []).filter((p) => p.vol_share != null) }));
  if (!series.some((x) => x.pts.length >= 2)) return <Empty>Volumen-Anteile über Zeit erscheinen nach ein paar Runden</Empty>;
  return (
    <EChart height={230} deps={[hist, sectors]} ariaLabel="Volumen-Anteil je Sektor über Zeit"
      build={(c) => baseOption(c, {
        legend: { bottom: 0, textStyle: { color: c.text2 }, icon: "roundRect", itemWidth: 10, itemHeight: 6 },
        grid: { left: 8, right: 14, top: 14, bottom: 30, containLabel: true },
        tooltip: { ...baseOption(c).tooltip, valueFormatter: (v) => pct(v, 1) },
        xAxis: timeAxis(c), yAxis: valueAxis(c, (v) => pct(v)),
        series: series.map(({ s, pts }, i) => ({
          type: "line", name: s.label, showSymbol: false, lineStyle: { width: 2, color: c.s[i % 8] }, itemStyle: { color: c.s[i % 8] },
          data: pts.map((p) => [p.ts * 1000, p.vol_share]),
        })),
      })} />
  );
}

function SectorDetail({ s }) {
  return (
    <div className="col" style={{ gap: 6 }}>
      <div className="dim" style={{ fontSize: 12 }}>Coins mit dem meisten Volumen in diesem Sektor (Klick öffnet die Coin-Seite):</div>
      <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
        {(s.top || []).map((t) => (
          <a key={t.mint} href={`#/coin/${t.mint}`} className="badge" title={t.mint}>
            {t.symbol || t.mint.slice(0, 6)} <span className={sign(t.ret_h1)}>{t.ret_h1 != null ? `${t.ret_h1 > 0 ? "+" : ""}${n(t.ret_h1, 0)} %` : ""}</span>
            {t.volume_h1 ? <span className="dim"> · {usd(t.volume_h1)}</span> : null}
          </a>
        ))}
        {!s.top?.length && <span className="dim">–</span>}
      </div>
    </div>
  );
}

function Evaluation({ ev }) {
  const [lens, setLens] = useState("by_phase");
  if (!ev) return <Empty>–</Empty>;
  const la = ev.launches;
  if (!la) return <Callout icon="info">{ev.note}</Callout>;
  const g = la[lens] || {};
  const rows = Object.entries(g).map(([k, v]) => ({ k, ...v }));
  const verdict = (ev.significant || {})[lens];
  const wf = (ev.walk_forward || {})[lens];
  const gate = la.gate || {};
  return (
    <div className="col" style={{ gap: 10 }}>
      <div className="row" style={{ justifyContent: "space-between", padding: "0 14px" }}>
        <span className="t2">{ev.note} Basis: {pct(la.base_hit_rate, 1)} der Launches machen 2x in 60 min.</span>
        <Seg value={lens} onChange={setLens} options={LENS} />
      </div>
      <Table rows={rows} rowKey={(r) => r.k} empty="Noch keine Gruppe" cols={[
        { k: "k", l: "Gruppe" },
        { k: "n", l: "Launches", r: true, fmt: (r) => n(r.n) },
        { k: "hit_rate", l: "2x in 60 min", r: true, fmt: (r) => <span className={r.hit_rate > la.base_hit_rate * 1.15 ? "up b" : r.hit_rate < la.base_hit_rate * 0.85 ? "down" : ""}>{pct(r.hit_rate, 1)}</span> },
        { k: "rug_rate", l: "Rugs", r: true, fmt: (r) => pct(r.rug_rate, 1) },
        { k: "median_max_ret", l: "Median Hoch", r: true, fmt: (r) => <Delta v={r.median_max_ret} d={0} arrow={false} /> },
      ]} />
      {verdict && (
        <div className="help" style={{ padding: "0 14px 10px" }}>
          {verdict.clear ? <span className="badge good">klarer Unterschied</span> : <span className="badge">noch nicht klar</span>} {verdict.text}
          {gate.candidates ? <> · Sniper-Tor im Rückblick: {n(gate.blocked)} von {n(gate.candidates)} Papier-Käufen wären in Risk-off gesperrt worden
            (deren Trefferquote {pct(gate.blocked_hit_rate, 0)}, die übrigen {pct(gate.kept_hit_rate, 0)}).</> : null}
        </div>
      )}
      {wf && (
        <div className="help" style={{ padding: "0 14px 10px" }}>
          {wf.ok ? <span className="badge good">Walk-forward bestätigt</span> : <span className="badge">Walk-forward offen</span>} Älterer Teil wählt die beste und schlechteste Gruppe, neuerer Teil prüft: {wf.text}
        </div>
      )}
    </div>
  );
}

export default function Market() {
  const [hours, setHours] = useState(24);
  const { data: d, error } = usePoll(`/api/market?hours=${hours}`, 30000);
  if (error) return <Callout kind="bad" icon="alert">{error}</Callout>;
  if (!d) return <div className="empty">Lade …</div>;
  if (!d.available) return <Callout icon="info">{d.note}</Callout>;
  const p = d.phase;
  const sv = d.service || {};
  const sw = d.switches || {};
  const comps = (p.components || []).map((c) => ({ label: c.why, value: c.points, title: `Wert ${n(c.value, 2)} × Gewicht ${n(c.weight, 2)}` }));
  const bk = (kind) => d.buckets.filter((b) => b.kind === kind);
  const hotTerm = d.terms[0];
  return (
    <div className="col" style={{ gap: 16 }}>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <Callout icon="info">
          Marktlage aus freien Quellen (DexScreener, CoinGecko) und den eigenen Daten (Launches, Smart Money). Wirkt auf Signale: <b>{sw.signal_weight ? "an" : "aus"}</b> · Sniper-Tor: <b>{sw.sniper_gate ? "an" : "aus"}</b> · Aufschlag bei brandneuem Narrativ: <b>{sw.sniper_new_term ? "an" : "aus"}</b>.
          Alles bleibt aus, bis die Auswertung unten einen klaren Unterschied auch auf ungesehenen Daten zeigt.
        </Callout>
        <Seg value={hours} onChange={setHours} options={[[6, "6 h"], [24, "24 h"], [168, "7 Tage"]]} />
      </div>
      <div className="tiles">
        <Tile label="Marktphase" value={<PhaseBadge p={p} />} sub={`${p.trend ? `${p.trend} · ` : ""}Abdeckung ${pct(p.coverage)} · Stand ${ago(p.ts)}`} />
        <Tile label="SOL 24 h" value={pctSigned(p.sol_ret_24h, 1)} cls={sign(p.sol_ret_24h)} sub={`7 Tage ${pctSigned(p.sol_ret_7d, 1)} · $${n(p.sol_usd, 2)}`} />
        <Tile label="Breite" value={pct(p.breadth)} sub={`im Plus (1 h) von ${n(p.universe_n)} Coins`} />
        <Tile label="Launches" value={`${n(p.launches_1h)} / h`} sub={`2x in 60 min ${pct(p.launch_hit2x, 1)} · Rugs ${pct(p.launch_rug, 1)}`} />
        <Tile label="Smart Money 1 h" value={usdSigned(p.smart_net_usd, 1)} cls={sign(p.smart_net_usd)} sub={`${n(p.smart_buyers)} Tier-Wallets aktiv`} />
        <Tile label="Heißestes Narrativ" value={<span style={{ fontSize: 17 }}>{hotTerm ? hotTerm.term : "–"}</span>}
          sub={hotTerm ? `Hitze ${n(hotTerm.heat)} · ${n(hotTerm.lift, 1)}x häufiger als sonst` : "noch keins"} />
      </div>

      <div className="grid g-wide-left">
        <Card title="Marktphase über Zeit" sub={`0–100 · rot unter ${n(d.thresholds.risk_off)} (Risk-off), grün ab ${n(d.thresholds.risk_on)} (Risk-on), gelb ab ${n(d.thresholds.hot)} (heiß) · gestrichelt SOL`}>
          <PhaseChart history={d.history} th={d.thresholds} />
        </Card>
        <Card title="Woraus sich die Phase zusammensetzt" sub="Punkte je Eingang (Wert 0–1 × Gewicht × 100), fehlende Eingänge fallen heraus">
          {comps.length ? <Bars items={comps} fmt={(v) => n(v, 1)} max={Math.max(...comps.map((c) => c.value), 1)} /> : <Empty>–</Empty>}
        </Card>
      </div>

      <div className="grid g-wide-left">
        <Card title="Regime und Übergänge" sub={`Regime mit ±3 Punkten Puffer an den Grenzen, damit es nicht hin und her springt${d.dynamics?.ts ? ` · gelernt ${ago(d.dynamics.ts)}` : ""}`}>
          <Regime p={p} tr={d.dynamics?.transitions} />
        </Card>
        <Card title="Rotation: wer seit mehreren Runden zulegt" sub="Aufeinanderfolgende 5-min-Runden mit wachsendem (grün) oder schrumpfendem (rot) Volumen-Anteil">
          <Bars items={d.sectors.filter((s) => s.streak).sort((a, b) => b.streak - a.streak).slice(0, 8).map((s) => ({ label: s.label, value: s.streak, color: s.streak > 0 ? "var(--good)" : "var(--bad)" }))} fmt={(v) => `${v > 0 ? "+" : ""}${v}`} />
        </Card>
      </div>

      <Card title="Sektoren" sub="Hitze 0–100 aus Momentum (Median 1 h), Breite, Wachstum des Volumen-Anteils, Netto-Kauf, Smart Money und 2x-Rate neuer Launches · Klick zeigt die Top-Coins" flush>
        <Table rows={d.sectors} rowKey={(r) => r.sector} expand={(r) => <SectorDetail s={r} />} initialSort={{ k: "heat", d: "desc" }} cols={[
          { k: "label", l: "Sektor", fmt: (r) => <span className="b">{r.label}</span> },
          { k: "heat", l: "Hitze", fmt: (r) => <HeatBar v={r.heat} /> },
          { k: "n_tokens", l: "Coins", r: true, fmt: (r) => n(r.n_tokens) },
          { k: "vol_share", l: "Volumen-Anteil", r: true, fmt: (r) => pct(r.vol_share, 1) },
          { k: "vol_share_chg", l: "ggü. 1 h", r: true, fmt: (r) => (r.vol_share_chg == null ? <span className="dim">–</span> : <span className={sign(r.vol_share_chg)}>{r.vol_share_chg > 0 ? "+" : ""}{n(r.vol_share_chg * 100, 1)} Pkt</span>) },
          { k: "median_ret_h1", l: "Median 1 h", r: true, fmt: (r) => (r.median_ret_h1 == null ? "–" : <span className={sign(r.median_ret_h1)}>{r.median_ret_h1 > 0 ? "+" : ""}{n(r.median_ret_h1, 1)} %</span>) },
          { k: "net_flow_usd", l: "Netto-Kauf (geschätzt)", r: true, title: "Volumen × (Käufe − Verkäufe) / alle Trades, 1 h", fmt: (r) => <span className={sign(r.net_flow_usd)}>{usdSigned(r.net_flow_usd)}</span> },
          { k: "smart_net_usd", l: "Smart Money", r: true, fmt: (r) => <span className={sign(r.smart_net_usd)}>{usdSigned(r.smart_net_usd)}</span> },
          { k: "streak", l: "Serie", r: true, title: "Runden in Folge mit wachsendem (+) oder schrumpfendem (−) Volumen-Anteil", fmt: (r) => (r.streak ? <span className={sign(r.streak)}>{r.streak > 0 ? "+" : ""}{r.streak}</span> : <span className="dim">–</span>) },
          { k: "launches", l: "Launches 1 h", r: true, fmt: (r) => n(r.launches) },
          { k: "launch_hit2x", l: "2x-Rate", r: true, title: "Launches dieses Sektors mit 2x in 60 min (geglättet), letzte 24 h", fmt: (r) => pct(r.launch_hit2x, 1) },
        ]} />
      </Card>

      <div className="grid g2">
        <Card title="Kapitalrotation" sub="Volumen-Anteil der größten Sektoren über Zeit">
          {d.rotation?.length ? <ul className="risk-list pos" style={{ marginBottom: 8 }}>{d.rotation.map((t, i) => <li key={i}><span>→</span><span>{t}</span></li>)}</ul> : null}
          <RotationChart hist={d.sector_history} sectors={d.sectors} />
        </Card>
        <Card title="Wo das Geld sitzt" sub="Volumen-Anteil der letzten Stunde nach Market Cap und nach Alter">
          <div className="b" style={{ margin: "2px 0 6px" }}>Nach Market Cap</div>
          <Bars items={bk("mcap").map((b) => ({ label: b.bucket, value: b.vol_share, title: `${n(b.n)} Coins · Median 1 h ${n(b.median_ret_h1, 1)} % · Smart Money ${usdSigned(b.smart_net_usd)}` }))} fmt={(v) => pct(v)} />
          <div className="b" style={{ margin: "12px 0 6px" }}>Nach Alter</div>
          <Bars items={bk("age").map((b) => ({ label: b.bucket, value: b.vol_share, title: `${n(b.n)} Coins · Median 1 h ${n(b.median_ret_h1, 1)} % · Smart Money ${usdSigned(b.smart_net_usd)}` }))} fmt={(v) => pct(v)} />
        </Card>
      </div>

      <Card title="Narrative: was gerade auftaucht" sub="Wörter in Namen neuer Launches (letzte 3 h) gegen die letzten 24 h · Hitze vor allem aus dem Anstieg, dazu Verbreitung, Volumen und ob diese Launches besser liefen" flush>
        <Table rows={d.terms} rowKey={(r) => r.term} initialSort={{ k: "heat", d: "desc" }} empty="Noch kein Narrativ häufig genug" cols={[
          { k: "term", l: "Narrativ", fmt: (r) => <span className="b">{r.term}</span> },
          { k: "heat", l: "Hitze", fmt: (r) => <HeatBar v={r.heat} /> },
          { k: "stage", l: "Stufe", title: "neu < 1 h · wachsend +15 % ggü. 1 h · Höhepunkt ≥ 80 % des Hochs · abklingend < 50 % des Hochs", fmt: (r) => (r.stage_de ? <span className={`badge ${STAGE_CLS[r.stage] || ""}`}>{r.stage_de}</span> : "–") },
          { k: "n_recent", l: "Launches 3 h", r: true, fmt: (r) => n(r.n_recent) },
          { k: "growth_1h", l: "ggü. 1 h", r: true, fmt: (r) => <Delta v={r.growth_1h} d={0} arrow={false} /> },
          { k: "lift", l: "Anstieg", r: true, title: "Anteil an den Launches jetzt / in den letzten 24 h", fmt: (r) => `${n(r.lift, 1)}x` },
          { k: "hit2x", l: "2x-Rate", r: true, title: "geglättet mit 10 durchschnittlichen Launches", fmt: (r) => pct(r.hit2x, 1) },
          { k: "rug", l: "Rugs", r: true, fmt: (r) => pct(r.rug, 1) },
          { k: "volume_h1_usd", l: "Volumen 1 h", r: true, fmt: (r) => usd(r.volume_h1_usd) },
          { k: "since", l: "Erstmals", r: true, fmt: (r) => (r.since ? ago(r.since) : "–") },
          { k: "mints", l: "Beispiele", sort: false, fmt: (r) => <span className="row" style={{ gap: 4 }}>{(r.mints || []).slice(0, 3).map((m) => <a key={m} className="mono" href={`#/coin/${m}`} onClick={(e) => { e.preventDefault(); go(`/coin/${m}`); }}>{m.slice(0, 4)}…</a>)}</span> },
        ]} />
      </Card>

      <Card title="Was sagt die nächste Stunde voraus?" sub="Jeder Baustein und jede Sektor-Größe gegen echte Ergebnisse, stündlich neu gemessen, ohne Blick in die Zukunft" flush>
        <Power dyn={d.dynamics} lw={d.learned_weights} active={d.learned_active} />
      </Card>

      <Card title="Taugt die Marktanalyse?" sub="Echte Ergebnisse der Launches, gruppiert nach dem Markt-Stand im Moment der Entscheidung (ohne Blick in die Zukunft)" flush>
        <Evaluation ev={d.evaluation} />
      </Card>

      <div className="help">
        Dienst market: {sv.up ? <span className="up">läuft</span> : <span className="down">{sv.ts ? `steht (letzte Meldung ${ago(sv.ts)})` : "noch keine Meldung"}</span>}
        {sv.ts ? <> · Runde {n(sv.rounds)} in {n(sv.round_s, 1)} s · {n(sv.tokens_refreshed)} Coins aufgefrischt · Abfragen seit Start: DexScreener {n(sv.calls?.dexscreener)}, CoinGecko {n(sv.calls?.coingecko)} · keine Helius-Credits</> : null}
        {sv.last_error ? <span className="warn-ink"> · letzter Fehler: {sv.last_error}</span> : null}
      </div>
    </div>
  );
}
