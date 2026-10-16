import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { api, mc, n, pct, refreshAll, sol, usd } from "./lib.js";
import { RiskBadge } from "./safety.jsx";
import { Callout, Modal } from "./ui.jsx";

const MODE_TXT = { off: "Aus", manual: "Manuell", semi: "Halbautomatisch", auto: "Automatisch" };

/** Buy button for a signal row or a coin: disabled while paused, already held or an order is waiting. */
export function BuyButton({ mint, symbol, signal, heldId, pendingId, paused, blocked, sm = true }) {
  const [open, setOpen] = useState(false);
  const why = paused ? "Käufe sind pausiert" : heldId ? `Schon im Coin (Position #${heldId})` : pendingId ? "Kaufauftrag wartet schon"
    : blocked ? "Rug-Schutz blockiert diesen Coin (Freigeben auf der Coin-Seite)" : "";
  return (
    <>
      <button className={`btn ${sm ? "sm" : ""} primary`} disabled={!!why} title={why || `${symbol || "Coin"} kaufen`}
        onClick={(e) => { e.stopPropagation(); setOpen(true); }}>
        {heldId ? "Gekauft" : pendingId ? "Wartet …" : blocked ? "Gesperrt" : "Kaufen"}
      </button>
      {open && createPortal(
        // portal: outside the table cell (no nowrap); the span keeps clicks from toggling the table row
        <span onClick={(e) => e.stopPropagation()}><BuyModal mint={mint} symbol={symbol} signal={signal} onClose={() => setOpen(false)} /></span>,
        document.body)}
    </>
  );
}

/** Confirmation with preset, amount and account, then the order's live result (position or error). */
export function BuyModal({ mint, symbol, signal, onClose }) {
  const [pv, setPv] = useState(null);
  const [err, setErr] = useState("");
  const [size, setSize] = useState("");
  const [busy, setBusy] = useState(false);
  const [cmd, setCmd] = useState(null); // {id, status, result, position}
  const [waited, setWaited] = useState(0);

  useEffect(() => {
    api(`/api/buy/preview?mint=${encodeURIComponent(mint)}`).then(setPv).catch((e) => setErr(e.message));
  }, [mint]);

  useEffect(() => {
    if (!cmd || cmd.status !== "pending") return undefined;
    const t = setTimeout(async () => {
      try { setCmd(await api(`/api/commands/${cmd.id}`)); } catch { /* keep waiting */ }
      setWaited((w) => w + 1.5);
    }, 1500);
    return () => clearTimeout(t);
  }, [cmd, waited]);

  useEffect(() => { if (cmd && cmd.status !== "pending") refreshAll(); }, [cmd?.status]);

  const live = pv?.account === "live";
  const dry = pv?.account === "dry";
  const amount = size ? Number(size.replace(",", ".")) : pv?.size_sol;
  const badSize = size && !(amount > 0 && amount <= 100);
  const submit = async () => {
    setBusy(true); setErr("");
    try {
      const r = await api("/api/buy", { method: "POST", body: { mint, size_sol: size ? amount : null, strategy: "signal" } });
      setCmd({ id: r.command_id, status: "pending" });
    } catch (e) { setErr(e.message); } finally { setBusy(false); }
  };
  const name = symbol || pv?.symbol || `${mint.slice(0, 6)}…`;
  const done = cmd && cmd.status !== "pending";

  return (
    <Modal title={`${name} kaufen`} onClose={onClose} actions={cmd ? (
      <>
        {cmd.status === "done" && cmd.position && <a className="btn" href="#/copytrade" onClick={onClose}>Zu den Positionen</a>}
        <button className="btn primary" onClick={onClose}>{done ? "Schließen" : "Im Hintergrund weiter"}</button>
      </>
    ) : (
      <>
        <button className="btn" onClick={onClose}>Abbrechen</button>
        <button className={`btn ${live ? "danger" : "primary"}`} disabled={busy || !pv || !!pv.blocked || badSize || !pv.available}
          onClick={submit}>{live ? `Mit Echtgeld kaufen (${n(amount, 2)} SOL)` : dry ? `Im Dry-Run kaufen (${n(amount, 2)} SOL)` : `Auf Papier kaufen (${n(amount, 2)} SOL)`}</button>
      </>
    )}>
      {!pv && !err && <div className="dim">Lade Preset …</div>}
      {pv && !cmd && (
        <>
          <div className="buy-grid">
            <div className="kv"><span className="k">Konto</span><span className={`v ${live ? "down" : "up"}`}>{live ? "Echtgeld" : dry ? "Dry-Run (nichts wird gesendet)" : "Papier"}</span></div>
            <div className="kv"><span className="k">Preset (Modus 2)</span><span className="v">{pv.preset}</span></div>
            <div className="kv"><span className="k">Betrag</span><span className="v">{sol(amount)}</span></div>
            <div className="kv"><span className="k">Ziel / Stop</span><span className="v">{pv.take_profit != null ? `+${pct(pv.take_profit)}` : "–"} / {pv.stop_loss != null ? `−${pct(pv.stop_loss)}` : "–"}</span></div>
            <div className="kv"><span className="k">MC jetzt</span><span className="v">{mc(pv.mcap_usd)}</span></div>
            {signal && <div className="kv"><span className="k">MC beim Signal</span><span className="v">{mc(signal.mcap_usd)}</span></div>}
            {signal && <div className="kv"><span className="k">Score</span><span className="v">{n(signal.score)} ({signal.grade})</span></div>}
            <div className="kv"><span className="k">Liquidität</span><span className="v">{usd(pv.liquidity_usd)}</span></div>
            <div className="kv"><span className="k">Rug-Risiko</span><span className="v"><RiskBadge s={pv.safety} /></span></div>
          </div>
          <label className="fld">Betrag in SOL <small>leer = {sol(pv.size_sol)} aus dem Preset</small>
            <input className="in" inputMode="decimal" value={size} onChange={(e) => setSize(e.target.value)} placeholder={pv.size_sol != null ? n(pv.size_sol, 2) : "0,5"} />
          </label>
          <div className="t2" style={{ fontSize: 12.5 }}>
            Der Bot kauft sofort zum Marktpreis. Verkauft wird wie bei Halbautomatisch: du verkaufst selbst mit dem Verkaufen-Knopf,
            der Bot zeigt dir Hinweise bei Ziel, Stop und wenn das Smart Money aussteigt. Betriebsart Modus 2 gerade: {MODE_TXT[pv.mode] || "–"}.
          </div>
          {pv.blocked && <Callout kind="warn" icon="alert">{pv.blocked}</Callout>}
          {live ? <Callout kind="bad" icon="alert">Echtgeld ist für Modus 2 eingeschaltet. Dieser Kauf nutzt echtes SOL aus deiner Wallet.</Callout>
            : dry ? <Callout kind="warn" icon="info">Dry-Run: Die Order wird gebaut, signiert und simuliert, aber nicht gesendet. Gebucht wird im Konto „Dry-Run“.</Callout>
            : <Callout icon="info">Papiermodus: echte Preise, simulierte Ausführung. Echtgeld geht nur mit dem Hauptschalter in der .env.</Callout>}
        </>
      )}
      {cmd && <BuyStatus cmd={cmd} waited={waited} live={live} />}
      {err && <Callout kind="bad" icon="alert">{err}</Callout>}
    </Modal>
  );
}

