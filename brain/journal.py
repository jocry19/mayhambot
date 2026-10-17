"""The daily learning diary and Telegram messages. Plain German, as a trader would write it for themselves."""
from __future__ import annotations

import html
import json
import logging
import time

from . import features, store
from .learner import honest
from .plans import BY_NAME, PLANS

log = logging.getLogger("brain.journal")

GOAL_TRADES = 100  # out-of-sample paper trades in profit before real money is even worth discussing


def send(text: str) -> bool:
    """Telegram, when TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set in .env (the token never leaves the Mac)."""
    try:
        from core.config import settings
        from signals.notifier import TelegramNotifier

        if not (settings.telegram_bot_token and settings.telegram_chat_id):
            return False
        return bool(TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id).send(html.escape(text)))
    except Exception as e:
        log.debug("telegram: %s", e)
        return False


def effects(model, min_evidence: float = 0.0) -> dict[str, tuple[float, float]]:
    """{token: (mean effect over all plans on the expected net return, evidence)}."""
    if model.d <= 1:
        return {}
    import numpy as np

    W = np.mean([model.weights(p) for p in range(model.n_plans)], axis=0)
    return {t: (float(W[i]), model.counts.get(t, 0.0)) for t, i in model.vocab.items()
            if i and model.counts.get(t, 0.0) >= min_evidence}


def _p(x) -> str:
    return "–" if x is None else f"{x:+.1%}"


GOAL_MULT = (2, 4)  # the user's goal (10/2026): the paper account x2 to x4 every day, consistently


def multipliers(conn, now: float, days: int = 14) -> list:
    """[(UTC day, equity at its end / equity at its start)] for the last days, today so far last."""
    out = []
    t_end = store.day_start(now) + 86400
    for k in range(days - 1, -1, -1):
        lo, hi = t_end - (k + 1) * 86400, t_end - k * 86400
        a = conn.execute("SELECT equity_sol FROM brain_equity WHERE ts>=? AND ts<? ORDER BY ts LIMIT 1",
                         (lo, hi)).fetchone()
        b = conn.execute("SELECT equity_sol FROM brain_equity WHERE ts>=? AND ts<? ORDER BY ts DESC LIMIT 1",
                         (lo, hi)).fetchone()
        if a and b and a[0]:
            out.append((time.strftime("%Y-%m-%d", time.gmtime(lo)), round(b[0] / a[0], 4)))
    return out


