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

from . import exits, features, store
from .plans import BASE, NAMES, PLANS, gains_from_grid, nearest_delay

log = logging.getLogger("brain.learner")

RETRY_S = 2 * 86400
CACHE_GRACE_S = 30 * 60
REAL_WEIGHT = 2.0
PRIOR_DELAY_S = 60  # copytrade's grid delay used for prior whale replays: the brain enters ~30-90 s after the whale
SIGNAL_CT_MAX_DELAY_S = 30  # copytrade replays signals 15 s after the signal; a later brain decision replays itself


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
    if model.n == 0 and model.d <= 1 and (cur != -1 or store.get(conn, "relearned_at") is None):
        # model file lost, damaged or reset: relearn the prior knowledge and everything the brain itself replayed or
        # traded (all stored in brain_opps / brain_positions), no network needed
        cur = -1
        store.put(conn, "prior_check", {})
        store.put(conn, "prior_rows", 0)
        n_own = relearn(conn, model, s, now)
        store.put(conn, "relearned_at", int(now))
        if n_own:
            log.info("model was empty: relearned %d own outcomes from brain_opps/brain_positions", n_own)
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
            gains = gains_from_grid(json.loads(r["gains"]), grid, PRIOR_DELAY_S if kind == "whale" else None)
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


def relearn(conn, model, s, now: float | None = None) -> int:
    """Rebuild an empty model from the brain's own stored outcomes: every replay (brain_opps.gains, also the short
    step) and every closed paper trade (double weight), older ones weighted less like the forgetting would."""
    now = now or time.time()

    def w(ts):
        return 0.5 ** (max(0.0, now - (ts or now)) / 3600 / s.half_life_h) if s.half_life_h > 0 else 1.0

    n = 0
    for o in conn.execute("SELECT * FROM brain_opps WHERE gains IS NOT NULL AND gains!='[]' ORDER BY ts").fetchall():
        try:
            gains = json.loads(o["gains"])
        except ValueError:
            continue
        cost = extra_cost(conn, s, o["kind"])
        ys = {i: g - cost for i, g in enumerate(gains[:len(PLANS)]) if g is not None}
        if o["learned_real"] and o["plan"] in NAMES:
            ys.pop(NAMES.index(o["plan"]), None)
        if ys:
            model.update(_toks(o["kind"], o["features"]), ys, w(o["ts"]))
            n += 1
    for p in conn.execute("""SELECT o.kind, o.features, p.plan, p.pnl_pct, p.closed_at FROM brain_positions p
                             JOIN brain_opps o ON o.id = p.opp_id WHERE p.status='closed' AND o.learned_real=1
                             AND p.pnl_pct IS NOT NULL""").fetchall():
        if p["plan"] in NAMES:
            model.update(_toks(p["kind"], p["features"]), {NAMES.index(p["plan"]): p["pnl_pct"]},
                         REAL_WEIGHT * w(p["closed_at"]))
            n += 1
    return n


def _ct_gains(conn, o) -> list | None:
    """The plans' gains from copytrade's replay of the same buy/signal, when it already has one."""
    kind = {"whale": "whale_buy", "signal": "signal"}.get(o["kind"])
    if not kind or not store.has(conn, "ct_evals"):
        return None
    # the plans must start when the brain decided, not when copytrade's replay entered: whale buys take copytrade's
    # grid delay nearest to the brain's own, signals decided later than copytrade's entry replay themselves
    delay = (o["decided_at"] or o["ts"]) - (o["src_ts"] or o["ts"])
    if o["kind"] == "signal" and delay > SIGNAL_CT_MAX_DELAY_S:
        return None
    r = conn.execute("SELECT gains FROM ct_evals WHERE kind=? AND ref=? AND grid_version=? AND gains!='[]'",
                     (kind, o["ref"], sim.GRID_VERSION)).fetchone()
    if not r:
        return None
    if o["kind"] == "whale":
        g = gains_from_grid(json.loads(r[0]), sim.GRID, nearest_delay(delay))
    else:
        g = gains_from_grid(json.loads(r[0]), sim.EXIT_GRID)
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


