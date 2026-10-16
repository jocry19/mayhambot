import { useState } from "react";
import { ago, go, mc, n, pct, usdSigned, usePoll } from "../lib.js";
import { CoinLink } from "../parts.jsx";
import { RiskBadge } from "../safety.jsx";
import { Card, Delta, Score, Seg, Table, TierCounts } from "../ui.jsx";

const SORTS = [["strength", "Signalstärke"], ["inflow", "Inflow 30 min"], ["inflow24", "Inflow 24 h"], ["wallets", "Tier-Käufer"],
  ["signals", "Meiste Signale"], ["recent", "Neuestes Signal"], ["change", "1-h-Anstieg"], ["mcap", "Market Cap"]];

export default function Coins() {
  const [sort, setSort] = useState("strength");
  const [search, setSearch] = useState("");
  const { data } = usePoll(`/api/coins?sort=${sort}&limit=300${search ? `&search=${encodeURIComponent(search)}` : ""}`, 15000);
  return (
    <div className="col" style={{ gap: 16 }}>
      <div className="row" style={{ gap: 12 }}>
        <Seg value={sort} onChange={setSort} options={SORTS} />
        <span className="sp" />
        <input className="in" placeholder="Filtern …" value={search} onChange={(e) => setSearch(e.target.value)} style={{ width: 200 }} aria-label="Coins filtern" />
      </div>
      <Card title="Rangliste Coins" sub="Signalstärke = aktueller Score der Signal-Engine bzw. des Modells; Tier-Käufer = Wallets, die netto kaufen" flush>
        <Table rows={data} rowKey={(r) => r.mint} onRow={(r) => go(`/coin/${r.mint}`)} tall max={300} cols={[
          { k: "rank", l: "#", sort: false, fmt: (r) => <span className="dim">{data.indexOf(r) + 1}</span> },
          { k: "symbol", l: "Coin", fmt: (r) => <span><CoinLink mint={r.mint} symbol={r.symbol} /> <span className="dim">{r.age_h != null ? `${n(r.age_h, r.age_h < 10 ? 1 : 0)} h alt` : ""}</span></span> },
          { k: "strength", l: "Stärke", fmt: (r) => <Score v={r.strength} g={r.grade} /> },
          { k: "safety", l: "Rug-Risiko", sort: (r) => r.safety?.score ?? -1, fmt: (r) => <RiskBadge s={r.safety} /> },
          { k: "p_1h", l: "Pump-Chance", r: true, title: "Chance auf +50 % innerhalb 1 h", fmt: (r) => (r.p_1h != null ? pct(r.p_1h) : "–") },
          { k: "buyers_win", l: "Käufer 30 min", fmt: (r) => <TierCounts tiers={r.buyers_win} />, sort: (r) => r.buyers_win.ultra * 100 + r.buyers_win.smart * 10 + r.buyers_win.good },
          { k: "net_tiered_win", l: "Inflow 30 min", r: true, fmt: (r) => <span className={r.net_tiered_win > 0 ? "up" : r.net_tiered_win < 0 ? "down" : "dim"}>{usdSigned(r.net_tiered_win)}</span> },
          { k: "net_tiered_24h", l: "Inflow 24 h", r: true, fmt: (r) => <span className={r.net_tiered_24h > 0 ? "up" : r.net_tiered_24h < 0 ? "down" : "dim"}>{usdSigned(r.net_tiered_24h)}</span> },
          { k: "buyers_24h", l: "Käufer 24 h", fmt: (r) => <TierCounts tiers={r.buyers_24h} />, sort: (r) => r.buyers_24h.ultra * 100 + r.buyers_24h.smart * 10 + r.buyers_24h.good },
          { k: "mcap_usd", l: "MC", r: true, fmt: (r) => mc(r.mcap_usd) },
          { k: "liquidity_usd", l: "Liquidität", r: true, fmt: (r) => mc(r.liquidity_usd) },
          { k: "change_h1", l: "1 h", r: true, fmt: (r) => <Delta v={r.change_h1 != null ? r.change_h1 / 100 : null} d={0} arrow={false} /> },
          { k: "change_h24", l: "24 h", r: true, fmt: (r) => <Delta v={r.change_h24 != null ? r.change_h24 / 100 : null} d={0} arrow={false} /> },
          { k: "signals", l: "Signale", r: true, fmt: (r) => (r.signals ? <span title={`zuletzt ${ago(r.last_signal_ts)}`}>{r.signals}</span> : <span className="dim">0</span>) },
          { k: "since_first_signal", l: "Seit 1. Signal", r: true, title: "Market Cap jetzt gegenüber dem ersten Signal", fmt: (r) => <Delta v={r.since_first_signal} d={0} arrow={false} /> },
        ]} />
      </Card>
    </div>
  );
}
