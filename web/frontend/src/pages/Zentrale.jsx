import { useState } from "react";
import { ago, api, dt, go, mc, n, pct, pctSigned, sign, short, usd, usdSigned, usePoll, TIER_LABEL } from "../lib.js";
import { Bars, Callout, Card, Empty, Seg, Table, Tile } from "../ui.jsx";

const TONE = { good: "good", bad: "bad", warn: "warn", "": "" };
const UNIT = {
  usd: (v) => usd(v), pct: (v) => pct(v, 1), n: (v) => n(v, v != null && Math.abs(v) < 10 && v % 1 ? 1 : 0), x: (v) => (v == null ? "–" : `${n(v, 1)}x`),
  pts: (v) => (v == null ? "–" : `${v > 0 ? "+" : ""}${n(v, 1)} Pkt`), h: (v) => (v == null ? "–" : `${n(v, 1)} h`),
  min: (v) => (v == null ? "–" : `${n(v, 1)} min`), s: (v) => (v == null ? "–" : `${n(v)} s`), sol: (v) => (v == null ? "–" : `${n(v, 3)} SOL`),
};
const fmtU = (u, v) => (UNIT[u] || UNIT.n)(v);
const PHASE = { risk_off: ["Risk-off", "bad"], neutral: ["Neutral", ""], risk_on: ["Risk-on", "good"], hot: ["Heiß", "warn"] };

function Flags({ f }) {
  if (!f?.length) return <span className="dim">–</span>;
  return <span className="row" style={{ gap: 4, flexWrap: "wrap" }}>{f.map(([k, t], i) => <span key={i} className={`badge ${TONE[k] || ""}`}>{t}</span>)}</span>;
}

function Cov({ v }) {
  if (v == null) return <span className="dim">–</span>;
  const color = v >= 0.7 ? "var(--good)" : v >= 0.4 ? "var(--warn)" : "var(--bad)";
  return (
    <span title={`${pct(v)} der Merkmale bekannt`} style={{ width: 40, height: 6, borderRadius: 3, background: "var(--surface-3)", display: "inline-block", position: "relative" }}>
      <b style={{ position: "absolute", left: 0, top: 0, bottom: 0, width: `${Math.max(3, v * 100)}%`, borderRadius: 3, background: color }} />
    </span>
  );
}

function Dossier({ mint }) {
  const { data: d } = usePoll(`/api/intel/coin/${mint}`, 30000);
  if (!d) return <div className="dim">Lade …</div>;
  if (!d.available) return <div className="dim">{d.note || "Keine Daten"}</div>;
  return (
    <div className="col" style={{ gap: 10 }}>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <Flags f={d.flags} />
        <span className="row" style={{ gap: 10 }}>
          <span className="dim">Stand {ago(d.ts)}</span>
          <a href={`#/coin/${mint}`} onClick={(e) => { e.preventDefault(); go(`/coin/${mint}`); }}>Coin-Seite öffnen</a>
        </span>
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(250px, 1fr))", gap: 14, maxWidth: 1100 }}>
        {d.groups.map((g) => (
          <div key={g.group} className="col" style={{ gap: 3 }}>
            <div style={{ display: "grid", gridTemplateColumns: "1fr auto", gap: 12, alignItems: "center" }}>
              <b>{g.label}</b><Cov v={d.coverage?.[g.group]} />
            </div>
            {g.items.map((it) => (
              <div key={it.feature} style={{ display: "grid", gridTemplateColumns: "1fr auto", gap: 12, fontSize: 12 }}>
                <span className="t2">{it.label}</span>
                <span className={it.value == null ? "dim" : "b"} style={{ fontVariantNumeric: "tabular-nums" }}>{it.value == null ? "unbekannt" : fmtU(it.unit, it.value)}</span>
              </div>
            ))}
          </div>
        ))}
      </div>
      {d.events?.length ? (
        <div className="col" style={{ gap: 4 }}>
          <b>Ereignisse dieses Coins</b>
          {d.events.slice(0, 8).map((e, i) => <div key={i} style={{ fontSize: 12 }}><span className={`badge ${TONE[e.tone] || ""}`}>{e.kind_label}</span> <span className="dim">{dt(e.ts)}</span> {e.text}</div>)}
        </div>
      ) : null}
    </div>
  );
}