def _exit_step(conn, xm, s, o, candles_fn, from_min: float, final: bool, merged=None) -> int:
    """Organic exits learn from the same coin: first (final step only) replay the organic exit with the exit model as
    it is now, i.e. before it saw this coin, next to the chosen plan; then learn the coin's minutes from from_min on.
    Cached candles only (the plan replay just fetched or cached them). Returns minute-samples learned."""
    t0 = int(o["decided_at"] or o["ts"])
    to_min = exits.MAX_HOLD_MIN if final else max(PLANS[i].max_hold_min for i in SHORT)
    span = (to_min + exits.FWD_MIN + 5) * 60
    cs = candles_fn(conn, o["mint"], t0 - 20 * 60, t0 + span, False)
    if not cs:
        return 0
    try:
        f = json.loads(o["features"] or "{}")
    except ValueError:
        f = {}
    swaps = exits.coin_swaps(conn, o["mint"], t0 - 3600, t0 + span)
    src = o["src_ts"] or t0
    sells = [ts for ts, w, side in swaps if w == o["wallet"] and side == "sell" and ts > src]
    args = (cs, t0, o["kind"], f.get("liq"), o["wallet"], swaps, sells)
    if final and merged is not None and o["plan"] in NAMES:
        cost = extra_cost(conn, s, o["kind"])
        org = exits.replay(xm, *args, s, sim.ROUNDTRIP_COST + cost) if exits.mature(xm) else None
        pg = merged[NAMES.index(o["plan"])]
        i0 = exits._entry_index(cs, t0)
        hold = max_up = None
        if i0 < len(cs) - 1 and cs[i0][4] > 0:
            e = cs[i0][4]
            later = [c for c in cs[i0 + 1:] if c[0] - cs[i0][0] <= exits.MAX_HOLD_MIN * 60]
            hour = [c for c in later if c[0] - cs[i0][0] <= 3600]
            hold = hour[-1][4] / e - 1 - sim.ROUNDTRIP_COST - cost if hour else None
            max_up = max(c[2] or c[4] for c in later) / e - 1 if later else None
        conn.execute("INSERT OR REPLACE INTO brain_exit_evals(opp_id, ts, kind, plan, plan_gain, organic, "
                     "organic_reason, organic_min, hold_1h, max_up, evaluated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (o["id"], o["ts"], o["kind"], o["plan"], None if pg is None else pg - cost,
                      org[0] if org else None, org[2] if org else None, org[1] if org else None, hold, max_up,
                      int(time.time())))
    return exits.learn_path(xm, *args, from_min, to_min)


REGIME_WINDOW_S = 6 * 3600
REGIME_MIN_N = 30
REGIME_Z = 3.0  # the last hours' outcomes miss the predictions by more than 3 standard errors
REGIME_MIN_ERR = 0.05  # and by at least 5 percentage points per trade
REGIME_KEEP = 0.5  # then halve all old evidence at once (the half-life alone would take 120 h)
REGIME_COOLDOWN_S = 3 * 3600


