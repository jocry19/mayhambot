import { useState } from "react";
import { createPortal } from "react-dom";
import { EChart, baseOption, timeAxis, valueAxis } from "./chart.jsx";
import { act, ago, dt, n, pct } from "./lib.js";
import { Callout, Card, Empty, Icon, Modal, Table, Tile } from "./ui.jsx";

// level -> German label and badge colour (status colours, always with the text, never colour alone)
export const RISK = {
  low: { l: "niedrig", cls: "good" },
  caution: { l: "erhöht", cls: "warn" },
  high: { l: "hoch", cls: "serious" },
  critical: { l: "kritisch", cls: "bad" },
  unchecked: { l: "ungeprüft", cls: "" },
};
const STATUS = { ok: "geprüft", unknown: "keine Daten", skipped: "übersprungen", error: "Fehler" };

/** Small risk badge: level + score; "gesperrt" when the copy trader skips the coin. `s` = {level, score, blocked, override, reasons}. */
export function RiskBadge({ s, now }) {
  if (!s || !s.level) return <span className="dim" title="Rug-Schutz hat diesen Coin noch nicht geprüft">–</span>;
  const blocked = now ? !!now.blocked : !!s.blocked;
  const ov = (now || s).override;
  const m = RISK[s.level] || RISK.unchecked;
  const why = (s.reasons || []).filter(Boolean);
  const title = [
    `Rug-Risiko ${s.score != null ? n(s.score) : "–"} von 100 (${m.l})`,
    ...why.map((w) => `• ${w}`),
    blocked ? "Copy-Trading kauft diesen Coin nicht." : "",
    ov === "block" ? "Manuell gesperrt." : ov === "allow" ? "Manuell freigegeben." : "",
    now && now.level && now.level !== s.level ? `Jetzt: ${(RISK[now.level] || RISK.unchecked).l} (${n(now.score)})` : "",
  ].filter(Boolean).join("\n");
  return (
    <span className={`badge risk ${blocked ? "bad" : m.cls}`} title={title}>
      {blocked ? <><Icon name="shield" />gesperrt</> : <>{m.l}{s.score != null ? ` ${n(s.score)}` : ""}</>}
      {ov === "allow" && !blocked ? " ✓" : ""}
    </span>
  );
}

/** Block / allow / back-to-automatic, each behind a confirmation. Writes only through safety.block / allow / clear_override. */
export function SafetyButtons({ mint, symbol, s, sm = false }) {
  const [ask, setAsk] = useState(null);
  const ov = s?.override?.action || s?.override;
  const btn = `btn ${sm ? "sm" : ""}`;
  return (
    <span className="row" style={{ gap: 6 }} onClick={(e) => e.stopPropagation()}>
      {ov !== "block" && !s?.blocked && <button className={`${btn} danger`} onClick={() => setAsk("block")}>Sperren</button>}
      {ov !== "allow" && s?.blocked && <button className={btn} onClick={() => setAsk("allow")}>Trotzdem freigeben</button>}
      {ov && <button className={btn} onClick={() => setAsk("clear")}>Automatisch entscheiden</button>}
      {ask && createPortal(<ConfirmOverride mint={mint} symbol={symbol} action={ask} s={s} onClose={() => setAsk(null)} />, document.body)}
    </span>
  );
}

const ASK = {
  block: { t: "sperren", txt: "Der Bot kauft diesen Coin dann in keinem Modus mehr, auch nicht über den Kaufen-Knopf. Signale bekommen 0 Punkte.", ok: "Sperren", cls: "danger" },
  allow: { t: "freigeben", txt: "Die automatische Sperre wird übergangen: der Bot darf diesen Coin wieder kaufen, obwohl der Rug-Schutz ihn als gefährlich einstuft.", ok: "Freigeben", cls: "danger" },
  clear: { t: "wieder automatisch prüfen", txt: "Deine manuelle Entscheidung wird gelöscht; ab jetzt gilt wieder das Ergebnis der automatischen Prüfung.", ok: "Zurücksetzen", cls: "primary" },
};

