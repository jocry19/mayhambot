import { TIER_SOURCE, ago, compact, n, pct, usePoll } from "../lib.js";
import { Card, Empty, Table, Tile } from "../ui.jsx";

const GROUP = { helius: "Helius (RPC)", geckoterminal: "GeckoTerminal" };

/** Share of a limit as a thin bar + number; warning colour from 80 %, critical from 100 %. */
function LoadBar({ v }) {
  if (v == null) return <span className="dim">–</span>;
  const cls = v >= 1 ? "bad" : v >= 0.8 ? "warn" : "";
  return (
    <span className={`load ${cls}`} title={v >= 0.8 ? "Budget fast ausgeschöpft: der Dienst wartet vor weiteren Abfragen" : undefined}>
      <span className="load-track"><span className="load-fill" style={{ width: `${Math.min(100, v * 100)}%` }} /></span>
      <span className="load-num">{pct(v)}</span>
    </span>
  );
}

function ApiUsage({ u }) {
  if (!u) return null;
  const rows = u.rows || [];
  const credits = rows.some((r) => r.credits_month != null);
  const win = Math.max(0, ...rows.map((r) => r.window_s || 0));
  return (
    <Card title="API-Verbrauch je Dienst" flush
      sub={`Abfragen pro Dienst und Anbieter · Auslastung = Abfragen der letzten Minute ÷ Budget pro Minute${rows.length && win < u.window_s - 30 ? (win ? ` · 10-min-Fenster bisher ${n(win / 60)} min` : " · 10-min-Zähler startet mit der nächsten Messung (jede Minute)") : ""}`}>
      {!u.available ? <Empty>{u.note}</Empty> : (
        <Table rows={rows} rowKey={(r) => `${r.service}|${r.group}`} empty="Noch keine Verbrauchsdaten (die Dienste schreiben sie jede Minute)"
          initialSort={{ k: "load", d: "desc" }} cols={[
            { k: "service", l: "Dienst", fmt: (r) => <span>{r.service}{r.stale ? <span className="badge" style={{ marginLeft: 6 }} title={`letzter Stand ${ago(r.ts)}`}>inaktiv</span> : null}</span> },
            { k: "group", l: "Anbieter", fmt: (r) => GROUP[r.group] || <span className="mono">{r.group}</span> },
            { k: "calls_window", l: "Calls 10 min", r: true, title: `Abfragen in den letzten 10 min (seit Start des Dienstes: Tooltip)`,
              fmt: (r) => <span title={`${n(r.calls)} seit Start des Dienstes`}>{r.calls_window != null ? n(r.calls_window) : "–"}</span> },
            { k: "last_min", l: "Letzte Minute", r: true, fmt: (r) => n(r.last_min) },
            { k: "429", l: "429", r: true, title: "Zu viele Anfragen: der Anbieter hat gebremst (seit Start)", fmt: (r) => <span className={r["429"] ? "down" : "dim"}>{n(r["429"])}</span> },
            { k: "waited_s", l: "Wartezeit", r: true, title: "Sekunden, die der Dienst wegen Budget oder Bremse gewartet hat (seit Start)", fmt: (r) => (r.waited_s ? `${n(r.waited_s)} s` : "–") },
            { k: "budget_per_min", l: "Budget/min", r: true, fmt: (r) => (r.budget_per_min ? n(r.budget_per_min) : <span className="dim">ohne</span>) },
            { k: "load", l: "Auslastung", sort: (r) => r.load ?? -1, fmt: (r) => <LoadBar v={r.load} /> },
            ...(credits ? [{ k: "credits_month", l: "Credits Monat", r: true, sort: (r) => (r.credits_cap ? r.credits_month / r.credits_cap : -1),
              fmt: (r) => (r.credits_month != null ? <span title={[r.credits_cap ? `Monat ${n(r.credits_month)} von ${n(r.credits_cap)}` : "", r.credits_day_budget ? `heute ${n(r.credits_today)} von ${n(r.credits_day_budget)}` : "", r.share_pct != null ? `Anteil ${n(r.share_pct)} %` : "", r.refused ? `${n(r.refused)} Abfragen wegen Budget abgelehnt` : ""].filter(Boolean).join(" · ") || undefined}>
                {compact(r.credits_month)}{r.credits_cap ? <> / {compact(r.credits_cap)} <LoadBar v={r.credits_month / r.credits_cap} /></> : ""}</span> : "–") }] : []),
          ]} />
      )}
    </Card>
  );
}

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
      <ApiUsage u={d.api_usage} />
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