def regime_check(conn, model, xm, now: float | None = None) -> dict | None:
    """The market is not always what the brain learned. Compare the last hours' replayed outcomes with what the model
    predicted for them when it decided (out of sample): a systematic miss in one direction means the regime changed,
    so old evidence is faded at once and the recent outcomes weigh more. Returns the shift, or None."""
    now = now or time.time()
    last = store.get(conn, "regime", {}) or {}
    if now - (last.get("at") or 0) < REGIME_COOLDOWN_S:
        return None
    rows = conn.execute("SELECT realized_plan, pred_mean FROM brain_opps WHERE evaluated_at>=? AND realized_plan IS "
                        "NOT NULL AND pred_mean IS NOT NULL AND kind!='pool'", (int(now - REGIME_WINDOW_S),)).fetchall()
    if len(rows) < REGIME_MIN_N:
        return None
    errs = [r[0] - r[1] for r in rows]
    n = len(errs)
    m = sum(errs) / n
    sd = (sum((e - m) ** 2 for e in errs) / (n - 1)) ** 0.5
    z = m / (sd / n ** 0.5) if sd > 0 else 0.0
    if abs(z) < REGIME_Z or abs(m) < REGIME_MIN_ERR:
        return None
    model.fade(REGIME_KEEP)
    if xm is not None:
        xm.fade(REGIME_KEEP)
    shift = {"at": int(now), "n": n, "err": round(m, 4), "z": round(z, 2)}
    store.put(conn, "regime", shift)
    hist = (store.get(conn, "regime_history", []) or [])[-29:] + [shift]
    store.put(conn, "regime_history", hist)
    log.info("regime shift: last %d outcomes %+.1f%% vs predicted (z %.1f): old evidence halved", n, m * 100, z)
    return shift


def backfill_exits(conn, xm, s, candles_fn=None, limit: int = 150) -> int:
    """Opportunities finished before the organic exits existed: learn their minutes (cached candles, oldest first,
    `limit` per round) so the exit model does not start from nothing. Returns opportunities done."""
    if xm is None:
        return 0
    candles_fn = candles_fn or _default_candles
    cur = store.get(conn, "exit_backfill_id", 0) or 0
    rows = conn.execute("SELECT * FROM brain_opps WHERE evaluated_at IS NOT NULL AND id>? AND gains!='[]' "
                        "ORDER BY id LIMIT ?", (cur, limit)).fetchall()
    if not rows:
        return 0
    xm.forget()
    for o in rows:
        try:
            merged = json.loads(o["gains"]) if o["gains"] else None
            _exit_step(conn, xm, s, o, candles_fn, 0, True, merged if merged and len(merged) == len(PLANS) else None)
        except Exception as e:
            log.debug("exit backfill %s failed: %s", o["id"], e)
        cur = o["id"]
    store.put(conn, "exit_backfill_id", cur)
    return len(rows)


def evaluate(conn, model, s, candles_fn=None, now: float | None = None, limit: int | None = None, xm=None,
             pool=None) -> int:
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
    if xm is not None:
        xm.forget(now)
    if pool is not None:
        pool.forget(now)
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
        if o["kind"] in ("market", "pool") and net <= (limit or s.evals_per_round) // 2:
            allow = False  # side sources get at most half of the round's candle downloads (shared free limit)
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
                if o["kind"] != "pool":
                    model.update(_toks(o["kind"], o["features"]), ys, 1.0)
                elif pool is not None:
                    pool.update(_toks(o["kind"], o["features"]), ys, 1.0)
                n += 1
            if xm is not None and not final:
                try:
                    _exit_step(conn, xm, s, o, candles_fn, 0, False)
                except Exception as e:
                    log.debug("exit learning %s failed: %s", o["id"], e)
        if final and xm is not None and any(g is not None for g in merged):
            try:
                _exit_step(conn, xm, s, o, candles_fn, 0 if prev is None else max(PLANS[i].max_hold_min for i in SHORT),
                           True, merged)
            except Exception as e:
                log.debug("exit learning %s failed: %s", o["id"], e)
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
            if o["kind"] == "pool":
                _pool_outcome(conn, o, ya, merged, cost, pi, now)
    return n