def write(conn, model, s, now: float | None = None) -> str:
    """Today's diary entry (UTC day), stored in brain_journal. Returns the text."""
    now = now or time.time()
    t0 = store.day_start(now)
    day = time.strftime("%Y-%m-%d", time.gmtime(now))
    c = conn
    opps = c.execute("SELECT kind, decision, wanted, explore, plan, features FROM brain_opps WHERE ts>=?", (t0,)).fetchall()
    trades = c.execute("SELECT pnl_sol, pnl_pct FROM brain_positions WHERE status='closed' AND closed_at>=?",
                       (t0,)).fetchall()
    n_open = c.execute("SELECT COUNT(*) FROM brain_positions WHERE status='open'").fetchone()[0]
    eq = c.execute("SELECT equity_sol FROM brain_equity ORDER BY ts DESC LIMIT 1").fetchone()
    by_kind = {k: sum(1 for o in opps if o["kind"] == k) for k in ("whale", "signal", "launch")}
    n_v2 = sum(1 for o in opps if o["kind"] == "signal" and '"sig": "v2"' in (o["features"] or ""))
    entered = [o for o in opps if o["decision"] == "enter"]
    lines = [f"🧠 Lerntagebuch {day}", ""]
    lines.append(f"Gesehen: {len(opps)} Gelegenheiten (Wale {by_kind['whale']}, Signale {by_kind['signal']}"
                 f"{f' davon v2 {n_v2}' if n_v2 else ''}, "
                 f"Launches {by_kind['launch']}). Gekauft: {len(entered)}, davon "
                 f"{sum(1 for o in entered if o['explore'])} zum Erkunden. Offen: {n_open}.")
    if trades:
        sol = sum(r["pnl_sol"] or 0 for r in trades)
        wins = sum(1 for r in trades if (r["pnl_pct"] or 0) > 0)
        lines.append(f"Heute geschlossen: {len(trades)} Trades, {wins} im Plus, {sol:+.3f} SOL.")
    if eq:
        lines.append(f"Papierkonto: {eq[0]:.3f} SOL (Start {s.start_balance_sol:.1f}).")
    mult = multipliers(conn, now)
    if mult:
        lines.append(f"Tagesfaktor (Konto Tagesende zu Tagesanfang, Ziel ×{GOAL_MULT[0]}–{GOAL_MULT[1]}): heute "
                     f"×{mult[-1][1]:.2f}" + (", die Tage davor " + ", ".join(f"×{m:.2f}" for _, m in mult[-8:-1])
                                              if len(mult) > 1 else "") + ".")

    # honest score of the decisions whose outcome became known today
    h = honest(conn, evaluated_since=now - 86400)
    lines.append("")
    if h["n"]:
        lines.append(f"Ehrlich nachgeprüft (Entscheidung vor dem Ergebnis, nach Kosten), {h['n']} Ergebnisse heute: "
                     f"genommen {h['n_taken']} mit Ø {_p(h['taken_mean'])} (erwartet {_p(h['taken_pred'])}), "
                     f"erkundet {h['n_explored']} mit Ø {_p(h['explored_mean'])}, "
                     f"ausgelassen {h['n_passed']} mit Ø {_p(h['passed_mean'])}; alles stumpf kopiert wäre "
                     f"Ø {_p(h['copy_all_mean'])} gewesen.")
        v2 = h["by_kind"].get("signal_v2")
        if v2:
            lines.append(f"Signal v2 getrennt: {v2['n']} Ergebnisse, genommen {v2['n_taken']} mit Ø "
                         f"{_p(v2['taken_mean'])}, alles kopiert Ø {_p(v2['copy_all_mean'])}.")
        if h["alpha"] is not None:
            lines.append(f"Auswahl-Vorsprung: genommen minus alles kopiert {_p(h['alpha'])} je Trade.")
        xr = h["exit_replays"]
        if xr["n"]:
            lines.append(f"Ausstieg im Vergleich ({xr['n']} nachgespielte Coins, vorher ungesehen): organisch Ø "
                         f"{_p(xr['organic'])}, feste Pläne Ø {_p(xr['plan'])}, 1 h halten Ø {_p(xr['hold_1h'])}; "
                         f"vom besten Kursanstieg mitgenommen organisch {_p(xr['capture_organic'])}, "
                         f"Pläne {_p(xr['capture_plan'])}.")
        for k, x in h["by_exit"].items():
            lines.append(f"Eigene Trades mit {'organischem Ausstieg' if k == 'organic' else 'festen Plänen'}: "
                         f"{x['n']}× Ø {_p(x['mean'])} ({x['sol']:+.3f} SOL), vom Hoch mitgenommen {_p(x['capture'])}.")
        segs = [(f"Wal erkannt nach {d}", x) for d, x in h["by_delay"].items()] + \
               [("Curve-Coins" if k == "curve" else "Pool-Coins", x) for k, x in h["by_liq"].items()]
        if segs:
            lines.append("Nach Segment (alles kopiert / genommen): " + "; ".join(
                f"{name} {x['n']}× Ø {_p(x['copy_all_mean'])} / {x['n_taken']}× Ø {_p(x['taken_mean'])}"
                for name, x in segs) + ".")
    else:
        lines.append("Heute kam noch kein Ergebnis zurück (kurze Pläne nach etwa 1 Stunde, lange nach gut 4 Stunden).")

    # what changed in the beliefs since the last entry
    eff = effects(model, min_evidence=5)
    prev = store.get(conn, "diary_effects", {}) or {}
    moves = sorted(((t, e, prev.get(t)) for t, (e, _) in eff.items() if t in prev),
                   key=lambda x: -abs(x[1] - x[2]))[:3]
    moves = [m for m in moves if abs(m[1] - m[2]) >= 0.003]
    if moves:
        lines.append("Gelernt: " + "; ".join(f"{features.describe(t)} jetzt {_p(e)} statt {_p(p)}"
                                             for t, e, p in moves) + ".")
    strong = sorted(((t, e, n) for t, (e, n) in eff.items() if n >= 20), key=lambda x: x[1])
    if strong:
        good = [x for x in reversed(strong) if x[1] > 0.005][:3]
        bad = [x for x in strong if x[1] < -0.005][:3]
        if good:
            lines.append("Spricht für einen Trade: " + ", ".join(f"{features.describe(t)} ({_p(e)})"
                                                                 for t, e, _ in good) + ".")
        if bad:
            lines.append("Spricht dagegen: " + ", ".join(f"{features.describe(t)} ({_p(e)})" for t, e, _ in bad) + ".")
    wanted = [o["plan"] for o in opps if o["wanted"]]
    if wanted:
        top = max(set(wanted), key=wanted.count)
        lines.append(f"Geändert: Bevorzugter Ausstieg heute „{BY_NAME[top].label}“ ({wanted.count(top)} von "
                     f"{len(wanted)} gewollten Käufen).")
    # where it still does not know
    seen_today = {}
    for (fj, kind) in c.execute("SELECT features, kind FROM brain_opps WHERE ts>=?", (t0,)).fetchall():
        try:
            for tok in features.tokens(kind, json.loads(fj or "{}")):
                if ":" not in tok:
                    seen_today[tok] = seen_today.get(tok, 0) + 1
        except ValueError:
            pass
    thin = sorted((t for t in seen_today if model.counts.get(t, 0.0) < 10 and not t.startswith("kind=")),
                  key=lambda t: -seen_today[t])[:3]
    if thin:
        lines.append("Noch unsicher (zu wenig Ergebnisse): " + ", ".join(features.describe(t) for t in thin) + ".")
    # the way to real money
    tot = honest(conn)
    lines.append("")
    lines.append(f"Weg zu Echtgeld: {tot['real_n']} eigene Papier-Trades insgesamt, Ø {_p(tot['real_mean'])}, "
                 f"{tot['real_sol']:+.3f} SOL; nachgeprüft genommen {tot['n_taken']} mit Ø {_p(tot['taken_mean'])}. "
                 f"Ziel: {GOAL_TRADES} nachgeprüfte Trades im Plus. Echtgeld bleibt aus.")
    text = "\n".join(lines)
    store.put(conn, "diary_effects", {t: e for t, (e, _) in eff.items()})
    conn.execute("INSERT OR REPLACE INTO brain_journal(day, ts, text, data, sent) VALUES (?,?,?,?,"
                 "COALESCE((SELECT sent FROM brain_journal WHERE day=?),0))",
                 (day, int(now), text, json.dumps({"honest_today": h, "honest_all": tot, "multipliers": mult}), day))
    return text


def due(conn, s, now: float | None = None) -> bool:
    now = now or time.time()
    day = time.strftime("%Y-%m-%d", time.gmtime(now))
    if time.gmtime(now).tm_hour < s.diary_hour_utc:
        return False
    r = conn.execute("SELECT sent FROM brain_journal WHERE day=?", (day,)).fetchone()
    return not (r and r[0])


def daily(conn, model, s, now: float | None = None) -> str | None:
    """Write today's entry once after BRAIN_DIARY_HOUR_UTC and send it unless Telegram is off."""
    if not due(conn, s, now):
        return None
    text = write(conn, model, s, now)
    if s.telegram != "off":
        send(text)
    day = time.strftime("%Y-%m-%d", time.gmtime(now or time.time()))
    conn.execute("UPDATE brain_journal SET sent=1 WHERE day=?", (day,))
    return text


def plans_text() -> str:
    return "\n".join(f"{p.label}: {p.text()}" for p in PLANS)
