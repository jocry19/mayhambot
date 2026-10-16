import { useEffect, useState } from "react";
import { act, dt, n, usePoll } from "../lib.js";
import { Callout, Card, Icon, Modal, Switch, Table } from "../ui.jsx";
import { CoinLink } from "../parts.jsx";
import { RISK, RiskBadge, SafetyButtons } from "../safety.jsx";

function SafetyList() {
  const { data: d } = usePoll("/api/safety", 15000);
  if (!d) return null;
  const c = d.counts || {};
  const total = Object.values(c).reduce((a, b) => a + b, 0);
  return (
    <Card title="Rug-Schutz: gesperrte Coins" flush
      sub={d.available ? `${n(total)} Coins geprüft · ${["low", "caution", "high", "critical"].map((k) => `${n(c[k] || 0)} ${RISK[k].l}`).join(" · ")}. Gesperrte Coins kauft der Bot in keinem Modus.`
        : "Der Rug-Schutz (safety) hat noch keine Coins geprüft."}>
      <Table rows={d.items} rowKey={(r) => r.mint} empty="Kein Coin gesperrt oder manuell entschieden" cols={[
        { k: "symbol", l: "Coin", fmt: (r) => <CoinLink mint={r.mint} symbol={r.symbol || `${r.mint.slice(0, 6)}…`} /> },
        { k: "score", l: "Risiko", fmt: (r) => <RiskBadge s={r} /> },
        { k: "override", l: "Entscheidung", fmt: (r) => (r.override === "block" ? "von dir gesperrt" : r.override === "allow" ? "von dir freigegeben" : "automatisch") },
        { k: "reasons", l: "Grund", sort: false, fmt: (r) => <span className="t2" style={{ whiteSpace: "normal" }}>{r.override_reason || (r.reasons || [])[0] || "–"}</span> },
        { k: "act", l: "", sort: false, fmt: (r) => <SafetyButtons mint={r.mint} symbol={r.symbol} s={r} sm /> },
      ]} />
    </Card>
  );
}

const MODES = [
  ["off", "Aus", "Dieser Modus handelt nicht."],
  ["manual", "Manuell", "Nur Kaufsignale. Du entscheidest und kaufst selbst (oder per Knopf)."],
  ["semi", "Halbautomatisch", "Der Bot kauft, du verkaufst. Er zeigt dir Verkaufs-Hinweise."],
  ["auto", "Automatisch", "Der Bot kauft und verkauft selbst nach deinem Preset."],
];
const STRATS = [
  ["whale", "Modus 1 · Wal-Pumps kopieren", "Kauft direkt nach den Walen, deren Käufe die besten Copy-Trading-Pumps auslösen, und verkauft in den ersten Pump."],
  ["signal", "Modus 2 · Smart-Money-Signale kaufen", "Kauft die besten Smart-Money-Signale mit deinem Preset. Ist das Modell trainiert, entscheidet dessen Score und die Note A–E skaliert die Größe."],
  ["launch", "Modus 3 · Launch-Sniper-Käufe", "Kauft die Launches, die der Sniper nach Bundle- und Insider-Prüfung einsteigen würde, mit dessen Ausstiegen (Stop, Trailing ab TP1, Haltezeit). Stop nie weiter als dein Preset."],
];
const FIELDS = [
  ["size_sol", "SOL pro Trade", "", 1],
  ["max_open", "Max. offene Positionen", "", 1],
  ["max_daily_loss_sol", "Tagesverlust-Limit (SOL)", "danach keine neuen Käufe", 1],
  ["take_profit", "Gewinnziel", "Anteil, 0,5 = +50 %", 100],
  ["stop_loss", "Stop-Loss", "Anteil, 0,2 = −20 %", 100],
  ["trail_activate", "Trailing ab", "Gewinn, ab dem der Trailing-Stop greift", 100],
  ["trail_drawdown", "Trailing-Abstand", "Rückgang vom Hoch", 100],
  ["max_hold_min", "Max. Haltedauer (min)", "", 1],
  ["min_score", "Mindest-Score", "Modus 2: Signal-/Modell-Score 0–100", 1],
  ["min_smart_buyers", "Mindest-Smart-Wallets", "Modus 2", 1],
  ["min_liquidity_usd", "Mindest-Liquidität ($)", "", 1],
  ["max_mcap_usd", "Max. Market Cap ($)", "", 1],
  ["max_signal_age_s", "Max. Signal-Alter (s)", "Modus 2: ältere Signale nicht kaufen", 1],
  ["slippage_bps", "Slippage (bps)", "300 = 3 %", 1],
];

