import { TIER_SOURCE, ago, compact, dt, dur, n, pct, usePoll } from "../lib.js";
import { Callout, Card, Empty, Table, Tile } from "../ui.jsx";
import { SafetyQuality } from "../safety.jsx";

const GROUP = { helius: "Helius (RPC)", geckoterminal: "GeckoTerminal" };

/** Share of a limit as a thin bar + number; warning colour from 80 %, critical from 100 %. */
export function LoadBar({ v }) {
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

/** When the month's plan runs out at the current rate (core.http.cap_forecast), with and without the sniper's stream. */
export function CapForecast({ t }) {
  if (!t || (t.cap_at === undefined && t.cap_at_no_stream === undefined)) return null;
  const when = (x) => (x ? dt(x) : "nicht mehr in diesem Monat");
  const soon = t.cap_at && t.cap_at - Date.now() / 1000 < 3 * 86400;
  return (
    <span className={soon ? "t2 down" : "t2"} title="Hochgerechnet aus der Rate der letzten 6 Stunden (vorher dem Tagesmittel); danach lehnt Helius ab und die Dienste gehen aufs öffentliche RPC">
      {t.rate_per_h != null && <>{n(t.rate_per_h)} Credits je Stunde · </>}Kappe bei aktueller Rate: <b>{when(t.cap_at)}</b> (ohne Sniper-Strom 2: {when(t.cap_at_no_stream)})
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
      <CapForecast t={t} />
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
                fmt: (r) => (r.day_full && (r.borrowed > 0 || r.borrow_left > 0)
                  ? <span className="col" style={{ gap: 2 }}>
                    <span className="badge warn" title={`Eigener Tagesanteil aufgebraucht. Der Dienst leiht ungenutzte Credits anderer Dienste (HELIUS_BORROW_PCT) und läuft weiter auf Helius.${r.lenders?.length ? ` Größte Verleiher: ${r.lenders.join(", ")}.` : ""}`}>
                      voll, leiht{r.lenders?.length ? ` von ${r.lenders[0]}` : ""}</span>
                    <span className="dim" style={{ fontSize: 11 }}>{compact(r.borrowed || 0)} geliehen · noch {compact(r.borrow_left || 0)}</span>
                  </span>
                  : r.day_full && !r.credits_day_budget
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

/** Keyless public Solana RPC endpoints (core.http pool): state, answer time, calls in the last 10 min. */
function RpcPool({ p }) {
  if (!p || !p.available) return null;
  const slow = p.slow_ms || 1000;
  const win = p.window_s ? (p.window_s >= 540 ? "10 min" : dur(p.window_s / 60)) : "10 min";
  return (
    <Card title="Öffentliche RPC-Endpunkte"
      sub={`${n(p.active)} von ${n(p.total)} aktiv${p.fastest_ms != null ? ` · schnellster ${p.fastest_host} mit ${n(p.fastest_ms)} ms` : ""} · Aufrufe der letzten ${win}, alle Dienste`} flush>
      <Table rows={p.rows} rowKey={(r) => r.host} empty="Keine Endpunkte eingetragen" cols={[
        { k: "host", l: "Endpunkt", fmt: (r) => <span className="mono">{r.host}</span> },
        { k: "state", l: "Status", fmt: (r) => (r.state === "pausiert"
          ? <span><span className="badge bad">pausiert</span> <span className="dim">bis {dt(r.disabled_until)}{r.reason ? ` · ${r.reason}` : ""}</span></span>
          : <span className={`badge ${r.state === "aktiv" ? "good" : ""}`}>{r.state}</span>) },
        { k: "latency_ms", l: "Antwortzeit", r: true, sort: (r) => r.latency_ms ?? 1e9, fmt: (r) => (r.latency_ms != null
          ? <span className={r.latency_ms > slow ? "warn-ink" : ""} title={r.latency_src === "Probe" ? "aus der letzten Prüfung" : "Mittel der Aufrufe"}>{n(r.latency_ms)} ms{r.latency_src === "Probe" ? <span className="dim"> · Probe</span> : ""}</span>
          : <span className="dim">–</span>) },
        { k: "calls_window", l: "Aufrufe", r: true, fmt: (r) => (r.calls_window != null ? n(r.calls_window) : "–") },
        { k: "429_window", l: "429", r: true, fmt: (r) => (r["429_window"] ? <span className="down">{n(r["429_window"])}</span> : r["429_window"] === 0 ? "0" : "–") },
        { k: "live_share", l: "Live / Bulk", r: true, fmt: (r) => (r.live_share != null
          ? <span title={`${n(r.live_window)} live · ${n(r.bulk_window)} bulk`}>{pct(r.live_share)} / {pct(1 - r.live_share)}</span> : "–") },
        { k: "services", l: "Genutzt von", sort: false, fmt: (r) => (r.services?.length ? r.services.join(", ") : <span className="dim">–</span>) },
      ]} />
    </Card>
  );
}

const STATE = { ok: ["läuft", "good"], langsam: ["verzögert", "warn"], steht: ["steht", "bad"], neu: ["noch keine Daten", ""] };
const LSRC = { pumpportal: "PumpPortal", rpc: "Solana-RPC" };
const every = (s) => (s == null ? "–" : s < 120 ? `${n(s)} s` : s < 7200 ? `${n(s / 60)} min` : `${n(s / 3600)} h`);

function svcDetail(r) {
  if (r.name === "sniper" && r.last_ts) {
    return [r.connected ? "Datenstrom verbunden" : "Datenstrom getrennt",
      r.launch_source ? `Launches zuerst über ${LSRC[r.launch_source] || r.launch_source}${r.launches_per_min != null ? ` (${n(r.launches_per_min, 1)}/min)` : ""}` : "",
      r.trade_source ? `Trades über ${LSRC[r.trade_source] || r.trade_source}${r.trade_source === "rpc" ? (r.rpc_ws_connected ? ", Websocket 2 verbunden" : ", Websocket 2 getrennt") : ""}` : ""].filter(Boolean).join(" · ");
  }
  return r.what;
}

/** All app services: process, newest data each one wrote, overdue or not. */
function Health({ rows }) {
  if (!rows?.length) return null;
  const bad = rows.filter((r) => r.state === "steht");
  return (
    <Card title="Zustand der Dienste" sub={bad.length ? `${bad.length} von ${rows.length} stehen: ${bad.map((r) => r.label).join(", ")}` : `alle ${rows.length} Dienste liefern Daten oder starten gerade`} flush>
      <Table rows={rows} rowKey={(r) => r.name} cols={[
        { k: "label", l: "Dienst", fmt: (r) => <span><span className="b">{r.label}</span> <span className="mono dim" style={{ fontSize: 11.5 }}>{r.name}</span></span> },
        { k: "state", l: "Status", sort: (r) => ["steht", "langsam", "neu", "ok"].indexOf(r.state), fmt: (r) => {
          const [l, c] = STATE[r.state] || [r.state, ""];
          return <span className={`badge ${c}`} title={r.running === false ? "Prozess läuft nicht" : r.running == null ? "Prozess-Status unbekannt (kein PID-File), bewertet nach den Daten" : "Prozess läuft"}>{l}</span>;
        } },
        { k: "last_ts", l: "Letzte Daten", r: true, sort: (r) => r.last_ts || 0, fmt: (r) => (r.name === "web" ? "jetzt" : r.last_ts ? <span className={r.state === "steht" ? "down" : r.state === "langsam" ? "warn-ink" : ""}>{ago(r.last_ts)}</span> : "–") },
        { k: "expected_s", l: "Erwartet alle", r: true, fmt: (r) => (r.expected_s ? every(r.expected_s) : "–") },
        { k: "what", l: "Aufgabe und Stand", sort: false, fmt: (r) => <span className="t2" style={{ whiteSpace: "normal", display: "inline-block", maxWidth: 440 }}>{svcDetail(r)}</span> },
        { k: "log_ts", l: "Log", r: true, sort: (r) => r.log_ts || 0, fmt: (r) => (r.log_ts ? <span className="dim">{ago(r.log_ts)}</span> : "–") },
      ]} />
    </Card>
  );
}

/** Backup, watchdog and auto-update as tiles; open watchdog problems as one callout. */
function Ops({ b }) {
  if (!b) return null;
  const au = b.autoupdate || {};
  const probs = Object.values(b.problems || {}).filter(Boolean);
  return (
    <>
      <div className="tiles wrap-sub">
        <Tile label="Letztes Backup" value={b.last_ts ? <span className={b.ok ? "" : "down"}>{ago(b.last_ts)}</span> : <span className="down">keins</span>}
          sub={b.last_ts ? `${n(b.count)} Backups · ${n((b.last_size || 0) / 1e6, 1)} MB · stündlich erwartet` : "Backups liegen in BACKUP_DIR (Standard ~/mayhambot-backups)"} />
        <Tile label="Wächter" value={b.watchdog_ts ? ago(b.watchdog_ts) : "–"} sub={b.watchdog_ts ? (probs.length ? `${probs.length} offene Probleme` : "keine offenen Probleme") : "läuft auf diesem Rechner nicht"} />
        <Tile label="Auto-Update" value={au.disabled ? "aus" : au.blocked ? <span className="down">blockiert</span> : au.red_streak >= 2 ? <span className="down">hängt</span>
          : au.deployed ? <span className="mono" style={{ fontSize: 17 }}>{au.deployed}</span> : "–"}
          sub={au.disabled ? "abgeschaltet (data/NO_AUTO_UPDATE)"
            : au.blocked ? <span className="down">lokaler Commit auf dem Mac · bleibt auf {au.deployed || "?"}</span>
            : au.red_streak >= 2 ? <span className="down">Tests {au.red_streak}× in Folge rot · bleibt auf {au.deployed || "?"}</span>
            : au.last_deploy ? `eingespielt ${ago(au.last_deploy)}${au.red_streak ? " · letzte Runde rot" : ""}${au.bad ? ` · ${au.bad} zurückgerollt` : ""}` : "noch kein automatisches Update"} />
      </div>
      {au.blocked && (
        <Callout kind="bad" icon="alert">
          <div className="b">Das Update ist blockiert:</div>
          <div>{au.blocked}</div>
          <div className="muted" style={{ fontSize: 12 }}>Auf dem Mac im Ordner prüfen: git status und git log origin/main..HEAD zeigen den lokalen Commit.</div>
        </Callout>
      )}
      {au.red_tests?.length > 0 && (
        <Callout kind="bad" icon="alert">
          <div className="b">Diese Tests halten das Update auf:</div>
          <div className="mono" style={{ fontSize: 12, whiteSpace: "pre-wrap" }}>{au.red_tests.join("\n")}</div>
        </Callout>
      )}
      {probs.length > 0 && <Callout kind="bad" icon="alert">Wächter meldet: {probs.join(" · ")}</Callout>}
    </>
  );
}

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
      <Health rows={d.health} />
      <Ops b={d.backup} />
      <SafetyQuality q={d.safety_quality} />
      <CreditsTotal t={d.api_usage?.credits} />
      <ApiUsage u={d.api_usage} />
      <RpcPool p={d.rpc_pool} />
      <div className="grid g2">
        <Card title="Prozesse" sub="python mayham.py status" flush>
          <Table rows={d.services} rowKey={(r) => r.name} empty="Keine Prozess-Infos (App läuft woanders oder als launchd/systemd-Dienst)" cols={[
            { k: "name", l: "Dienst" },
            { k: "running", l: "Status", fmt: (r) => <span className={`badge ${r.running ? "good" : "bad"}`}>{r.running ? "läuft" : "aus"}</span> },
            { k: "pid", l: "PID", r: true },
          ]} />
          <div className="help" style={{ padding: "8px 14px" }}>{TIER_SOURCE[d.tier_source]}.</div>
        </Card>
        <Card title="Tabellen" sub="Datensätze in der gemeinsamen Datenbank, alle 5 min gezählt" flush>
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
