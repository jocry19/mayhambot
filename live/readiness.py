"""Go-live check: measurable criteria that must hold before real sends (guard.py enforces it while
LIVE_REQUIRE_GATE is on), plus the criteria for moving up a stage. Read-only, from the shared database."""
from __future__ import annotations

import os
import statistics
import time

from . import store
from .config import ADVANCE, STAGES, LiveConfig
from .guard import HEARTBEAT_MAX_AGE_S, live_strategies


def paper_edge(conn, strategy: str, days: float) -> dict:
    now = int(time.time())
    t0 = now - int(days * 86400)
    rows = conn.execute("SELECT closed_at, pnl_sol FROM ct_positions WHERE mode='paper' AND strategy=? "
                        "AND status='closed' AND closed_at>=? AND pnl_sol IS NOT NULL", (strategy, t0)).fetchall()
    pnl = [float(r["pnl_sol"]) for r in rows]
    mid = t0 + (now - t0) / 2
    first = sum(float(r["pnl_sol"]) for r in rows if r["closed_at"] < mid)
    second = sum(float(r["pnl_sol"]) for r in rows if r["closed_at"] >= mid)
    wins, losses = sum(p for p in pnl if p > 0), -sum(p for p in pnl if p < 0)
    pf = wins / losses if losses else (float("inf") if wins else 0.0)
    return {"n": len(pnl), "net_sol": sum(pnl), "profit_factor": pf, "first_half": first, "second_half": second,
            "win_rate": sum(p > 0 for p in pnl) / len(pnl) if pnl else 0.0}


def learned_edge(conn, strategy: str) -> dict | None:
    """The strategy's active learned parameter version (copytrade/learn.py): out-of-sample mean gain per trade after
    calibrated costs, number of test entries, baseline. None when the learner has no active version."""
    try:
        r = conn.execute("SELECT id, n_train, n_test, train_gain, test_gain, baseline_test_gain, ts FROM "
                         "ct_param_versions WHERE strategy=? AND scope='mode' AND active=1 ORDER BY id DESC LIMIT 1",
                         (strategy,)).fetchone()
    except Exception:
        return None
    return dict(r) if r else None


def dry_stats(conn, days: float = 7) -> dict:
    t0 = int(time.time() - days * 86400)
    cnt = {(r["side"], r["status"]): r["n"] for r in conn.execute(
        "SELECT side, status, COUNT(*) n FROM live_orders WHERE dry=1 AND ts>=? GROUP BY side, status", (t0,))}
    buys_ok = cnt.get(("buy", "dry_ok"), 0)
    buys_bad = sum(v for (s, st), v in cnt.items() if s == "buy" and st in ("dry_sim_failed",))
    booked = conn.execute("SELECT COUNT(*) FROM ct_positions WHERE mode='dry' AND status='closed' AND opened_at>=?",
                          (t0,)).fetchone()[0]
    return {"buys_ok": buys_ok, "buys_sim_failed": buys_bad, "sells_built": cnt.get(("sell", "dry_built"), 0),
            "rejected": sum(v for (s, st), v in cnt.items() if st == "rejected"), "round_trips_booked": booked}


def live_stats(conn, since: int = 0) -> dict:
    rows = conn.execute("SELECT pnl_sol FROM ct_positions WHERE mode='live' AND status='closed' AND closed_at>=?",
                        (since,)).fetchall()
    slips = [r[0] for r in conn.execute("SELECT realized_slippage FROM live_orders WHERE dry=0 AND side='buy' "
                                        "AND status='landed' AND realized_slippage IS NOT NULL AND ts>=?", (since,))]
    fails = conn.execute("SELECT COUNT(*) FROM live_orders WHERE dry=0 AND ts>=? AND status IN "
                         "('failed','expired','sim_failed')", (since,)).fetchone()[0]
    landed = conn.execute("SELECT COUNT(*) FROM live_orders WHERE dry=0 AND ts>=? AND status='landed'",
                          (since,)).fetchone()[0]
    return {"round_trips": len(rows), "net_sol": sum(float(r[0] or 0) for r in rows),
            "median_buy_slippage": statistics.median(slips) if slips else None, "landed": landed, "failed": fails,
            "land_rate": landed / (landed + fails) if landed + fails else None}


def _check(out, key, label, ok, detail, required=True):
    out.append({"key": key, "label": label, "ok": bool(ok), "required": required, "detail": detail})