function Ticker({ events, kinds }) {
  const [k, setK] = useState("all");
  const opts = [["all", "Alle"], ["good", "Einstiege"], ["bad", "Warnungen"], ["rotation", "Rotation"]];
  const rows = events.filter((e) => k === "all" || (k === "rotation" ? e.kind === "rotation" : (kinds[e.kind]?.tone === k || (k === "bad" && kinds[e.kind]?.tone === "warn"))));
  return (
    <div className="col">
      <div style={{ padding: "0 14px 8px" }}><Seg value={k} onChange={setK} options={opts} /></div>
      {!rows.length ? <Empty>Noch keine Ereignisse</Empty> : (
        <div className="feed" style={{ maxHeight: 620, overflowY: "auto" }}>
          {rows.slice(0, 80).map((e) => (
            <div key={e.id} className="feed-i" onClick={() => e.mint && go(`/coin/${e.mint}`)} title={e.text}>
              <span className={`badge ${TONE[e.tone] || ""}`} style={{ minWidth: 92, justifyContent: "center" }}>{e.kind_label}</span>
              <span><div className="feed-sym">{e.title}</div><div className="feed-sub">{e.text}</div></span>
              <span className="dim nowrap" style={{ fontSize: 12 }}>{ago(e.ts)}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function Evaluation({ ev }) {
  if (!ev) return <Empty>Die erste Messung kommt gut eine Stunde nach dem Start des Dienstes.</Empty>;
  const rows = (ev.features || []).filter((r) => r.n > 0);
  return (
    <div className="col" style={{ gap: 8 }}>
      <div className="t2" style={{ padding: "0 14px" }}>{ev.note} Ein Merkmal gilt als hilfreich, wenn das beste Fünftel klar öfter (oder seltener) pumpt als das schlechteste (|z| ≥ 2, je Fünftel mindestens 40 Stichproben).</div>
      <Table rows={rows} rowKey={(r) => r.feature} empty="Noch keine Stichproben" cols={[
        { k: "label", l: "Merkmal", fmt: (r) => <span><span className="b">{r.label}</span> <span className="dim">{r.group_label}</span></span> },
        { k: "clear", l: "Ergebnis", sort: (r) => (r.clear ? 2 : r.z != null ? 1 : 0), fmt: (r) => (r.clear ? <span className="badge good">hilft · {r.direction}</span> : r.z != null ? <span className="badge">noch nicht klar</span> : <span className="dim">zu wenig Daten</span>) },
        { k: "n", l: "Stichproben", r: true, fmt: (r) => n(r.n) },
        { k: "coverage", l: "Abdeckung", r: true, fmt: (r) => pct(r.coverage) },
        { k: "ic", l: "Rang-Korrelation", r: true, title: "Spearman mit dem Höchststand in 1 h", fmt: (r) => (r.ic == null ? "–" : <span className={sign(r.ic)}>{n(r.ic, 3)}</span>) },
        { k: "hit_top", l: "Pumps oberes Fünftel", r: true, fmt: (r) => pct(r.hit_top, 1) },
        { k: "hit_bottom", l: "unteres Fünftel", r: true, fmt: (r) => pct(r.hit_bottom, 1) },
        { k: "rug_top", l: "Abstürze oben / unten", r: true, sort: false, fmt: (r) => (r.rug_top == null ? "–" : `${pct(r.rug_top, 0)} / ${pct(r.rug_bottom, 0)}`) },
        { k: "z", l: "z", r: true, sort: (r) => (r.z == null ? null : Math.abs(r.z)), fmt: (r) => (r.z == null ? "–" : n(r.z, 1)) },
      ]} />
    </div>
  );
}

function Setups({ s }) {
  if (!s?.length) return null;
  return <span className="row" style={{ gap: 4, flexWrap: "wrap", marginTop: 3 }}>{s.map(([k, t, lift], i) => (
    <span key={i} className={`badge ${k === "good" ? "accent" : "bad"}`} title={`Bestätigtes Setup aus dem Playbook · ${n(lift, 1)}x Basis-Pumprate`}>▶ {t}</span>))}</span>;
}

function EventStudy({ rows }) {
  return (
    <Table rows={rows || []} rowKey={(r) => r.kind} empty="Ergebnisse kommen, sobald Ereignisse eine Stunde alt sind" cols={[
      { k: "label", l: "Ereignis", fmt: (r) => <span className={`badge ${TONE[r.tone] || ""}`}>{r.label}</span> },
      { k: "n", l: "Anzahl", r: true, fmt: (r) => n(r.n) },
      { k: "med_r5", l: "Median 5 min", r: true, fmt: (r) => <span className={sign(r.med_r5)}>{pctSigned(r.med_r5)}</span> },
      { k: "med_r15", l: "15 min", r: true, fmt: (r) => <span className={sign(r.med_r15)}>{pctSigned(r.med_r15)}</span> },
      { k: "med_r60", l: "60 min", r: true, fmt: (r) => <span className={sign(r.med_r60)}>{pctSigned(r.med_r60)}</span> },
      { k: "med_max", l: "Median Hoch", r: true, fmt: (r) => pctSigned(r.med_max) },
      { k: "hit_rate", l: "Pumps +50 %", r: true, fmt: (r) => pct(r.hit_rate, 1) },
      { k: "crash_rate", l: "Abstürze −50 %", r: true, fmt: (r) => pct(r.crash_rate, 1) },
      { k: "clear", l: "", sort: false, fmt: (r) => (r.clear ? <span className={`badge ${r.z > 0 ? "good" : "bad"}`}>{r.z > 0 ? "besser" : "schlechter"} als Basis</span> : <span className="dim">noch offen</span>) },
    ]} />
  );
}

function Playbook({ pb }) {
  if (!pb) return <Empty>–</Empty>;
  if (!pb.good?.length && !pb.bad?.length) return <Empty>{pb.note || `${n(pb.tested)} Setups geprüft, noch keins sicher genug`}</Empty>;
  const cols = [
    { k: "text", l: "Setup", fmt: (r) => <span className="b">{r.text}</span> },
    { k: "confirmed", l: "", sort: (r) => (r.confirmed ? 1 : 0), fmt: (r) => (r.confirmed ? <span className="badge good">bestätigt</span> : <span className="badge">nur gefunden</span>) },
    { k: "found", l: "Gefunden (älter)", r: true, sort: (r) => r.found.hit, fmt: (r) => `${pct(r.found.hit, 1)} · ${n(r.found.n)}×` },
    { k: "lift", l: "ggü. Basis", r: true, fmt: (r) => `${n(r.lift, 1)}x` },
    { k: "check", l: "Prüfung (neuer)", r: true, sort: (r) => r.check?.hit, fmt: (r) => (r.check ? `${pct(r.check.hit, 1)} · ${n(r.check.n)}×` : "–") },
    { k: "crash", l: "Abstürze", r: true, sort: (r) => r.found.crash, fmt: (r) => pct(r.found.crash, 0) },
  ];
  return (
    <div className="col" style={{ gap: 8 }}>
      <div className="t2" style={{ padding: "0 14px" }}>
        {n(pb.tested)} Setups aus {n(Object.keys(pb.conditions || {}).length || 33)} Trader-Situationen und ihren Paaren, gefunden auf den älteren {n(pb.n_found)} Stichproben
        (Basis {pct(pb.base_found, 1)} Pumps), geprüft auf den neuesten {n(pb.n_check)} (Basis {pct(pb.base_check, 1)}). Mehrfach-Tests korrigiert (Benjamini-Hochberg).
      </div>
      <div className="b" style={{ padding: "0 14px" }}>Kaufen-Setups</div>
      <Table rows={pb.good} rowKey={(r) => r.text} empty="noch keins" cols={cols} />
      <div className="b" style={{ padding: "0 14px" }}>Finger weg</div>
      <Table rows={pb.bad} rowKey={(r) => r.text} empty="noch keins" cols={cols} />
    </div>
  );
}

function Robustness({ dp }) {
  const rows = (dp?.robustness || []).filter((r) => r.verdict !== "kein Effekt");
  const V = { robust: "good", "nur Größe/Alter": "warn", instabil: "" };
  return (
    <div className="col" style={{ gap: 8 }}>
      <div className="t2" style={{ padding: "0 14px" }}>Robust heißt: nach Korrektur für viele Tests signifikant (q ≤ 5 %), gleiche Richtung in älterer und neuerer Hälfte, und auch dann wirksam, wenn man Market Cap, Alter und Liquidität herausrechnet.</div>
      <Table rows={rows} rowKey={(r) => r.feature} empty="Noch kein Merkmal mit Effekt" cols={[
        { k: "label", l: "Merkmal", fmt: (r) => <span><span className="b">{r.label}</span> <span className="dim">{r.group_label}</span></span> },
        { k: "verdict", l: "Urteil", fmt: (r) => <span className={`badge ${V[r.verdict] || ""}`}>{r.verdict}</span> },
        { k: "ic", l: "Korrelation", r: true, fmt: (r) => <span className={sign(r.ic)}>{n(r.ic, 3)}</span> },
        { k: "ic_old", l: "älter / neuer", r: true, sort: false, fmt: (r) => `${n(r.ic_old, 3)} / ${n(r.ic_new, 3)}` },
        { k: "partial_ic", l: "ohne Größe/Alter", r: true, fmt: (r) => n(r.partial_ic, 3) },
        { k: "by_phase", l: "je Marktphase", sort: false, fmt: (r) => Object.values(r.by_phase || {}).map((p) => `${p.label} ${n(p.ic, 2)}`).join(" · ") || "–" },
        { k: "q", l: "q", r: true, fmt: (r) => pct(r.q, 1) },
      ]} />
      {dp?.redundancy?.length ? (
        <div style={{ padding: "0 14px 10px" }} className="t2">
          <b>Messen dasselbe:</b> {dp.redundancy.slice(0, 12).map((x) => `${x.a_label} ≈ ${x.b_label} (${n(x.rho, 2)})`).join(" · ")}
        </div>
      ) : null}
    </div>
  );
}

export default function Zentrale() {
  const { data: d, error } = usePoll("/api/intel", 15000);
  const [view, setView] = useState("all");
  if (error) return <Callout kind="bad" icon="alert">{error}</Callout>;
  if (!d) return <div className="empty">Lade …</div>;
  if (!d.available) return <Callout icon="info">{d.note}</Callout>;
  const sv = d.service || {};
  const mf = d.market?.features || {};
  const ph = PHASE[d.market?.meta?.label] || ["–", ""];
  const tape = d.flows?.tape || {};
  const smartNet = ["ultra", "smart"].reduce((a, t) => a + ((tape[t]?.buy || 0) - (tape[t]?.sell || 0)), 0);
  const evs = d.evaluation;
  const clear = (evs?.features || []).filter((r) => r.clear).length;
  const srcOk = d.sources.filter((s) => s.ok).length;
  const coins = d.coins.filter((c) => view === "all" || (view === "smart" ? (c.flow_smart_entities_60m || 0) >= 1 : c.flags.some(([k]) => k === "bad")));
  const ec = d.event_counts_24h || {};
  return (
    <div className="col" style={{ gap: 16 }}>
      <Callout icon="info">
        Die Zentrale führt alles zusammen, was der Bot sieht: Geld- und Orderfluss nach Personen, Liquidität, Holder-Struktur, Kursregime, Markt und Narrativ.
        Jeder Wert gilt für einen Zeitpunkt und nutzt nur Daten bis dahin; <b>unbekannt</b> heißt unbekannt, nicht null. Unten steht, welche Merkmale nachweislich helfen.
      </Callout>
      <div className="tiles dense">
        <Tile label="Marktphase" value={<span className={`badge ${ph[1]}`}>{ph[0]} {n(mf.m_phase)}</span>} sub={`SOL 1 h ${pctSigned(mf.m_sol_ret_1h)} · 24 h ${pctSigned(mf.m_sol_ret_24h)}`} />
        <Tile label="Smart Money 60 min" value={usdSigned(smartNet)} cls={sign(smartNet)} sub={`Ultra + Smart, ${n((tape.ultra?.n || 0) + (tape.smart?.n || 0))} Trades`} />
        <Tile label="Alle Swaps 60 min" value={n(Object.values(tape).reduce((a, t) => a + (t.n || 0), 0))} sub={`Kauf ${usd(Object.values(tape).reduce((a, t) => a + (t.buy || 0), 0))} · Verkauf ${usd(Object.values(tape).reduce((a, t) => a + (t.sell || 0), 0))}`} />
        <Tile label="Launches / h" value={n(mf.m_launches_1h)} sub={`2x ${pct(mf.m_launch_hit2x, 1)} · Rug ${pct(mf.m_launch_rug, 1)}`} />
        <Tile label="Ereignisse 24 h" value={n(Object.values(ec).reduce((a, b) => a + b, 0))} sub={`Einstiege ${n((ec.smart_entry || 0) + (ec.ultra_buy || 0))} · Ausstiege/Warnungen ${n((ec.smart_exit || 0) + (ec.insider_sell || 0) + (ec.liq_drop || 0) + (ec.risk_jump || 0))}`} />
        <Tile label="Coins im Blick" value={n(d.coins.length)} sub={`Merkmale je Coin ${n(Object.keys(d.feature_labels).length)}`} />
        <Tile label="Hilfreiche Merkmale" value={evs?.n ? `${n(clear)} von ${n((evs.features || []).length)}` : "–"} sub={evs?.n ? `${n(evs.n)} Stichproben` : "Messung startet"} />
        <Tile label="Bitcoin / Ethereum 24 h" value={<span className={sign(mf.m_btc_chg_24h)}>{pctSigned(mf.m_btc_chg_24h)}</span>} sub={`ETH ${pctSigned(mf.m_eth_chg_24h)}`} />
        <Tile label="Fear & Greed" value={n(mf.m_fng)} sub={d.market?.meta?.fng_label || "0 Angst … 100 Gier"} />
        <Tile label="Datenquellen" value={`${n(srcOk)} / ${n(d.sources.length)}`} sub={srcOk === d.sources.length ? "alle frisch" : "einige veraltet"} cls={srcOk === d.sources.length ? "up" : ""} />
      </div>

      <div className="grid g-wide-left">
        <Card title="Lagebild" sub="Coins mit Aktivität, sortiert nach unabhängigen Smart-Money-Käufern (verbundene Wallets zählen als eine Person) · Klick öffnet das Dossier"
          actions={<Seg value={view} onChange={setView} options={[["all", "Alle"], ["smart", "Smart Money"], ["bad", "Warnungen"]]} />} flush>
          <Table rows={coins} rowKey={(r) => r.mint} expand={(r) => <Dossier mint={r.mint} />} tall cols={[
            { k: "symbol", l: "Coin", fmt: (r) => <span className="b" title={r.mint}>{r.symbol || short(r.mint)}</span> },
            { k: "flags", l: "Lage", sort: (r) => r.flags.length + 3 * (r.setups?.length || 0), fmt: (r) => <span><Flags f={r.flags} /><Setups s={r.setups} /></span> },
            { k: "px_mcap", l: "MC", r: true, fmt: (r) => mc(r.px_mcap) },
            { k: "flow_smart_entities_60m", l: "Smart-Personen", r: true, title: "netto kaufend, 60 min", fmt: (r) => n(r.flow_smart_entities_60m) },
            { k: "flow_smart_net_usd_60m", l: "Smart netto", r: true, fmt: (r) => <span className={sign(r.flow_smart_net_usd_60m)}>{usdSigned(r.flow_smart_net_usd_60m)}</span> },
            { k: "flow_net_usd_15m", l: "Netto 15 min", r: true, fmt: (r) => <span className={sign(r.flow_net_usd_15m)}>{usdSigned(r.flow_net_usd_15m)}</span> },
            { k: "flow_buy_ratio_15m", l: "Kaufdruck", r: true, fmt: (r) => pct(r.flow_buy_ratio_15m) },
            { k: "vol_roundtrip_60m", l: "Wash", r: true, fmt: (r) => pct(r.vol_roundtrip_60m) },
            { k: "liq_usd", l: "Liquidität", r: true, fmt: (r) => usd(r.liq_usd) },
            { k: "exec_rt_0_25", l: "Kosten 0,25 SOL", r: true, sort: (r) => r.exec_q_0_25 ?? r.exec_rt_0_25, title: "hin und zurück: gemessen mit Jupiter (fett) oder geschätzt aus dem Pool", fmt: (r) => (r.exec_q_0_25 != null ? <b>{pct(r.exec_q_0_25, 1)}</b> : <span className="t2">{pct(r.exec_rt_0_25, 1)}</span>) },
            { k: "tape_insider_held", l: "Insider halten", r: true, title: "Anteil ihres Launch-Kaufs, laut Swaps", fmt: (r) => pct(r.tape_insider_held) },
            { k: "net_dev_rug_rate", l: "Dev-Rugs", r: true, fmt: (r) => pct(r.net_dev_rug_rate) },
            { k: "micro_vpin", l: "VPIN", r: true, title: "Toxizität: wie einseitig der Fluss über Volumen-Buckets ist", fmt: (r) => pct(r.micro_vpin) },
            { k: "hold_top10", l: "Top-10", r: true, fmt: (r) => pct(r.hold_top10) },
            { k: "px_ret_15m", l: "15 min", r: true, fmt: (r) => <span className={sign(r.px_ret_15m)}>{pctSigned(r.px_ret_15m)}</span> },
            { k: "coverage", l: "Daten", r: true, fmt: (r) => <Cov v={r.coverage} /> },
          ]} />
        </Card>
        <Card title="Live-Ticker" sub="Was gerade passiert, neueste zuerst · Klick öffnet den Coin" flush>
          <Ticker events={d.events} kinds={d.event_kinds} />
        </Card>
      </div>

      <div className="grid g2">
        <Card title="Smart-Money-Fluss 60 min" sub="Netto-Käufe der Ultra- und Smart-Wallets je Coin">
          {d.flows.inflow.length ? <Bars items={[...d.flows.inflow.filter((r) => r.net_usd > 0).slice(0, 8), ...d.flows.outflow.filter((r) => r.net_usd < 0).slice(0, 5)].map((r) => ({
            label: r.symbol || short(r.mint), value: r.net_usd, title: `${n(r.entities)} Personen · Kauf ${usd(r.buy_usd)} · Verkauf ${usd(r.sell_usd)}`,
          }))} fmt={(v) => usdSigned(v)} /> : <Empty>Keine Smart-Money-Trades in der letzten Stunde</Empty>}
        </Card>
        <Card title="Wohin das Geld rotiert" sub={`Smart-Wallet verkauft Coin A und kauft binnen 30 min Coin B · letzte ${d.rotation.hours} h`} flush>
          {d.rotation.magnets?.length ? (
            <div className="row" style={{ gap: 6, flexWrap: "wrap", padding: "0 14px 8px" }}>
              <span className="dim">Ziele:</span>{d.rotation.magnets.filter((m) => m.net_usd > 0).map((m) => <a key={m.mint} className="badge good" href={`#/coin/${m.mint}`}>{m.symbol || short(m.mint)} {usdSigned(m.net_usd)}</a>)}
            </div>
          ) : null}
          <Table rows={d.rotation.edges} rowKey={(r) => r.from + r.to} empty="Noch keine Rotation erkannt" cols={[
            { k: "from_symbol", l: "Von", fmt: (r) => <a href={`#/coin/${r.from}`}>{r.from_symbol || short(r.from)}</a> },
            { k: "to_symbol", l: "Nach", fmt: (r) => <a href={`#/coin/${r.to}`} className="b">{r.to_symbol || short(r.to)}</a> },
            { k: "wallets", l: "Wallets", r: true, fmt: (r) => n(r.wallets) },
            { k: "usd", l: "Betrag", r: true, fmt: (r) => usd(r.usd) },
            { k: "last", l: "Zuletzt", r: true, fmt: (r) => ago(r.last) },
          ]} />
        </Card>
      </div>

      <Card title="Wallet-Profile" sub="Wie die beobachteten Wallets handeln (30 Tage): Haltedauer, Scalps, Konzentration, Mitkäufer, mitgenommener Anteil vom Hoch je Marktphase, Verspätung unserer Erkennung" flush>
        <Table rows={d.wallets.rows} rowKey={(r) => r.address} tall max={30} empty="Wallet-Profile entstehen in der ersten Stunde" cols={[
          { k: "address", l: "Wallet", fmt: (r) => <a href={`#/wallet/${r.address}`} className="mono">{short(r.address)}</a> },
          { k: "tier", l: "Stufe", fmt: (r) => (r.tier ? <span className={`tier ${r.tier}`}>{TIER_LABEL[r.tier]}</span> : <span className="dim">–</span>) },
          { k: "w_n_coins_30d", l: "Coins", r: true, fmt: (r) => n(r.w_n_coins_30d) },
          { k: "w_hold_median_min", l: "Haltedauer", r: true, fmt: (r) => fmtU("min", r.w_hold_median_min) },
          { k: "w_scalp_share", l: "Scalps < 5 min", r: true, fmt: (r) => pct(r.w_scalp_share) },
          { k: "w_concentration", l: "Lieblings-Coin", r: true, fmt: (r) => pct(r.w_concentration) },
          { k: "w_cluster_size", l: "Gruppe", r: true, fmt: (r) => n(r.w_cluster_size) },
          { k: "w_cobuy_share", l: "Mitkäufer 60 s", r: true, fmt: (r) => pct(r.w_cobuy_share) },
          { k: "w_exit_capture", l: "Vom Hoch mitgenommen", r: true, fmt: (r) => pct(r.w_exit_capture) },
          { k: "w_exit_capture_hot", l: "… heiß / Risk-off", r: true, sort: false, fmt: (r) => `${pct(r.w_exit_capture_hot)} / ${pct(r.w_exit_capture_risk_off)}` },
          { k: "w_latency_median_s", l: "Verspätung", r: true, fmt: (r) => fmtU("s", r.w_latency_median_s) },
        ]} />
      </Card>

      <Card title="Playbook: Setups wie ein Trader" sub="Welche Situationen nachweislich öfter pumpen oder abstürzen: gefunden auf älteren Daten, bestätigt auf neueren (walk-forward). Bestätigte Setups stehen im Lagebild am Coin." flush>
        <Playbook pb={d.deep?.playbook && { ...d.deep.playbook, conditions: d.deep.conditions }} />
      </Card>

      <div className="grid g2">
        <Card title="Was nach Ereignissen passiert" sub="Ereignis-Studie: Kurs nach 5, 15 und 60 min, Hoch und Absturz in der Stunde danach, verglichen mit der Basis-Pumprate" flush>
          <EventStudy rows={d.deep?.events} />
        </Card>
        <Card title="Robustheit und Redundanz" sub="Welche Merkmale wirklich und dauerhaft etwas sagen, und welche dasselbe messen" flush>
          <Robustness dp={d.deep} />
        </Card>
      </div>

      <Card title="Taugt das? Messung je Merkmal" sub="Jeder Stand bekommt eine Stunde später sein echtes Ergebnis (Höchststand, Absturz); je Coin und Stunde zählt eine Stichprobe" flush>
        <Evaluation ev={evs} />
      </Card>

      <div className="grid g2">
        <Card title="Datenquellen" sub="Was die Zentrale gerade sieht · alles kostenlos, keine zusätzlichen Abfragen" flush>
          <Table rows={d.sources} rowKey={(r) => r.key} cols={[
            { k: "label", l: "Quelle", fmt: (r) => <span className="b">{r.label}</span> },
            { k: "n_1h", l: "Einträge 1 h", r: true, fmt: (r) => n(r.n_1h) },
            { k: "age_s", l: "Zuletzt", r: true, fmt: (r) => (r.last ? ago(r.last) : "–") },
            { k: "ok", l: "Status", fmt: (r) => (r.ok ? <span className="badge good">frisch</span> : <span className="badge warn">{r.last ? "veraltet" : "keine Daten"}</span>) },
          ]} />
        </Card>
        <Card title="Ausbaustufen" sub="Die Zentrale wächst; jede Stufe wird gemessen, bevor das Gehirn sie nutzt">
          <ul className="risk-list">
            {d.stages.map((s) => (
              <li key={s.n}>
                <span className={`badge ${s.status === "live" ? "good" : s.status === "next" ? "accent" : ""}`}>{s.status === "live" ? "läuft" : s.status === "next" ? "als Nächstes" : "geplant"}</span>
                <span><b>{s.n}. {s.title}</b> <span className="t2">{s.text}</span></span>
              </li>
            ))}
          </ul>
        </Card>
      </div>

      <div className="help">
        Dienst intel: {sv.up ? <span className="up">läuft</span> : <span className="down">{sv.ts ? `steht (letzte Meldung ${ago(sv.ts)})` : "noch keine Meldung"}</span>}
        {sv.ts ? <> · Runde {n(sv.rounds)} in {n(sv.round_s, 2)} s · keine API-Abfragen, keine Helius-Credits</> : null}
        {sv.last_error ? <span className="warn-ink"> · letzter Fehler: {sv.last_error}</span> : null}
      </div>
    </div>
  );
}
