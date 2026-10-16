"""How the brain learns, online and out of sample.

1. Prior knowledge (once, then for late arrivals): every whale buy and signal copytrade already replayed on minute
   candles (ct_evals) before the brain started, mapped onto the brain's exit plans, older ones weighted less.
2. Every opportunity, also the ones it let pass: when the longest plan is over (~4.2 h), each plan is replayed on
   1-minute candles (copytrade.sim, the whale's real sells as forced exits) and the model learns all of them at once.
3. Every own trade, the moment it closes: the real paper fill (Jupiter quote, slippage, fees) for the plan it took,
   counted double. The later replay then skips that plan so nothing is learned twice.

Before learning an outcome, the prediction made at decision time is compared with what happened. That comparison is
the honest score (`honest()`): the model had not seen the outcome when it decided.
All returns are net: the simulator's 3 % plus the brain's extra cost (copy 5.5 %, launch 9 % round trip in total,
or more when copytrade's own trades measured more).
"""
from __future__ import annotations

import json
import logging
import time

from copytrade import sim

from . import features, store
from .plans import BASE, NAMES, PLANS, gains_from_grid

log = logging.getLogger("brain.learner")

RETRY_S = 2 * 86400
CACHE_GRACE_S = 30 * 60
REAL_WEIGHT = 2.0


def horizon_s() -> int:
    return int((max(p.max_hold_min for p in PLANS) + 10) * 60)


def extra_cost(conn, s, kind: str) -> float:
    base = s.extra_cost(kind)
    if kind == "launch":
        return base
    try:
        from copytrade import learn

        return max(base, learn.extra_cost(conn, "whale" if kind == "whale" else "signal"))
    except Exception:
        return base


def _toks(kind: str, feats_json: str) -> list[str]:
    try:
        f = json.loads(feats_json or "{}")
    except ValueError:
        f = {}
    return features.tokens(kind, f)


def _record_prior(st: dict, model, toks, ys: dict, s) -> None:
    """Prequential check on prior knowledge: would the brain have been convinced before seeing it, and what came of it."""
    if not ys:
        return
    from .decide import verdict

    best, _, _, convinced = verdict(model.predict(toks), s)
    st["n"] = st.get("n", 0) + 1
    base = NAMES.index(BASE)
    if base in ys:
        st["sum_base"] = st.get("sum_base", 0.0) + ys[base]
        st["n_base"] = st.get("n_base", 0) + 1
    if convinced and best in ys:
        st["n_take"] = st.get("n_take", 0) + 1
        st["sum_take"] = st.get("sum_take", 0.0) + ys[best]


def warm_start(conn, model, s, now: float | None = None, limit: int = 5000) -> int:
    """Learn the replays copytrade stored for buys and signals from before the brain's start (later rows of that
    time keep arriving for a few hours, so this runs every learning round). Returns rows learned."""
    if not store.has(conn, "ct_evals"):
        return 0
    now = now or time.time()
    started = store.get(conn, "started_at")
    if started is None:
        started = int(now)
        store.put(conn, "started_at", started)
    cur = store.get(conn, "prior_cursor", -1)
    rows = conn.execute(
        """SELECT e.kind, e.ref, e.ts, e.gains, e.features, e.created_at FROM ct_evals e
           WHERE e.kind IN ('whale_buy','signal') AND e.grid_version=? AND e.gains!='[]' AND e.created_at>?
           AND e.created_at<? AND e.ts<? ORDER BY e.created_at, e.ts LIMIT ?""",
        (sim.GRID_VERSION, cur, int(now), started, limit)).fetchall()
    if not rows:
        return 0
    model.forget(now)
    st = store.get(conn, "prior_check", {}) or {}
    n = 0
    for r in rows:
        kind = "whale" if r["kind"] == "whale_buy" else "signal"
        grid = sim.GRID if kind == "whale" else sim.EXIT_GRID
        try:
            gains = gains_from_grid(json.loads(r["gains"]), grid)
        except ValueError:
            continue
        cost = extra_cost(conn, s, kind)
        ys = {i: g - cost for i, g in enumerate(gains) if g is not None}
        if not ys:
            continue
        toks = _toks(kind, r["features"])
        _record_prior(st, model, toks, ys, s)
        w = 0.5 ** (max(0.0, now - r["ts"]) / 3600 / s.half_life_h) if s.half_life_h > 0 else 1.0
        model.update(toks, ys, w)
        n += 1
    store.put(conn, "prior_cursor", rows[-1]["created_at"])
    store.put(conn, "prior_check", st)
    store.put(conn, "prior_rows", (store.get(conn, "prior_rows", 0) or 0) + n)
    return n