def _pool_outcome(conn, o, ya: dict, merged: list, cost: float, pi, now: float) -> None:
    """Write the honest result back to the shared opportunities row: net after costs with the plan the brain chose
    (else the base plan), every plan's net for the challenger models, and whether the coin rugged."""
    try:
        oid = int(json.loads(o["raw"] or "{}").get("opp") or 0)
    except (TypeError, ValueError):
        oid = 0
    if not oid:
        return
    plan = o["plan"] if pi is not None and ya.get(pi) is not None else BASE
    net = ya.get(NAMES.index(plan))
    if net is None:
        return
    gross = merged[NAMES.index(plan)]
    rug = int(min(g for g in merged if g is not None) <= -0.9)
    cur = conn.execute("SELECT exit_plan FROM opportunities WHERE id=?", (oid,)).fetchone()
    if cur and cur[0] == "real":  # the position's real fill is already there: add only the replays beside it
        conn.execute("UPDATE opportunities SET outcomes=?, rug=MAX(COALESCE(rug,0), ?) WHERE id=?",
                     (json.dumps({NAMES[i]: round(v, 5) for i, v in ya.items()}), rug, oid))
        return
    conn.execute("UPDATE opportunities SET evaluated_at=?, exit_plan=?, outcome_net=?, outcome_gross=?, cost=?, rug=?, "
                 "outcomes=? WHERE id=?", (int(now), plan, round(net, 5), gross, round(cost, 5), rug,
                                          json.dumps({NAMES[i]: round(v, 5) for i, v in ya.items()}), oid))


LABEL_DEDUP_S = 6 * 3600  # one label per source and coin in this window
LABEL_WEIGHT = 0.5  # an hour on sampled prices: less than a minute-candle replay (1.0) or a real fill (2.0)


def learn_labels(conn, pool, s, limit: int = 3000) -> int:
    """The pool model learns every labelled opportunity (labels.py): each plan's net on the hour after the report.
    The features are the ones the brain decided on where it decided, else what the source reported."""
    if pool is None or not store.has(conn, "opportunities"):
        return 0
    cur = store.get(conn, "label_learn_cursor") or [0, 0]
    try:
        rows = conn.execute("SELECT id, source, mint, ts, features, label_at, label_outcomes FROM opportunities WHERE "
                            "label_at IS NOT NULL AND (label_at>? OR (label_at=? AND id>?)) ORDER BY label_at, id "
                            "LIMIT ?", (cur[0], cur[0], cur[1], limit)).fetchall()
    except Exception:  # columns not there yet (a reader's connection)
        return 0
    if not rows:
        return 0
    pool.forget()
    n = 0
    for r in rows:
        try:
            outs = json.loads(r["label_outcomes"] or "{}")
        except (TypeError, ValueError):
            outs = {}
        ys = {NAMES.index(k): v for k, v in outs.items() if k in NAMES and v is not None}
        if not ys:
            continue
        if conn.execute("SELECT 1 FROM opportunities WHERE mint=? AND ts>=? AND ts<=? AND source=? AND id<? "
                        "AND label_at IS NOT NULL LIMIT 1",
                        (r["mint"], (r["ts"] or 0) - LABEL_DEDUP_S, r["ts"] or 0, r["source"], r["id"])).fetchone():
            continue  # the same coin from the same source again (copytrade/strategy: many whales, one path): learned once
        o = conn.execute("SELECT features FROM brain_opps WHERE kind='pool' AND ref=?", (f"opp{r['id']}",)).fetchone()
        try:
            feats = json.loads(o[0]) if o else features.pool_reported(r)
        except (TypeError, ValueError):
            feats = features.pool_reported(r)
        pool.update(features.tokens("pool", feats), ys, LABEL_WEIGHT)
        n += 1
    store.put(conn, "label_learn_cursor", [rows[-1]["label_at"], rows[-1]["id"]])
    return n


def _real_outcome(conn, o, pos) -> None:
    """A pooled opportunity the learning account bought: its real result after costs goes into the shared row at
    once (exit_plan 'real'), instead of waiting ~4 h for the final replay. The paper fill after a rug is the quote
    on the drained pool, so it already is the honest value."""
    try:
        oid = int(json.loads(o["raw"] or "{}").get("opp") or 0)
    except (TypeError, ValueError):
        oid = 0
    if not oid:
        return
    conn.execute("UPDATE opportunities SET evaluated_at=?, exit_plan='real', outcome_net=?, rug=? WHERE id=?",
                 (int(pos["closed_at"] or time.time()), round(pos["pnl_pct"], 5), int(pos["exit_reason"] == "rug"),
                  oid))