function ConfirmOverride({ mint, symbol, action, s, onClose }) {
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const a = ASK[action];
  const go = async () => {
    setBusy(true);
    try { await act(`/api/safety/${mint}`, { action, reason }); onClose(); } catch { setBusy(false); }
  };
  return (
    <span onClick={(e) => e.stopPropagation()}>
      <Modal title={`${symbol || `${mint.slice(0, 6)}…`} ${a.t}?`} onClose={onClose} actions={<>
        <button className="btn" onClick={onClose}>Abbrechen</button>
        <button className={`btn ${a.cls}`} disabled={busy} onClick={go}>{a.ok}</button>
      </>}>
        <div className="t2" style={{ fontSize: 13 }}>{a.txt}</div>
        {action === "allow" && s?.reasons?.length > 0 && (
          <Callout kind="bad" icon="alert">
            {(s.reasons || []).slice(0, 3).map((r) => (typeof r === "string" ? r : r.text)).join(" · ")}
          </Callout>
        )}
        {action !== "clear" && (
          <label className="fld">Grund <small>optional, steht in der Liste unter Steuerung</small>
            <input className="in" value={reason} maxLength={200} onChange={(e) => setReason(e.target.value)}
              placeholder={action === "block" ? "z. B. Dev bekannt von früheren Rugs" : "z. B. selbst geprüft"} />
          </label>
        )}
      </Modal>
    </span>
  );
}

function RiskHistory({ hist }) {
  if (!hist || hist.length < 2) return <Empty>Noch keine Verlaufsdaten</Empty>;
  return (
    <EChart height={180} deps={[hist]} ariaLabel="Rug-Risiko über Zeit"
      build={(c) => baseOption(c, {
        tooltip: { ...baseOption(c).tooltip, valueFormatter: (v) => `${n(v)} von 100` },
        xAxis: timeAxis(c), yAxis: valueAxis(c, (v) => n(v), { min: 0, max: 100, interval: 25 }),
        series: [{
          name: "Rug-Risiko", type: "line", step: "end", showSymbol: false, lineStyle: { width: 2, color: c.text2 },
          itemStyle: { color: c.text2 },
          data: hist.map((h) => [h.ts * 1000, h.score]),
          markArea: {
            silent: true,
            data: [
              [{ yAxis: 25, itemStyle: { color: c.warn, opacity: 0.07 } }, { yAxis: 50 }],
              [{ yAxis: 50, itemStyle: { color: c.warn, opacity: 0.13 } }, { yAxis: 75 }],
              [{ yAxis: 75, itemStyle: { color: c.bad, opacity: 0.14 } }, { yAxis: 100 }],
            ],
          },
          markLine: {
            silent: true, symbol: "none", lineStyle: { color: c.bad, type: "dashed", width: 1 },
            label: { color: c.muted, formatter: "Sperre ab 75", position: "insideEndTop" }, data: [{ yAxis: 75 }],
          },
        }],
      })} />
  );
}

