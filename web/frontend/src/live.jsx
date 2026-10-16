import { useState } from "react";
import { act, ago, dt, n, pct, sol, solSigned, usePoll } from "./lib.js";
import { CoinLink } from "./parts.jsx";
import { Callout, Card, Icon, Modal, Table, Tile } from "./ui.jsx";

const STATE = {
  off: ["Aus", "", "LIVE_TRADING ist in der .env aus. Alles läuft auf Papier."],
  dry: ["Dry-Run", "warn", "Generalprobe: Orders werden gebaut, signiert und simuliert, aber nie gesendet. Gebucht wird als Konto „Dry-Run“."],
  live: ["Echtgeld", "live", "Orders werden wirklich gesendet. Die Grenzen unten sind harte Obergrenzen."],
};

/** Real-money path (live/): state, go-live checklist, hard limits, kill switch and the newest orders. Read-only
 *  except the kill switch, which only ever switches trading OFF; releasing it stays on the Mac or in Telegram. */
export function LiveCard() {
  const { data: d } = usePoll("/api/live", 10000);
  const [ask, setAsk] = useState(false);
  const [why, setWhy] = useState("");
  if (!d) return null;
  if (!d.available) return <Card title="Echtgeld-Pfad"><Callout icon="info">Noch nicht verfügbar{d.error ? `: ${d.error}` : ""}.</Callout></Card>;
  const [label, tone, expl] = STATE[d.state] || STATE.off;
  const L = d.limits || {};
  const req = d.checks.filter((c) => c.required);
  const passed = req.filter((c) => c.ok).length;
  const bk = d.books?.[d.state === "live" ? "live" : "dry"] || {};
  return (
    <Card title="Echtgeld-Pfad" sub={`${L.stage_name || "–"} · Go-Live-Prüfung ${passed}/${req.length} erfüllt`}
      actions={<span className={`badge ${tone}`}>{label}</span>}>
      {d.killed ? <Callout kind="bad" icon="alert"><span className="b">Not-Aus aktiv:</span> {d.killed}. Keine neuen Echtgeld-Käufe, Verkäufe laufen weiter.
        Aufheben geht auf dem Mac mit <span className="mono">python -m live resume</span> oder in Telegram mit <span className="mono">/weiter</span>.</Callout>
        : <Callout kind={d.state === "live" ? "bad" : ""} icon={d.state === "live" ? "alert" : "info"}>{expl}</Callout>}
      <div className="tiles wrap-sub" style={{ marginTop: 12 }}>
        <Tile label="Go-Live-Prüfung" value={d.ready ? <span className="up">bestanden</span> : <span className="down">offen</span>}
          sub={d.require_gate ? "echte Orders erst nach bestandener Prüfung" : "Prüfung abgeschaltet (LIVE_REQUIRE_GATE=false)"} />
        <Tile label={d.state === "live" ? "Offen (Echtgeld)" : "Offen (Dry-Run)"} value={n(bk.open)} sub={`Einsatz ${sol(bk.exposure_sol, 3)} von max. ${sol(L.max_exposure_sol, 2)}`} />
        <Tile label="Ergebnis 24 h" value={<span className={(bk.pnl_24h_sol || 0) >= 0 ? "up" : "down"}>{solSigned(bk.pnl_24h_sol)}</span>}
          sub={`Tagesverlust-Grenze −${sol(L.max_daily_loss_sol, 2)}`} />
        <Tile label="Fehler in Folge" value={<span className={d.consecutive_fails ? "down" : ""}>{d.consecutive_fails}</span>}
          sub={`ab ${d.max_consecutive_fails} stoppt der Bot selbst${d.pending ? ` · ${d.pending} unklare Order(s)` : ""}`} />
        <Tile label="Live-Wächter" value={d.heartbeat ? ago(d.heartbeat) : "–"} sub={d.heartbeat ? "letztes Lebenszeichen" : "noch nie gelaufen"} />
      </div>
      <div className="grid g2" style={{ marginTop: 12, gap: 16 }}>
        <div>
          <div className="b" style={{ marginBottom: 6 }}>Go-Live-Prüfung</div>
          <ul className="checks">
            {d.checks.map((c) => (
              <li key={c.key} className={c.ok ? "ok" : c.required ? "bad" : "opt"}>
                <Icon name={c.ok ? "check" : "alert"} size={14} />
                <div><div>{c.label}{!c.required && <span className="dim"> (optional)</span>}</div><div className="t2">{c.detail}</div></div>
              </li>
            ))}
          </ul>
          {d.advance && (
            <div className="t2" style={{ marginTop: 8 }}>
              Nächste Stufe ({d.advance.next}): {n(d.advance.round_trips)}/{n(d.advance.min_trips)} Echtgeld-Runden, netto {solSigned(d.advance.net_sol)},
              Slippage {d.advance.median_buy_slippage != null ? pct(d.advance.median_buy_slippage, 2) : "–"} (Papier rechnet {pct(d.advance.paper_assumes, 2)})
              {d.advance.ok ? " · bereit" : ""}
            </div>
          )}
        </div>
        <div>
          <div className="b" style={{ marginBottom: 6 }}>Harte Grenzen</div>
          <div className="kvs">
            <div className="kv"><span className="k">Position max.</span><span className="v">{sol(L.max_position_sol, 2)}</span></div>
            <div className="kv"><span className="k">Offene Positionen max.</span><span className="v">{n(L.max_open)}</span></div>
            <div className="kv"><span className="k">Einsatz gesamt max.</span><span className="v">{sol(L.max_exposure_sol, 2)}</span></div>
            <div className="kv"><span className="k">Tagesverlust max.</span><span className="v">{sol(L.max_daily_loss_sol, 2)}</span></div>
            <div className="kv"><span className="k">Reserve für Gebühren</span><span className="v">{sol(L.reserve_sol, 3)}</span></div>
            <div className="kv"><span className="k">Wallet größer als</span><span className="v">{sol(L.max_wallet_sol, 1)} wird abgelehnt</span></div>
            <div className="kv"><span className="k">Preiseinfluss max.</span><span className="v">{pct(L.max_price_impact, 1)}</span></div>
            <div className="kv"><span className="k">Slippage Kauf / Verkauf</span><span className="v">{n((L.buy_slippage_bps || 0) / 100, 1)} % / {n((L.sell_slippage_bps || 0) / 100, 1)} %</span></div>
          </div>
          <div className="dim" style={{ fontSize: 12, marginTop: 6 }}>Stufe und Grenzen stehen in der .env (LIVE_STAGE); dort lassen sie sich nur enger stellen.</div>
        </div>
      </div>
      {!d.killed && (
        <div className="row" style={{ marginTop: 14, gap: 12 }}>
          <button className="btn danger" onClick={() => setAsk(true)}><Icon name="alert" size={14} /> Not-Aus</button>
          <span className="dim">Stoppt sofort alle neuen Echtgeld- und Dry-Run-Käufe. Verkäufe laufen weiter.</span>
        </div>
      )}
      {d.orders?.length > 0 && (
        <div style={{ marginTop: 14 }}>
          <div className="b" style={{ marginBottom: 6 }}>Letzte Orders</div>
          <Table rows={d.orders} rowKey={(r) => r.id} cols={[
            { k: "ts", l: "Zeit", fmt: (r) => dt(r.ts) },
            { k: "dry", l: "Art", fmt: (r) => (r.dry ? <span className="badge">Dry-Run</span> : <span className="badge live">echt</span>) },
            { k: "side", l: "", fmt: (r) => (r.side === "buy" ? "Kauf" : "Verkauf") },
            { k: "mint", l: "Coin", fmt: (r) => <CoinLink mint={r.mint} /> },
            { k: "status", l: "Status", fmt: (r) => <span className={`badge ${r.tone}`}>{r.status_de}</span> },
            { k: "price_impact", l: "Preiseinfluss", r: true, fmt: (r) => (r.price_impact != null ? pct(r.price_impact, 2) : "–") },
            { k: "realized_slippage", l: "Slippage", r: true, fmt: (r) => (r.realized_slippage != null ? pct(r.realized_slippage, 2) : "–") },
            { k: "error", l: "Hinweis", fmt: (r) => <span className="t2">{r.error || (r.attempt > 1 ? `Versuch ${r.attempt}` : "")}</span> },
          ]} />
        </div>
      )}
      {ask && (
        <Modal title="Not-Aus auslösen?" onClose={() => setAsk(false)} actions={<>
          <button className="btn" onClick={() => setAsk(false)}>Abbrechen</button>
          <button className="btn danger" onClick={() => act("/api/live/kill", { reason: why }).finally(() => { setAsk(false); setWhy(""); })}>Not-Aus auslösen</button>
        </>}>
          <p>Danach kauft der Bot nichts mehr mit Echtgeld oder im Dry-Run, bis du den Not-Aus auf dem Mac (<span className="mono">python -m live resume</span>) oder in Telegram (<span className="mono">/weiter</span>) aufhebst. Offene Positionen werden weiter verwaltet und verkauft.</p>
          <input className="in" style={{ width: "100%" }} placeholder="Grund (optional)" value={why} maxLength={150} onChange={(e) => setWhy(e.target.value)} />
        </Modal>
      )}
    </Card>
  );
}
