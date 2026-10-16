import { useState } from "react";
import { EChart, baseOption, tierColor, tipRow, valueAxis } from "../chart.jsx";
import { TIER_LABEL, TIER_SOURCE, ago, go, mc, n, pct, usd, usdSigned, usePoll } from "../lib.js";
import { HistBars, TierSrc, WalletLink } from "../parts.jsx";
import { Callout, Card, Empty, Legend, Score, Seg, Table, Tier, Tile } from "../ui.jsx";

const SORTS = [["score", "Score"], ["win_rate", "Trefferquote"], ["roi", "ROI"], ["total_pnl_usd", "Gewinn"],
  ["influence_followers", "Einfluss"], ["influence_usd", "Erzeugter Inflow"], ["early_rate", "Früh-Quote"], ["exit_capture", "Ausstieg"], ["copy_score", "Copy-Score"]];
const srcOf = (r) => (r.tier_source === "heuristic" ? "heuristic" : r.tier_source ? "model" : null);

export default function Wallets() {
  const [tier, setTier] = useState("");
  const [sort, setSort] = useState("score");
  const [search, setSearch] = useState("");
  const [cluster, setCluster] = useState("");
  const [group, setGroup] = useState(false);
  const { data } = usePoll(`/api/wallets?tier=${tier}&sort=${sort}&limit=500${search ? `&search=${encodeURIComponent(search)}` : ""}${cluster ? `&cluster=${encodeURIComponent(cluster)}` : ""}${group ? "&group=1" : ""}`, 30000);
  const { data: st } = usePoll("/api/wallets/stats", 60000);
  const ti = st?.tier_info;
  return (
    <div className="col" style={{ gap: 16 }}>
      {st && st.tier_source !== "model" && <Callout>{TIER_SOURCE[st.tier_source]}.</Callout>}
      {st && (
        <div className="tiles">
          {["ultra", "smart", "good"].map((k) => (
            <Tile key={k} label={<><i className="dot" style={{ background: `var(--tier-${k})`, borderRadius: 3 }} />{TIER_LABEL[k]}</>} value={n(st.counts[k])}
              sub={st.by_tier[k].n ? `Treffer ${pct(st.by_tier[k].win_rate)} · ROI ${pct(st.by_tier[k].roi)}` : "noch keine"} />
          ))}
          <Tile label="Bewertete Wallets" value={n(st.scored_wallets)} sub={`${n(st.total_wallets)} Wallets beobachtet`} />
          <Tile label="Herkunft der Stufen" value={ti ? <TierSrc src={ti.source === "model" ? "model" : "heuristic"} version={ti.version} synced={ti.synced} /> : "–"}
            sub={ti?.source === "model" ? (ti.synced ? "gilt gleich für Signale und Copy-Trading" : "wird beim nächsten Signal-Zyklus übernommen") : `${st.scored_wallets ? pct(st.tiered / st.scored_wallets, 1) : "–"} mit Tier · Modell noch nicht aktiv`} />
        </div>
      )}

      {st && (
        <div className="grid g3">
          <Card title="Tier-Profil" sub="Median je Tier: was Ultra von Good unterscheidet" flush>
            <Table rows={["ultra", "smart", "good"].map((k) => ({ k, ...st.by_tier[k] }))} rowKey={(r) => r.k} cols={[
              { k: "k", l: "Tier", fmt: (r) => <Tier t={r.k} />, sort: false },
              { k: "win_rate", l: "Treffer", r: true, fmt: (r) => pct(r.win_rate) },
              { k: "roi", l: "ROI", r: true, fmt: (r) => pct(r.roi) },
              { k: "influence_followers", l: "Follower", r: true, title: "Ø Wallets, die nach ihrem Kauf nachkaufen", fmt: (r) => n(r.influence_followers, 1) },
              { k: "influence_usd", l: "Inflow", r: true, title: "Erzeugter Inflow", fmt: (r) => usd(r.influence_usd) },
              { k: "median_entry_mcap", l: "MC", r: true, fmt: (r) => mc(r.median_entry_mcap) },
            ]} />
          </Card>
          <Card title="Score-Verteilung" sub="Alle bewerteten Wallets">
            <HistBars data={st.score_hist} label="Wallets" height={210} />
          </Card>
          <Card title="Trefferquote gegen ROI" sub="Top-800 Wallets; Farbe = Tier">
            {st.scatter.length ? (
              <>
                <EChart height={190} deps={[st.scatter]} ariaLabel="Trefferquote gegen ROI"
                  onClick={(p) => p.data?.a && go(`/wallet/${p.data.a}`)}
                  build={(c) => baseOption(c, {
                    tooltip: { ...baseOption(c).tooltip, trigger: "item", formatter: (p) => `<b class="mono">${p.data.a.slice(0, 6)}…</b>${tipRow(p.color, "Trefferquote", pct(p.data.value[0]))}${tipRow(p.color, "ROI", pct(p.data.value[1]))}${tipRow("transparent", "Score", n(p.data.s, 0))}` },
                    xAxis: valueAxis(c, (v) => `${Math.round(v * 100)} %`, { min: 0, max: 1 }),
                    grid: { left: 8, right: 22, top: 14, bottom: 8, containLabel: true },
                    yAxis: valueAxis(c, (v) => `${Math.round(v * 100)} %`),
                    series: ["other", "good", "smart", "ultra"].map((k) => ({
                      name: TIER_LABEL[k], type: "scatter", symbolSize: k === "other" ? 5 : 8,
                      itemStyle: { color: tierColor(c, k), borderColor: c.surface, borderWidth: k === "other" ? 0 : 1.5, opacity: k === "other" ? 0.55 : 1 },
                      data: st.scatter.filter((w) => w.tier === k).map((w) => ({ value: [w.win_rate, w.roi], a: w.address, s: w.score })),
                    })),
                  })} />
                <Legend items={["ultra", "smart", "good", "other"].map((k) => ({ label: TIER_LABEL[k], color: `var(--tier-${k})` }))} />
              </>
            ) : <Empty />}
          </Card>
        </div>
      )}

      <div className="row" style={{ gap: 12 }}>
        <Seg value={tier} onChange={setTier} options={[["", "Alle Tiers"], ["ultra", "Ultra Smart"], ["smart", "Smart"], ["good", "Good"]]} />
        <Seg value={sort} onChange={setSort} options={SORTS} />
        <label className="row nowrap dim" style={{ fontSize: 12.5, gap: 6 }} title="Wallets mit derselben Gruppe (gleicher Geldgeber oder gemeinsames Kaufen) sind ein Akteur: nur die beste Wallet je Gruppe zeigen">
          <input type="checkbox" checked={group} onChange={(e) => setGroup(e.target.checked)} />Gruppen zusammenfassen
        </label>
        {cluster && <button className="btn sm" onClick={() => setCluster("")}>Gruppe {cluster.slice(0, 8)} ✕</button>}
        <span className="sp" />
        <input className="in" placeholder="Adresse oder Label …" value={search} onChange={(e) => setSearch(e.target.value)} style={{ width: 220 }} aria-label="Wallets filtern" />
      </div>
      <Card title="Rangliste Smart Wallets" sub={data ? `${n(data.total)} ${group ? "Akteure" : "Wallets"}${cluster ? ` in Gruppe ${cluster.slice(0, 8)}` : ""} · Einfluss = Ø Wallets, die in den Minuten nach ihrem Kauf nachkaufen` : ""} flush>
        <Table rows={data?.items} rowKey={(r) => r.address} onRow={(r) => go(`/wallet/${r.address}`)} tall max={500}
          empty="Noch keine Wallet mit Tier. Das Modell vergibt Tiers, sobald genug Historie da ist." cols={[
            { k: "rank", l: "#", sort: false, fmt: (r) => <span className="dim">{data.items.indexOf(r) + 1 + 0}</span> },
            { k: "address", l: "Wallet", fmt: (r) => <span><WalletLink a={r.address} label={r.label || undefined} />{r.manual && <span className="badge" style={{ marginLeft: 6 }}>eigene</span>}</span> },
            { k: "tier", l: "Tier", fmt: (r) => <Tier t={r.tier} />, sort: (r) => ({ ultra: 3, smart: 2, good: 1 }[r.tier] || 0) },
            { k: "tier_source", l: "Quelle", title: "Woher die Stufe kommt: aktive Modellversion oder Heuristik", sort: (r) => r.tier_version || 0,
              fmt: (r) => <TierSrc src={srcOf(r)} version={r.tier_version} /> },
            { k: "score", l: "Score", title: "Score, nach dem die Stufe vergeben wurde (Modell, sonst Heuristik)", fmt: (r) => <Score v={r.score} /> },
            { k: "score_heuristic", l: "Heuristik", r: true, title: "Score der Wallet-Formel (Treffer, ROI, Konstanz) zum Vergleich", fmt: (r) => (r.score_heuristic != null ? n(r.score_heuristic) : "–") },
            { k: "n_tokens", l: "Coins", r: true },
            { k: "win_rate", l: "Treffer", r: true, fmt: (r) => pct(r.win_rate) },
            { k: "early_rate", l: "Früh", r: true, title: "Anteil Käufe vor einem 2x", fmt: (r) => pct(r.early_rate) },
            { k: "roi", l: "ROI", r: true, fmt: (r) => <span className={r.roi > 0 ? "up" : r.roi < 0 ? "down" : ""}>{pct(r.roi)}</span> },
            { k: "total_pnl_usd", l: "Gewinn", r: true, fmt: (r) => <span className={r.total_pnl_usd > 0 ? "up" : r.total_pnl_usd < 0 ? "down" : ""}>{usdSigned(r.total_pnl_usd)}</span> },
            { k: "influence_followers", l: "Einfluss", r: true, title: "Ø Nachkäufer nach ihrem Kauf", fmt: (r) => n(r.influence_followers, 1) },
            { k: "influence_usd", l: "Erzeugter Inflow", r: true, title: "Ø USD, die andere nach ihrem Kauf hineinstecken", fmt: (r) => usd(r.influence_usd) },
            { k: "median_entry_mcap", l: "Einstieg MC", r: true, fmt: (r) => mc(r.median_entry_mcap) },
            { k: "exit_capture", l: "Ausstieg", r: true, title: "Verkaufspreis als Anteil vom Hoch nach dem Einstieg (Median); 100 % = am Gipfel verkauft", fmt: (r) => (r.exit_capture != null ? pct(r.exit_capture) : "–") },
            { k: "median_hold_min", l: "Haltedauer", r: true, title: "Median, Minuten", fmt: (r) => (r.median_hold_min != null ? `${n(r.median_hold_min, 0)} min` : "–") },
            { k: "cluster_n", l: "Gruppe", title: "Wallets mit gleichem Geldgeber oder die wiederholt gemeinsam kaufen: ein Akteur. Klick zeigt die ganze Gruppe.",
              sort: (r) => r.cluster_n || 0,
              fmt: (r) => (r.cluster_id ? (
                <button className="badge linkish" onClick={(e) => { e.stopPropagation(); setCluster(String(r.cluster_id)); setGroup(false); }}
                  title={`Gruppe ${r.cluster_id}: alle Wallets zeigen`}>{String(r.cluster_id).slice(0, 8)}{r.cluster_n > 1 ? ` · ${r.cluster_n}` : ""}</button>
              ) : <span className="dim">–</span>) },
            { k: "copy_score", l: "Copy-Score", r: true, title: "Erwarteter Gewinn pro Kopie (Copy-Trading-Ranking)", fmt: (r) => (r.copy_score != null ? pct(r.copy_score, 1) : "–") },
            { k: "last_trade_ts", l: "Zuletzt", r: true, fmt: (r) => ago(r.last_trade_ts) },
          ]} />
      </Card>
    </div>
  );
}