/** Full rug-protection view for the coin page. */
export function SafetyCard({ mint, symbol, s }) {
  const head = s && <SafetyButtons mint={mint} symbol={symbol} s={s} />;
  if (!s) return null;
  if (!s.checked) {
    return (
      <Card title="Rug- und Insider-Schutz" actions={head}>
        {s.override?.action === "block" ? <Callout kind="bad" icon="alert">Manuell gesperrt{s.override.reason ? `: ${s.override.reason}` : ""}.</Callout>
          : <Empty>{s.available ? "Dieser Coin wurde noch nicht geprüft. Der Rug-Schutz prüft Coins mit Signal oder Position automatisch." : "Der Rug-Schutz (safety) läuft noch nicht auf diesem Rechner."}</Empty>}
      </Card>
    );
  }
  const m = RISK[s.level] || RISK.unchecked;
  const ov = s.override;
  const okChecks = s.checks.filter((c) => c.status === "ok").length;
  return (
    <Card title="Rug- und Insider-Schutz" sub={`Stand ${dt(s.ts)} (${ago(s.ts)}) · ${okChecks} von ${s.checks.length} Prüfungen mit Daten`} actions={head}>
      <div className="col" style={{ gap: 14 }}>
        <div className="tiles">
          <Tile label="Rug-Risiko" value={<span className={`risk-num ${m.cls}`}>{n(s.score)}<small> / 100</small></span>} sub={`Stufe ${m.l}`} />
          <Tile label="Copy-Trading" value={s.blocked ? "gesperrt" : "erlaubt"} cls={s.blocked ? "down" : "up"}
            sub={ov ? (ov.action === "block" ? "manuell gesperrt" : "manuell freigegeben") : s.blocked ? "automatisch ab Risiko 75 oder hartem Befund" : "unter der Sperrgrenze"} />
          <Tile label="Abdeckung" value={pct(s.coverage)} sub="Anteil der Prüfungen mit Antwort" />
        </div>
        {ov && <Callout kind={ov.action === "block" ? "bad" : "warn"} icon="alert">
          {ov.action === "block" ? "Von dir gesperrt" : "Von dir freigegeben, obwohl die Prüfung warnt"} am {dt(ov.ts)}{ov.reason ? `: ${ov.reason}` : ""}.
        </Callout>}
        {s.hard_flags?.length > 0 && <Callout kind="bad" icon="alert">Harter Befund (sperrt immer): {s.hard_flags.join(" · ")}</Callout>}
        <div className="grid g2">
          <div className="col" style={{ gap: 8 }}>
            <div className="b">Warum dieses Risiko</div>
            {s.reasons.length ? (
              <ul className="risk-list">
                {s.reasons.map((r, i) => (
                  <li key={i}><span className={`badge ${r.risk >= 75 ? "bad" : r.risk >= 50 ? "serious" : r.risk >= 25 ? "warn" : ""}`}>{n(r.risk)}</span><span>{r.text}</span></li>
                ))}
              </ul>
            ) : <div className="dim">Keine Warnungen.</div>}
            {s.positives?.length > 0 && (
              <>
                <div className="b" style={{ marginTop: 6 }}>Was dafür spricht</div>
                <ul className="risk-list pos">{s.positives.map((p, i) => <li key={i}><Icon name="check" /><span>{p}</span></li>)}</ul>
              </>
            )}
          </div>
          <div className="col" style={{ gap: 8 }}>
            <div className="b">Risiko über Zeit</div>
            <RiskHistory hist={s.history} />
          </div>
        </div>
        <Table rows={s.checks} rowKey={(r) => r.check} initialSort={{ k: "risk", d: "desc" }} cols={[
          { k: "label", l: "Prüfung" },
          { k: "status", l: "Status", fmt: (r) => <span className={r.status === "ok" ? "" : "dim"}>{STATUS[r.status] || r.status}</span> },
          { k: "risk", l: "Risiko", r: true, fmt: (r) => (r.status === "ok" ? <span className={r.risk >= 50 ? "down" : ""}>{n(r.risk)}{r.hard ? " · hart" : ""}</span> : "–") },
        ]} />
        <div className="dim" style={{ fontSize: 12 }}>
          Das Gesamtrisiko kombiniert die Prüfungen als unabhängige Gefahren: zweimal 50 ergibt 75, nicht 100. Ab 75 oder bei einem harten Befund
          (Mint/Freeze-Rechte aktiv, kein Verkauf möglich, hohe Steuer) kauft der Bot nicht.
        </div>
      </div>
    </Card>
  );
}

/** Risk stored with a copy-trading position or entry when the bot bought (ct_positions.safety_*, ct_alerts.data.safety). */
export const PosRisk = ({ r }) => (r.safety_level
  ? <RiskBadge s={{ level: r.safety_level, score: r.safety_score, reasons: r.safety_reasons, blocked: false }} />
  : <span className="dim">–</span>);