def learn_trade(conn, model, s, pos, pool=None) -> bool:
    """A closed position teaches its plan at once with the real fill (double weight). A learning-account position
    (pool) teaches the pool model with it whatever the exit was: what the learning account has to learn is whether
    such a buy pays as it is really traded."""
    o = conn.execute("SELECT * FROM brain_opps WHERE id=?", (pos["opp_id"],)).fetchone()
    if not o or o["learned_real"] or pos["pnl_pct"] is None or pos["plan"] not in NAMES:
        return False
    if o["kind"] == "pool":
        _real_outcome(conn, o, pos)
        if pool is None:
            return False
        model = pool
    else:
        try:
            if json.loads(pos["meta"] or "{}").get("exit_mode") == "organic":
                return False  # the organic exit's result is not the plan's: the plan learns from its replay
        except (TypeError, ValueError):
            pass
    model.forget()
    model.update(_toks(o["kind"], o["features"]), {NAMES.index(pos["plan"]): pos["pnl_pct"]}, REAL_WEIGHT)
    conn.execute("UPDATE brain_opps SET learned_real=1 WHERE id=?", (o["id"],))
    return True


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return (sum(xs) / len(xs)) if xs else None


def _capture(gain, max_up):
    """Share of the coin's best move after the buy the exit took home (only where there was a move of 10 %+)."""
    return gain / max_up if gain is not None and max_up and max_up >= 0.1 else None


def _by_exit(real) -> dict:
    """Own closed trades by who decided the exit: the plans' fixed rules or the organic exit."""
    out = {}
    for r in real:
        try:
            m = json.loads(r["meta"] or "{}") or {}
        except (TypeError, ValueError):
            m = {}
        k = "organic" if m.get("exit_mode") == "organic" else "plan"
        ref = m.get("ref_price") or r["entry_price_sol"]
        up = (r["high_price_sol"] / ref - 1) if ref and r["high_price_sol"] else None
        d = out.setdefault(k, {"n": 0, "pcts": [], "sol": 0.0, "caps": []})
        d["n"] += 1
        d["pcts"].append(r["pnl_pct"])
        d["sol"] += r["pnl_sol"] or 0
        d["caps"].append(_capture(r["pnl_pct"], up))
    return {k: {"n": d["n"], "mean": _mean(d["pcts"]), "sol": round(d["sol"], 4), "capture": _mean(d["caps"])}
            for k, d in out.items()}


def exit_replays(conn, since: float = 0, evaluated_since: float = 0) -> dict:
    """Out-of-sample replays of the same opportunities: organic exit vs the chosen plan vs buy and hold 1 h, and how
    much of each coin's best move (within 4 h) each of them took home."""
    try:
        rows = conn.execute("SELECT organic, plan_gain, hold_1h, max_up FROM brain_exit_evals WHERE organic IS NOT NULL "
                            "AND plan_gain IS NOT NULL AND evaluated_at>=? AND ts>=?",
                            (int(evaluated_since), int(since))).fetchall()
    except Exception:
        rows = []
    return {"n": len(rows), "organic": _mean([r[0] for r in rows]), "plan": _mean([r[1] for r in rows]),
            "hold_1h": _mean([r[2] for r in rows]),
            "capture_organic": _mean([_capture(r[0], r[3]) for r in rows]),
            "capture_plan": _mean([_capture(r[1], r[3]) for r in rows])}