def report(conn, cfg: LiveConfig) -> dict:
    store.init(conn)
    checks: list[dict] = []
    pub = store.get(conn, "wallet_pubkey")
    has_key = store.get(conn, "wallet_has_key") == "1"
    _check(checks, "wallet", "Trading-Wallet mit Schlüssel auf dem Mac", pub and has_key,
           f"Wallet {pub[:6]}…, Schlüssel {'geladen' if has_key else 'fehlt (nur Dry-Run möglich)'}" if pub
           else "keine Trading-Wallet (python -m live new-wallet)")

    strats = live_strategies(conn)
    _check(checks, "strategies", "mindestens ein Modus auf Echtgeld geschaltet", strats,
           ", ".join(strats) if strats else "live:<modus> ist überall aus")
    for s in strats:
        e = paper_edge(conn, s, cfg.gate_days)
        ok = (e["n"] >= cfg.gate_min_trades and e["net_sol"] > 0 and e["profit_factor"] >= cfg.gate_min_profit_factor
              and e["first_half"] > 0 and e["second_half"] > 0)
        _check(checks, f"paper:{s}", f"Papier-Plus nach Kosten ({s}, {cfg.gate_days:g} Tage)", ok,
               f"{e['n']} Trades (min. {cfg.gate_min_trades}), netto {e['net_sol']:+.3f} SOL, Profit-Faktor "
               f"{e['profit_factor']:.2f} (min. {cfg.gate_min_profit_factor:g}), Hälften {e['first_half']:+.3f} / "
               f"{e['second_half']:+.3f} SOL (beide > 0)")

        try:
            from copytrade import learn  # type: ignore

            autotune, min_test = learn.enabled(conn, s), learn.MIN_MODE_TEST
        except Exception:
            autotune, min_test = False, 12
        le = learned_edge(conn, s)
        ok = bool(le) and (le["test_gain"] or 0) > 0 and (le["n_test"] or 0) >= min_test
        _check(checks, f"learn:{s}", f"Gelernte Regeln out-of-sample im Plus ({s})", ok,
               (f"Version {le['id']}: Test {le['test_gain']:+.1%} pro Trade nach Kosten über {le['n_test']} "
                f"Test-Einstiege (min. {min_test}), Ausgangswert {(le['baseline_test_gain'] or 0):+.1%}") if le
               else "noch keine aktive gelernte Version", required=autotune)

    d = dry_stats(conn)
    ok = (d["buys_ok"] >= cfg.gate_min_dry_buys and d["sells_built"] >= cfg.gate_min_dry_sells
          and d["buys_ok"] >= 4 * d["buys_sim_failed"])
    _check(checks, "dry_run", "Dry-Run erfolgreich (gebaut, signiert, simuliert)", ok,
           f"{d['buys_ok']} Käufe simuliert ok (min. {cfg.gate_min_dry_buys}), {d['buys_sim_failed']} Simulation "
           f"fehlgeschlagen, {d['sells_built']} Verkäufe gebaut (min. {cfg.gate_min_dry_sells}), "
           f"{d['round_trips_booked']} Dry-Runden gebucht")
    _check(checks, "dry_book", "Dry-Run-Positionen über den echten Handelsablauf gebucht", d["round_trips_booked"] >= 3,
           f"{d['round_trips_booked']} geschlossen (min. 3)")

    tg = bool(os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"))
    _check(checks, "telegram", "Telegram eingerichtet (Not-Aus vom Handy)", tg,
           "ok" if tg else "python deploy/ops.py setup-telegram")
    hb = time.time() - float(store.get(conn, "heartbeat") or 0)
    _check(checks, "watchdog", "Live-Wächter läuft", hb < HEARTBEAT_MAX_AGE_S,
           f"letztes Lebenszeichen vor {hb:.0f}s" if hb < 1e8 else "nie gelaufen")
    k, pend = store.killed(conn), store.pending(conn)
    _check(checks, "clean", "kein Not-Aus, keine offene unklare Order", not k and not pend,
           k or (f"{len(pend)} unklare Order(s)" if pend else "ok"))

    ls = live_stats(conn, int(store.get(conn, f"stage{cfg.stage_no}_since") or 0))
    adv = ADVANCE.get(cfg.stage_no)
    advance = None
    if adv:
        from copytrade.execution import paper_adverse  # type: ignore

        par = ls["median_buy_slippage"]
        parity = par is not None and par <= paper_adverse() + 0.01
        advance = {"next": STAGES[cfg.stage_no + 1].name, "round_trips": ls["round_trips"],
                   "min_trips": adv["min_trips"], "net_sol": ls["net_sol"], "median_buy_slippage": par,
                   "paper_assumes": paper_adverse(), "parity": parity, "land_rate": ls["land_rate"],
                   "ok": ls["round_trips"] >= adv["min_trips"] and ls["net_sol"] > 0 and parity
                   and (ls["land_rate"] or 0) >= 0.9}
    ready = all(c["ok"] for c in checks if c["required"])
    return {"ready": ready, "mode": "dry" if cfg.dry_run else "live", "enabled": cfg.enabled,
            "require_gate": cfg.require_gate, "limits": cfg.limits(), "checks": checks, "dry": d, "live": ls,
            "advance": advance}