def _ct_gains(conn, o) -> list | None:
    """The plans' gains from copytrade's replay of the same buy/signal, when it already has one."""
    kind = {"whale": "whale_buy", "signal": "signal"}.get(o["kind"])
    if not kind or not store.has(conn, "ct_evals"):
        return None
    r = conn.execute("SELECT gains FROM ct_evals WHERE kind=? AND ref=? AND grid_version=? AND gains!='[]'",
                     (kind, o["ref"], sim.GRID_VERSION)).fetchone()
    if not r:
        return None
    g = gains_from_grid(json.loads(r[0]), sim.GRID if o["kind"] == "whale" else sim.EXIT_GRID)
    return g if any(x is not None for x in g) else None


def _default_candles(conn, mint: str, lo: int, hi: int, allow_net: bool = True) -> list:
    """Minute candles from the caches research/ and copytrade already fill (research_candles, ct_candles), else
    GeckoTerminal/Birdeye through copytrade.learn.candles when this round still has network budget."""
    need = max(1, (hi - lo) // 60)
    for q in ("SELECT ts,o,h,l,c,v FROM research_candles WHERE mint=? AND tf_sec=60 AND ts BETWEEN ? AND ? ORDER BY ts",
              "SELECT ts,o,h,l,c,v FROM ct_candles WHERE mint=? AND ts BETWEEN ? AND ? ORDER BY ts"):
        try:
            rows = [tuple(r) for r in conn.execute(q, (mint, lo, hi)).fetchall()]
        except Exception:
            rows = []
        if len(rows) >= need * 0.4 and rows[-1][0] >= hi - 30 * 60:
            return rows
    if not allow_net:
        return []
    from copytrade import config as ct_config, learn

    return learn.candles(conn, ct_config.load(), mint, lo, hi)


def replay(conn, o, candles_fn, allow_net: bool = True) -> list | None:
    """Gains of every plan for one opportunity (None = no data yet, [] = cannot be evaluated)."""
    g = _ct_gains(conn, o)
    if g is not None:
        return g
    t0 = int(o["decided_at"] or o["ts"])
    events = []
    if o["kind"] == "whale" and o["wallet"]:
        from copytrade import learn

        src = int(o["src_ts"] or t0)
        events = [r[0] for r in conn.execute("SELECT ts FROM ct_swaps WHERE wallet=? AND mint=? AND side='sell' AND ts>? "
                                             "ORDER BY ts", (o["wallet"], o["mint"], src)).fetchall()]
        gaps = learn.tracker_gaps(conn, t0 - 3600, t0 + horizon_s() + 3600)
        if learn._sell_unknown(gaps, src, events):
            return []  # the tracker was offline: a whale sell may be missing, so the replay would hold too long
    cs = candles_fn(conn, o["mint"], t0 - 20 * 60, t0 + horizon_s() + 180, allow_net)
    if not cs:
        return None
    out = []
    for p in PLANS:
        r = sim.simulate(cs, t0, p.variant(0), events)
        out.append(round(r[0], 4) if r else None)
    return out if any(x is not None for x in out) else None


def evaluate(conn, model, s, candles_fn=None, now: float | None = None, limit: int | None = None) -> int:
    """Replay opportunities whose longest plan is over, score the decision made back then, then learn."""
    now = now or time.time()
    candles_fn = candles_fn or _default_candles
    # copytrade's own learner replays the same buys and caches their candles: wait a little for that before
    # spending GeckoTerminal calls (shared free limit), and fetch at most `net` coins per round
    net = limit or s.evals_per_round
    rows = conn.execute("SELECT * FROM brain_opps WHERE evaluated_at IS NULL AND ts<? ORDER BY ts LIMIT ?",
                        (int(now) - horizon_s(), max(400, 10 * net))).fetchall()
    if not rows:
        return 0
    model.forget(now)
    n = 0
    for o in rows:
        allow = net > 0 and o["ts"] < now - horizon_s() - CACHE_GRACE_S
        try:
            gains = replay(conn, o, candles_fn, allow)
            if allow and _ct_gains(conn, o) is None:
                net -= 1  # may have cost a network call
        except Exception as e:
            log.debug("replay %s failed: %s", o["id"], e)
            gains = None
        if gains is None:
            if o["ts"] < now - RETRY_S:
                conn.execute("UPDATE brain_opps SET evaluated_at=?, gains='[]' WHERE id=?", (int(now), o["id"]))
            continue
        if not gains:
            conn.execute("UPDATE brain_opps SET evaluated_at=?, gains='[]' WHERE id=?", (int(now), o["id"]))
            continue
        cost = extra_cost(conn, s, o["kind"])
        ys = {i: g - cost for i, g in enumerate(gains) if g is not None}
        pi = NAMES.index(o["plan"]) if o["plan"] in NAMES else None
        conn.execute("UPDATE brain_opps SET evaluated_at=?, gains=?, realized_plan=?, realized_base=?, realized_best=? "
                     "WHERE id=?", (int(now), json.dumps(gains), ys.get(pi), ys.get(NAMES.index(BASE)),
                                    max(ys.values()) if ys else None, o["id"]))
        if o["learned_real"] and pi is not None:
            ys.pop(pi, None)  # the real fill already taught this plan
        model.update(_toks(o["kind"], o["features"]), ys, 1.0)
        n += 1
    return n


def learn_trade(conn, model, s, pos) -> bool:
    """A closed position teaches its plan at once with the real fill (double weight)."""
    o = conn.execute("SELECT * FROM brain_opps WHERE id=?", (pos["opp_id"],)).fetchone()
    if not o or o["learned_real"] or pos["pnl_pct"] is None or pos["plan"] not in NAMES:
        return False
    model.forget()
    model.update(_toks(o["kind"], o["features"]), {NAMES.index(pos["plan"]): pos["pnl_pct"]}, REAL_WEIGHT)
    conn.execute("UPDATE brain_opps SET learned_real=1 WHERE id=?", (o["id"],))
    return True


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return (sum(xs) / len(xs)) if xs else None


def honest(conn, since: float = 0, evaluated_since: float = 0) -> dict:
    """Out-of-sample scorecard: decisions made before their outcome was known, judged by the replay after costs.
    taken = what the brain entered (or would have, where a gate stopped it); passed = what it let go;
    copy_all = every opportunity with the reference plan (a dumb copier). Plus the real paper trades."""
    rows = [dict(r) for r in conn.execute(
        "SELECT kind, wanted, explore, pred_mean, realized_plan, realized_base FROM brain_opps "
        "WHERE evaluated_at>=? AND realized_plan IS NOT NULL AND ts>=?",
        (int(evaluated_since), int(since))).fetchall()]
    taken = [r for r in rows if r["wanted"]]
    passed = [r for r in rows if not r["wanted"]]
    real = conn.execute("SELECT pnl_pct, pnl_sol FROM brain_positions WHERE status='closed' AND closed_at>=?",
                        (int(since),)).fetchall()
    by_kind = {}
    for k in ("whale", "signal", "launch"):
        kr = [r for r in rows if r["kind"] == k]
        if kr:
            kt = [r for r in kr if r["wanted"]]
            by_kind[k] = {"n": len(kr), "n_taken": len(kt), "taken_mean": _mean([r["realized_plan"] for r in kt]),
                          "copy_all_mean": _mean([r["realized_base"] for r in kr])}
    return {
        "n": len(rows),
        "n_taken": len(taken), "taken_mean": _mean([r["realized_plan"] for r in taken]),
        "taken_pred": _mean([r["pred_mean"] for r in taken]),
        "n_passed": len(passed), "passed_mean": _mean([r["realized_plan"] for r in passed]),
        "copy_all_mean": _mean([r["realized_base"] for r in rows]),
        "real_n": len(real), "real_mean": _mean([r["pnl_pct"] for r in real]),
        "real_sol": sum(r["pnl_sol"] or 0 for r in real),
        "real_wins": sum(1 for r in real if (r["pnl_pct"] or 0) > 0),
        "by_kind": by_kind,
    }