/** How well the rug protection really works (safety learn run): walk-forward launch backtest and rug rate per level. */
export function SafetyQuality({ q }) {
  if (!q) return null;
  const bt = q.backtest;
  const best = bt?.scores?.find((x) => x.key === bt.best);
  return (
    <Card title="Rug-Schutz-Trefferquote"
      sub={`Letzter Lernlauf ${ago(q.learned_ts)} (${dt(q.learned_ts)}) · ${n(q.n_labelled)} Coins mit bekanntem Ausgang · Rug = −${n(q.rug_drop_pct)} % in ${n(q.horizon_h)} h`}>
      <div className="col" style={{ gap: 14 }}>
        {bt && best && (
          <div className="tiles wrap-sub">
            <Tile label="Rugs erkannt" value={pct(best.recall)} sub={`${n(best.rugs_caught)} von ${n(best.rugs_total)} bei höchstens 10 % Fehlalarm`} />
            <Tile label="Treffsicherheit" value={pct(best.precision)} sub="der markierten Launches waren wirklich Rugs" />
            <Tile label="Verdoppler fälschlich markiert" value={`${n(best.doublers_flagged)} von ${n(best.doublers_total)}`} sub="Launches mit +100 %, die gesperrt worden wären" />
            <Tile label="Trennschärfe (AUC)" value={n(best.auc, 2)} sub={`0,5 = Zufall, 1 = perfekt · Bewertung „${best.label}“`} />
          </div>
        )}
        {bt?.scores?.length > 0 && (
          <Table rows={bt.scores} rowKey={(r) => r.key} cols={[
            { k: "label", l: "Bewertung", fmt: (r) => <span>{r.label}{r.key === bt.best ? <span className="badge good" style={{ marginLeft: 6 }}>beste</span> : null}</span> },
            { k: "auc", l: "AUC", r: true, fmt: (r) => n(r.auc, 2) },
            { k: "recall", l: "Rugs erkannt", r: true, fmt: (r) => (r.recall != null ? `${pct(r.recall)} (${n(r.rugs_caught)}/${n(r.rugs_total)})` : "–") },
            { k: "precision", l: "davon echt", r: true, fmt: (r) => pct(r.precision) },
            { k: "doublers_flagged", l: "Verdoppler markiert", r: true, fmt: (r) => (r.doublers_total != null ? `${n(r.doublers_flagged)}/${n(r.doublers_total)}` : "–") },
          ]} />
        )}
        {bt && <div className="help">Rückrechnung auf {n(bt.n)} Sniper-Launches: an der älteren Hälfte gelernt, an den {n(bt.test_n)} neueren geprüft.
          Die Schwelle ist so gewählt, dass höchstens jeder zehnte spätere Verdoppler gesperrt worden wäre.</div>}
        {q.levels?.length > 0 && (
          <Table rows={q.levels} rowKey={(r) => r.level} cols={[
            { k: "level", l: "Stufe bei der Prüfung", sort: (r) => ["low", "caution", "high", "critical", "unchecked"].indexOf(r.level),
              fmt: (r) => <span className={`badge risk ${(RISK[r.level] || RISK.unchecked).cls}`}>{(RISK[r.level] || RISK.unchecked).l}</span> },
            { k: "n", l: "Coins", r: true, fmt: (r) => n(r.n) },
            { k: "rugged", l: "später Rug", r: true, fmt: (r) => n(r.rugged) },
            { k: "rug_rate", l: "Rug-Quote", r: true, fmt: (r) => (r.rug_rate != null
              ? <span className={r.rug_rate > (q.base_rug_rate || 0) * 1.5 ? "down" : r.rug_rate < (q.base_rug_rate || 0) * 0.7 ? "up" : ""}>{pct(r.rug_rate)}</span> : "–") },
          ]} />
        )}
        {q.base_rug_rate != null && <div className="help">Im Schnitt endeten {pct(q.base_rug_rate)} der geprüften Coins als Rug. Je höher die Stufe, desto höher sollte die Quote sein.</div>}
      </div>
    </Card>
  );
}
