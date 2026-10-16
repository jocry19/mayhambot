import { TIER_SOURCE, ago, n, usePoll } from "../lib.js";
import { Card, Table, Tile } from "../ui.jsx";

const LABELS = {
  trades: "Trades", tokens: "Coins", wallets: "Wallets", wallet_positions: "Wallet-Positionen", token_prices: "Preis-Schnappschüsse",
  signal_metrics: "Score-Messungen", signals: "Signale", model_predictions: "Modell-Prognosen", model_versions: "Modell-Versionen",
  model_wallet_stats: "Wallet-Bewertungen", ct_positions: "Bot-Positionen", ct_swaps: "Wal-Swaps", ct_wallets: "Beobachtete Wale",
  ct_events: "Bot-Protokoll", ct_equity: "Equity-Punkte", research_candles: "Kerzen", research_reports: "Forschungsberichte",
};

export default function System() {
  const { data: d } = usePoll("/api/system", 10000);
  if (!d) return <div className="empty">Lade …</div>;
  const rows = Object.entries(d.tables).map(([t, c]) => ({ t, c, label: LABELS[t] || "" }));
  return (
    <div className="col" style={{ gap: 16 }}>
      <div className="tiles">
        <Tile label="Datenbank" value={d.db_size ? `${n(d.db_size / 1e6, 1)} MB` : "–"} sub={d.db_path.split("/").slice(-2).join("/")} />
        <Tile label="Letzter Trade" value={ago(d.last.trades)} sub="Signal-Engine sammelt" />
        <Tile label="Letzte Score-Messung" value={ago(d.last.signal_metrics)} />
        <Tile label="Letztes Bot-Ereignis" value={ago(d.last.ct_events)} />
        <Tile label="Letztes Modell" value={ago(d.last.model_versions)} />
        <Tile label="SOL-Preis (aus Trades)" value={`$${n(d.sol_usd, 0)}`} />
      </div>
      <div className="grid g2">
        <Card title="Dienste" sub="Prozesse der App (python mayham.py status)" flush>
          <Table rows={d.services} rowKey={(r) => r.name} empty="Keine Prozess-Infos (App läuft woanders oder als launchd/systemd-Dienst)" cols={[
            { k: "name", l: "Dienst" },
            { k: "running", l: "Status", fmt: (r) => <span className={`badge ${r.running ? "good" : "bad"}`}>{r.running ? "läuft" : "aus"}</span> },
            { k: "pid", l: "PID", r: true },
          ]} />
          <div className="help" style={{ padding: "8px 14px" }}>{TIER_SOURCE[d.tier_source]}.</div>
        </Card>
        <Card title="Tabellen" sub="Datensätze in der gemeinsamen Datenbank" flush>
          <Table rows={rows} rowKey={(r) => r.t} tall initialSort={{ k: "c", d: "desc" }} cols={[
            { k: "t", l: "Tabelle", fmt: (r) => <span className="mono">{r.t}</span> },
            { k: "label", l: "" , fmt: (r) => <span className="t2">{r.label}</span> },
            { k: "c", l: "Zeilen", r: true, fmt: (r) => n(r.c) },
          ]} />
        </Card>
      </div>
    </div>
  );
}