def honest(conn, since: float = 0, evaluated_since: float = 0) -> dict:
    """Out-of-sample scorecard: decisions made before their outcome was known, judged by the replay after costs.
    taken = what the brain was convinced of (entered, or would have where a gate stopped it); explored = its small
    exploration buys (or tries a gate stopped); passed = what it let go;
    copy_all = every opportunity with the reference plan (a dumb copier). Plus the real paper trades."""
    rows = [dict(r) for r in conn.execute(
        "SELECT kind, wanted, explore, pred_mean, realized_plan, realized_base, features FROM brain_opps "
        "WHERE evaluated_at>=? AND realized_plan IS NOT NULL AND ts>=?",
        (int(evaluated_since), int(since))).fetchall()]
    for r in rows:  # signal v1 and v2 are scored apart
        try:
            f = json.loads(r.pop("features") or "{}")
        except ValueError:
            f = {}
        if r["kind"] == "signal" and f.get("sig") == "v2":
            r["kind"] = "signal_v2"
        r["delay"], r["liq"] = f.get("delay"), f.get("liq")
    taken = [r for r in rows if r["wanted"]]
    explored = [r for r in rows if not r["wanted"] and r["explore"]]
    passed = [r for r in rows if not r["wanted"] and not r["explore"]]
    real = conn.execute("SELECT pnl_pct, pnl_sol, meta, entry_price_sol, high_price_sol FROM brain_positions "
                        "WHERE status='closed' AND closed_at>=?", (int(since),)).fetchall()
    by_kind = {}
    for k in ("whale", "signal", "signal_v2", "launch", "market", "pool"):
        kr = [r for r in rows if r["kind"] == k]
        if kr:
            kt = [r for r in kr if r["wanted"]]
            by_kind[k] = {"n": len(kr), "n_taken": len(kt), "taken_mean": _mean([r["realized_plan"] for r in kt]),
                          "copy_all_mean": _mean([r["realized_base"] for r in kr])}
    def seg(rs):
        st = [r for r in rs if r["wanted"]]
        return {"n": len(rs), "n_taken": len(st), "taken_mean": _mean([r["realized_plan"] for r in st]),
                "n_explored": sum(1 for r in rs if not r["wanted"] and r["explore"]),
                "copy_all_mean": _mean([r["realized_base"] for r in rs])}
    # whale copies by how late the tracker saw the buy, and curve vs pool coins: the segments BRAIN_NO_EXPLORE names
    by_delay = {d: seg(rs) for d in ("<5s", "5-15s", "15-30s", "30-90s", ">90s")
                if (rs := [r for r in rows if r["kind"] == "whale" and r["delay"] == d])}
    by_liq = {k: seg(rs) for k, rs in (("curve", [r for r in rows if r["liq"] == "curve"]),
                                       ("pool", [r for r in rows if r["liq"] and r["liq"] != "curve"])) if rs}
    return {
        "alpha": (None if _mean([r["realized_plan"] for r in taken]) is None or _mean([r["realized_base"] for r in rows])
                  is None else _mean([r["realized_plan"] for r in taken]) - _mean([r["realized_base"] for r in rows])),
        "by_exit": _by_exit(real),
        "exit_replays": exit_replays(conn, since, evaluated_since),
        "n": len(rows),
        "n_taken": len(taken), "taken_mean": _mean([r["realized_plan"] for r in taken]),
        "taken_pred": _mean([r["pred_mean"] for r in taken]),
        "n_passed": len(passed), "passed_mean": _mean([r["realized_plan"] for r in passed]),
        "n_explored": len(explored), "explored_mean": _mean([r["realized_plan"] for r in explored]),
        "copy_all_mean": _mean([r["realized_base"] for r in rows]),
        "real_n": len(real), "real_mean": _mean([r["pnl_pct"] for r in real]),
        "real_sol": sum(r["pnl_sol"] or 0 for r in real),
        "real_wins": sum(1 for r in real if (r["pnl_pct"] or 0) > 0),
        "by_kind": by_kind,
        "by_delay": by_delay,
        "by_liq": by_liq,
    }
