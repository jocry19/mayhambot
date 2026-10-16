import { TIER_SOURCE, ago, compact, dt, n, pct, usePoll } from "../lib.js";
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

function CreditBar({ used, cap, v, share }) {
  return (
    <span className="col" style={{ gap: 2 }} title={`${n(used)} von ${n(cap)} Credits${share != null ? ` · ${n(share)} % des Monatsbudgets` : ""}`}>
      <LoadBar v={v} />
      <span className="dim" style={{ fontSize: 11 }}>{compact(used)} / {compact(cap)}{share != null ? ` · ${n(share)} %` : ""}</span>
    </span>
  );
}

/** Whole-month Helius line for the top of the page. */
export function CreditsTotal({ t }) {
  if (!t) return null;
  const cls = t.load >= 1 ? "bad" : t.load >= 0.8 ? "warn" : "";
  return (
    <div className={`credits-total ${cls}`}>
      <span className="b">Helius-Credits diesen Monat:</span>
      <span className="num">{n(t.used)} von {n(t.limit)}</span>
      <span className={`load ${cls}`}><span className="load-track wide"><span className="load-fill" style={{ width: `${Math.min(100, t.load * 100)}%` }} /></span>
        <span className="load-num">{pct(t.load, 1)}</span></span>
      {t.key_fp && <span className="t2" title="Fingerabdruck des Helius-Keys (nie der Key selbst)">Key …<span className="mono">{t.key_fp}</span>{t.key_changed_at ? ` · gewechselt ${dt(t.key_changed_at)}` : ""}</span>}
      {t.load >= 0.8 && <span className="t2">Ab 100 % lehnt Helius ab; die Dienste weichen dann auf das öffentliche RPC aus.</span>}
      {t.history?.length > 0 && (
        <details className="credits-hist">
          <summary className="t2">Frühere Zähler ({t.history.length})</summary>
          <table className="tbl" style={{ fontSize: 12 }}>
            <thead><tr><th>Monat</th><th>Key</th><th className="r">verbraucht</th><th>bis</th><th>Grund</th></tr></thead>
            <tbody>{t.history.map((h, i) => (
              <tr key={i}><td>{h.month}</td><td className="mono">{h.key_fp ? `…${h.key_fp}` : "–"}</td><td className="r">{n(h.used)}</td>
                <td>{h.until ? dt(h.until) : "–"}</td><td>{{ month: "Monatswechsel", key: "neuer Key" }[h.reason] || h.reason || "–"}</td></tr>
            ))}</tbody>
          </table>
        </details>
      )}
    </div>
  );
}

function ApiUsage({ u }) {
  if (!u) return null;
  const rows = u.rows || [];
  const credits = rows.some((r) => r.credits_cap != null || r.credits_month != null);
  const lanes = rows.some((r) => r.live_calls != null || r.bulk_calls != null);
  const lane = (r, k) => {
    if (r[`${k}_calls`] == null) return <span className="dim">–</span>;
    const c = r[`${k}_calls_window`], w = r[`${k}_waited_s_window`];
    const slow = k === "live" && (w || 0) >= 2;
    return (
      <span title={`${n(r[`${k}_calls`])} Aufrufe und ${n(r[`${k}_waited_s`], 1)} s Wartezeit seit Start`}>
        {c != null ? n(c) : "–"} <span className={slow ? "warn-ink" : "dim"}>· {w != null ? `${n(w, 1)} s` : "–"}</span>
      </span>
    );
  };
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
            ...(lanes ? [
              { k: "live_calls_window", l: "live", r: true, sort: (r) => r.live_waited_s_window ?? -1,
                title: "Öffentliches RPC, schnelle Spur (Copy-Trader-Reaktion): Aufrufe · Wartezeit in den letzten 10 min; gelb ab 2 s", fmt: (r) => lane(r, "live") },
              { k: "bulk_calls_window", l: "bulk", r: true, sort: (r) => r.bulk_waited_s_window ?? -1,
                title: "Öffentliches RPC, Massen-Spur (Forschung, Nachladen): Aufrufe · Wartezeit in den letzten 10 min", fmt: (r) => lane(r, "bulk") },
            ] : []),
            { k: "budget_per_min", l: "Budget/min", r: true, fmt: (r) => (r.budget_per_min ? n(r.budget_per_min) : <span className="dim">ohne</span>) },
            { k: "load", l: "Auslastung", sort: (r) => r.load ?? -1, fmt: (r) => <LoadBar v={r.load} /> },
            ...(credits ? [
              { k: "load_day", l: "Credits heute", title: "Helius-Credits heute gegen das Tagesbudget des Dienstes (Rest des Monatsanteils ÷ übrige Tage)", sort: (r) => r.load_day ?? -1,
                fmt: (r) => (r.day_full && !r.credits_day_budget
                  ? <span className="badge bad" title="Monatsanteil aufgebraucht: Tagesbudget 0, der Dienst nutzt bis Monatsende das öffentliche RPC">voll</span>
                  : r.credits_day_budget != null ? <CreditBar used={r.credits_today} cap={r.credits_day_budget} v={r.load_day} /> : "–") },
              { k: "load_month", l: "Credits Monat", title: "Helius-Credits diesen Monat gegen den Anteil des Dienstes", sort: (r) => r.load_month ?? -1,
                fmt: (r) => (r.credits_cap != null ? <CreditBar used={r.credits_month} cap={r.credits_cap} v={r.load_month} share={r.share_pct} /> : "–") },
              { k: "refused", l: "Abgewiesen", r: true, title: "Aufrufe, die wegen aufgebrauchter Credits nicht an Helius gingen (seit Start des Dienstes)",
                fmt: (r) => (r.refused != null ? <span className={r.refused ? "down" : "dim"}>{n(r.refused)}</span> : "–") },
            ] : []),
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
      <CreditsTotal t={d.api_usage?.credits} />
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