function fmtVal(k, v, mul) {
  if (v == null) return "–";
  if (mul === 100) return `${n(v * 100, 0)} %`;
  if (k.endsWith("_usd")) return `$${n(v, 0)}`;
  return n(v, k === "size_sol" || k === "max_daily_loss_sol" ? 2 : 0);
}

function LiveModal({ strat, onClose }) {
  const [txt, setTxt] = useState("");
  return (
    <Modal title={`Echtgeld für ${strat[1]} einschalten?`} onClose={onClose} actions={<>
      <button className="btn" onClick={onClose}>Abbrechen</button>
      <button className="btn danger" disabled={txt !== "ECHTGELD"} onClick={() => act("/api/control/live", { strategy: strat[0], on: true, confirm: "ECHTGELD" }).finally(onClose)}>Echtgeld einschalten</button>
    </>}>
      <Callout kind="bad" icon="alert">Ab jetzt handelt dieser Modus mit echtem SOL aus deinem Wallet, mit den Grenzen deines Presets. Verluste sind möglich.</Callout>
      <label className="fld">Tippe ECHTGELD zur Bestätigung<input className="in" value={txt} onChange={(e) => setTxt(e.target.value)} autoFocus /></label>
    </Modal>
  );
}

function PresetEditor({ ctl }) {
  const names = Object.keys(ctl.presets || {});
  const [base, setBase] = useState(names.includes("standard") ? "standard" : names[0]);
  const [vals, setVals] = useState({});
  const [name, setName] = useState("mein-preset");
  const [useFor, setUseFor] = useState("");
  useEffect(() => {
    const p = ctl.presets?.[base];
    if (p) {
      const v = {};
      for (const [k, , , mul] of FIELDS) v[k] = p[k] == null ? "" : String(mul === 100 ? Math.round(p[k] * 1000) / 10 : p[k]).replace(".", ",");
      v.learned_tp = !!p.learned_tp;
      setVals(v);
      if (!p.builtin) setName(base);
    }
  }, [base, ctl.presets]);
  const save = () => {
    const preset = { learned_tp: vals.learned_tp };
    for (const [k, , , mul] of FIELDS) {
      const x = parseFloat(String(vals[k]).replace(",", "."));
      if (!Number.isNaN(x)) preset[k] = mul === 100 ? x / 100 : x;
    }
    act("/api/control/preset/save", { name: name.trim(), preset, use_for: useFor || null });
  };
  return (
    <Card title="Preset bearbeiten" sub="Echtgeld-Presets: Größe, Limits, Ziel, Stop. Eingebaute Presets bleiben unverändert, du speicherst unter neuem Namen.">
      <div className="row" style={{ gap: 12, marginBottom: 12 }}>
        <label className="fld">Ausgehend von
          <select className="in" value={base} onChange={(e) => setBase(e.target.value)}>{names.map((x) => <option key={x}>{x}</option>)}</select>
        </label>
        <label className="fld">Speichern als<input className="in" value={name} onChange={(e) => setName(e.target.value)} /></label>
        <label className="fld">Gleich verwenden für
          <select className="in" value={useFor} onChange={(e) => setUseFor(e.target.value)}>
            <option value="">nicht zuweisen</option><option value="whale">Modus 1</option><option value="signal">Modus 2</option><option value="launch">Modus 3</option>
          </select>
        </label>
        <span className="sp" />
        <button className="btn primary" onClick={save}>Preset speichern</button>
      </div>
      <div className="grid g4" style={{ gap: 12 }}>
        {FIELDS.map(([k, l, h, mul]) => (
          <label className="fld" key={k}>{l}{mul === 100 ? " (%)" : ""}
            <input className="in" inputMode="decimal" value={vals[k] ?? ""} onChange={(e) => setVals((v) => ({ ...v, [k]: e.target.value }))} />
            {h && <small>{h}</small>}
          </label>
        ))}
        <label className="fld">Gelerntes Ziel je Wal (Modus 1)
          <div className="row"><Switch on={!!vals.learned_tp} onChange={(x) => setVals((v) => ({ ...v, learned_tp: x }))} label="Gelerntes Ziel" /><small>statt festem Gewinnziel</small></div>
        </label>
      </div>
    </Card>
  );
}