function BuyStatus({ cmd, waited, live }) {
  if (cmd.status === "pending") {
    return (
      <Callout icon="info">
        Auftrag #{cmd.id} ist beim Bot. Er wird in der nächsten Runde ausgeführt …
        {waited >= 45 && <div style={{ marginTop: 6 }}>Noch nicht abgeholt. Läuft der Copy-Trading-Dienst? (System-Seite)</div>}
      </Callout>
    );
  }
  if (cmd.status === "done" && cmd.position) {
    const p = cmd.position;
    return (
      <Callout kind="good" icon="check">
        Gekauft: Position #{p.id} {p.mode === "live" ? "mit Echtgeld" : p.mode === "dry" ? "im Dry-Run" : "auf Papier"}, {sol(p.size_sol)} bei MC {mc(p.entry_mcap_usd)}
        {p.take_profit != null && ` · Ziel +${pct(p.take_profit)}`}{p.stop_loss != null && ` · Stop −${pct(p.stop_loss)}`}.
      </Callout>
    );
  }
  if (cmd.status === "done") return <Callout kind="good" icon="check">Auftrag #{cmd.id} ausgeführt.</Callout>;
  if (cmd.status === "expired") {
    return <Callout kind="warn" icon="alert">Auftrag #{cmd.id} verfallen: Der Bot hat ihn nicht innerhalb von 2 Minuten abgeholt, damit kein veralteter Kauf ausgeführt wird. Läuft der Copy-Trading-Dienst? (System-Seite)</Callout>;
  }
  if (cmd.status === "cancelled") return <Callout icon="info">Auftrag #{cmd.id} storniert.</Callout>;
  return <Callout kind="bad" icon="alert">Kauf nicht ausgeführt: {cmd.result || cmd.status}</Callout>;
}
