"""How the brain learns, online and out of sample.

1. Prior knowledge (once, then for late arrivals): every whale buy and signal copytrade already replayed on minute
   candles (ct_evals) before the brain started, mapped onto the brain's exit plans, older ones weighted less.
2. Every opportunity, also the ones it let pass, is replayed on 1-minute candles (copytrade.sim, the whale's real
   sells as forced exits) in two steps: the short plans (up to 45 min) about an hour after the decision, the long
   ones when the longest plan is over (~4.2 h). Each step is learned at once.
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
    if model.n == 0 and model.d <= 1 and cur != -1:  # model file lost or reset: the prior knowledge is relearned
        cur = -1
        store.put(conn, "prior_check", {})
        store.put(conn, "prior_rows", 0)
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


SHORT = [i for i, p in enumerate(PLANS) if p.max_hold_min <= 45]  # learned as soon as they are over, not after 4 h


def short_horizon_s() -> int:
    return int((max(PLANS[i].max_hold_min for i in SHORT) + 10) * 60)


def _sell_unknown(gaps, src: int, sells: list, horizon: int) -> bool:
    """copytrade.learn._sell_unknown for this horizon: an outage before the first recorded sell hides the exit."""
    first = sells[0] if sells else None
    return any(a < src + horizon and b > src and (first is None or first > b) for a, b in gaps)


def replay(conn, o, candles_fn, allow_net: bool = True, plans=None, horizon: int | None = None) -> list | None:
    """Gains of the plans (all, or the given indices; the others stay None) for one opportunity, replayed up to
    `horizon` after the decision (None = no data yet, [] = cannot be evaluated)."""
    horizon = horizon or horizon_s()
    plans = range(len(PLANS)) if plans is None else plans
    if horizon >= horizon_s():
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
        gaps = learn.tracker_gaps(conn, t0 - 3600, t0 + horizon + 3600)
        if _sell_unknown(gaps, src, events, horizon):
            return []  # the tracker was offline: a whale sell may be missing, so the replay would hold too long
    cs = candles_fn(conn, o["mint"], t0 - 20 * 60, t0 + horizon + 180, allow_net)
    if not cs:
        return None
    out = [None] * len(PLANS)
    for i in plans:
        r = sim.simulate(cs, t0, PLANS[i].variant(0), events)
        out[i] = round(r[0], 4) if r else None
    return out if any(x is not None for x in out) else None


_retry: dict = {}  # opp id -> (failed network replays, next try): a dead coin without candles is retried with backoff


def evaluate(conn, model, s, candles_fn=None, now: float | None = None, limit: int | None = None) -> int:
    """Replay opportunities in two steps and learn each step at once: the short plans (up to 45 min) about an hour
    after the decision, the long ones when the longest plan is over (~4.2 h). Then score the decision made back then.
    Returns the number of replays learned."""
    now = now or time.time()
    candles_fn = candles_fn or _default_candles
    # copytrade's own learner replays the same buys and caches their candles: for the final step wait a little for
    # that before spending GeckoTerminal calls (shared free limit), and fetch at most `net` coins per round
    net = limit or s.evals_per_round
    rows = conn.execute("SELECT * FROM brain_opps WHERE evaluated_at IS NULL AND ts<? ORDER BY ts LIMIT ?",
                        (int(now) - short_horizon_s(), max(400, 10 * net))).fetchall()
    if not rows:
        return 0
    model.forget(now)
    n = 0
    hz = horizon_s()
    for o in rows:
        final = o["ts"] < now - hz
        prev = json.loads(o["gains"]) if o["gains"] else None  # set = the short step is done
        if prev is not None and not final:
            continue
        if final:
            todo = [i for i in range(len(PLANS)) if prev is None or i not in SHORT]
            # oldest first: without a backoff the coins that never get candles took the whole network budget every
            # round for two days (RETRY_S) and fresh outcomes were never fetched
            allow = net > 0 and o["ts"] < now - hz - CACHE_GRACE_S and now >= _retry.get(o["id"], (0, 0))[1]
            horizon = hz
        else:
            todo, allow, horizon = SHORT, net > 0, short_horizon_s()
        try:
            gains = replay(conn, o, candles_fn, allow, todo, horizon)
            if allow and (not final or _ct_gains(conn, o) is None):
                net -= 1  # may have cost a network call
        except Exception as e:
            log.debug("replay %s failed: %s", o["id"], e)
            gains = None
        if gains is None and not final:
            continue  # no candles yet: the final step tries again with the full window
        if gains is None and o["ts"] >= now - RETRY_S:
            if allow:
                k = _retry.get(o["id"], (0, 0))[0] + 1
                _retry[o["id"]] = (k, now + min(6 * 3600, 300 * 2 ** k))
            continue
        _retry.pop(o["id"], None)
        merged = (list(prev or []) + [None] * len(PLANS))[:len(PLANS)]
        for i in todo:
            if gains and gains[i] is not None:
                merged[i] = gains[i]
        if not final:
            conn.execute("UPDATE brain_opps SET gains=? WHERE id=?", (json.dumps(gains or []), o["id"]))
        if gains:
            cost = extra_cost(conn, s, o["kind"])
            ys = {i: g - cost for i, g in enumerate(gains) if g is not None and i in todo}
            pi = NAMES.index(o["plan"]) if o["plan"] in NAMES else None
            if o["learned_real"] and pi is not None:
                ys.pop(pi, None)  # the real fill already taught this plan
            if ys:
                model.update(_toks(o["kind"], o["features"]), ys, 1.0)
                n += 1
        if final:
            if not any(g is not None for g in merged):
                conn.execute("UPDATE brain_opps SET evaluated_at=?, gains='[]' WHERE id=?", (int(now), o["id"]))
                continue
            cost = extra_cost(conn, s, o["kind"])
            ya = {i: g - cost for i, g in enumerate(merged) if g is not None}
            pi = NAMES.index(o["plan"]) if o["plan"] in NAMES else None
            conn.execute("UPDATE brain_opps SET evaluated_at=?, gains=?, realized_plan=?, realized_base=?, "
                         "realized_best=? WHERE id=?", (int(now), json.dumps(merged), ya.get(pi),
                                                        ya.get(NAMES.index(BASE)), max(ya.values()), o["id"]))
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