export default function Control() {
  const { data: ctl } = usePoll("/api/control", 8000);
  const { data: ct } = usePoll("/api/copytrade?mode=paper", 15000);
  const [liveAsk, setLiveAsk] = useState(null);
  const [mint, setMint] = useState("");
  const [size, setSize] = useState("");
  if (!ctl) return <div className="empty">Lade …</div>;
  if (!ctl.available) return <Callout kind="warn" icon="alert">Steuerung nicht verfügbar: {ctl.error}</Callout>;
  const cmds = ct?.commands || [];
  return (
    <div className="col" style={{ gap: 16 }}>
      <div className="grid g2">
        <Card title="Notbremse" sub="Stoppt alle neuen Käufe sofort. Offene Positionen werden weiter verwaltet.">
          <div className="row" style={{ gap: 14 }}>
            <Switch on={ctl.paused} onChange={(on) => act("/api/control/pause", { on })} red label="Käufe pausieren" />
            <span><span className="b">Käufe pausieren</span> <span className="dim">{ctl.paused ? "· an: keine neuen Käufe" : "· aus: Bot darf kaufen"}</span></span>
          </div>
        </Card>
        <Card title="Echtgeld-Hauptschalter" sub="Steht in der .env auf deinem Rechner und wird nur von Hand geändert">
          {ctl.live_ready ? <Callout kind="bad" icon="alert">Hauptschalter AN: LIVE_TRADING=true und ein Schlüssel sind gesetzt. Echtgeld wirkt für jeden Modus, den du unten auf Echtgeld stellst.</Callout>
            : <Callout icon="info">Hauptschalter aus: alles läuft im Papiermodus mit echten Marktdaten. Für Echtgeld in der .env LIVE_TRADING=true und SOLANA_KEYPAIR_PATH setzen, App neu starten, dann hier je Modus einschalten.</Callout>}
        </Card>
      </div>

      {STRATS.map((s) => {
        const [key, title, desc] = s;
        const mode = ctl.modes[key];
        const live = ctl.live[key];
        const p = ctl.presets[ctl.active_presets[key]] || {};
        return (
          <Card key={key} title={title} sub={desc}
            actions={<span className={`badge ${live && ctl.live_ready ? "live" : "good"}`}>{live && ctl.live_ready ? "ECHTGELD" : "Papier"}</span>}>
            <div className="grid g4" style={{ gap: 10 }}>
              {MODES.map(([m, l, h]) => (
                <div key={m} className={`mode-card ${mode === m ? "on" : ""}`} role="button" tabIndex={0}
                  onClick={() => mode !== m && act("/api/control/mode", { strategy: key, mode: m })}
                  onKeyDown={(e) => e.key === "Enter" && act("/api/control/mode", { strategy: key, mode: m })}>
                  <b>{mode === m ? "● " : ""}{l}</b><span>{h}</span>
                </div>
              ))}
            </div>
            <div className="row" style={{ gap: 18, marginTop: 14 }}>
              <label className="fld">Preset
                <select className="in" value={ctl.active_presets[key]} onChange={(e) => act("/api/control/preset", { strategy: key, name: e.target.value })}>
                  {Object.keys(ctl.presets).map((x) => <option key={x}>{x}</option>)}
                </select>
              </label>
              <div className="row" style={{ gap: 14, color: "var(--text-2)", fontSize: 12.5 }}>
                <span>{fmtVal("size_sol", p.size_sol)} SOL/Trade</span>
                <span>max. {p.max_open} offen</span>
                <span>Tageslimit {fmtVal("max_daily_loss_sol", p.max_daily_loss_sol)} SOL</span>
                <span>Ziel {key === "whale" && p.learned_tp ? "gelernt je Wal" : fmtVal("take_profit", p.take_profit, 100)}</span>
                <span>Stop {fmtVal("stop_loss", p.stop_loss, 100)}</span>
                {key === "signal" && <span>ab Score {p.min_score}</span>}
              </div>
              <span className="sp" />
              <label className="row nowrap" style={{ gap: 8 }} title={ctl.live_ready ? "" : "Erst den Hauptschalter in der .env einschalten"}>
                <span className="t2">Echtgeld</span>
                <Switch on={live} red disabled={!ctl.live_ready && !live} label="Echtgeld"
                  onChange={(on) => (on ? setLiveAsk(s) : act("/api/control/live", { strategy: key, on: false }))} />
              </label>
            </div>
          </Card>
        );
      })}
      {liveAsk && <LiveModal strat={liveAsk} onClose={() => setLiveAsk(null)} />}

      <PresetEditor ctl={ctl} />

      <div className="grid g2">
        <Card title="Selbst kaufen" sub="Auftrag an den laufenden Bot (Modus 2, du verkaufst). Mint-Adresse vom Coin.">
          <div className="row" style={{ gap: 10, alignItems: "flex-end" }}>
            <label className="fld" style={{ flex: 1, minWidth: 220 }}>Mint<input className="in mono" value={mint} onChange={(e) => setMint(e.target.value.trim())} placeholder="…pump" /></label>
            <label className="fld" style={{ width: 120 }}>SOL<input className="in" value={size} onChange={(e) => setSize(e.target.value)} placeholder="Preset" /></label>
            <button className="btn primary" disabled={mint.length < 32} onClick={() => act("/api/buy", { mint, size_sol: size ? Number(size.replace(",", ".")) : null, strategy: "signal" }).then(() => setMint(""))}>Kaufen</button>
          </div>
        </Card>
        <Card title="Aufträge" sub="Kauf- und Verkaufsaufträge an den Bot" flush>
          <Table rows={cmds} rowKey={(r) => r.id} empty="Keine Aufträge" cols={[
            { k: "ts", l: "Zeit", fmt: (r) => dt(r.ts) },
            { k: "action", l: "Art", fmt: (r) => (r.action === "buy" ? "Kauf" : "Verkauf") },
            { k: "target", l: "Ziel", sort: false, fmt: (r) => (r.position_id ? `Position #${r.position_id}` : <span className="mono">{(r.mint || "").slice(0, 6)}…</span>) },
            { k: "status", l: "Status", fmt: (r) => <span className={`badge ${r.status === "done" ? "good" : r.status === "failed" ? "bad" : r.status === "pending" ? "warn" : ""}`}>{{ done: "erledigt", failed: "fehlgeschlagen", pending: "wartet", cancelled: "storniert" }[r.status] || r.status}</span> },
            { k: "result", l: "", fmt: (r) => (r.status === "pending" ? <button className="btn sm" onClick={() => act(`/api/commands/${r.id}/cancel`)}>Stornieren</button> : <span className="t2">{r.result}</span>) },
          ]} />
        </Card>
      </div>
      <SafetyList />
      <div className="dim row" style={{ fontSize: 12 }}><Icon name="terminal" size={14} />Dasselbe im Terminal: <span className="mono">python -m cli</span> oder <span className="mono">python -m copytrade mode signal semi</span>, Coins sperren mit <span className="mono">python -m cli safety block MINT</span></div>
    </div>
  );
}
