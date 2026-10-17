"""Launch research on real outcomes: which launch features predict the next hour, which exit rules pay after
costs, where bundle and insider thresholds should sit, and whether any entry rule holds up on unseen data.

Everything is measured as profit after costs for a paper entry at decision time (20 s after the creation):
curve fee and adverse slippage on both sides plus the priority fee per transaction. Exits are replayed on the
stored price path (sniper_paths: 5 s buckets with low/high, so the order of stop and take-profit is known);
launches recorded before paths existed are replayed on their four clean samples (1/5/15/60 min after the
decision), a coarse path that misses the extremes in between.

A rule found here is only "accepted" when it is profitable after costs on the later, unseen part of the data
(time split, bootstrap lower bound above zero, enough trades). Only then may the engine use it, in paper mode.

    python -m sniper research [--hours 0] [--json] [--save]
"""
import json
import math
import random
import time

from . import store

# features as stored in sniper_launches (stage 1 values at decision time) plus a few derived ones
BASE_FEATURES = ["n_trades", "n_buyers", "n_sellers", "buy_sol", "sell_sol", "net_sol", "momentum", "prescore",
                 "unseen_pct", "bundle_pct", "bundle_wallets", "dev_buy_pct", "dev_sold_pct", "top10_pct",
                 "fast_buyers", "dev_launches", "dev_launches_24h", "n_smart", "n_good", "curve_progress",
                 "mcap_decide_sol", "risk", "block0_wallets"]
# rug_p (safety's launch model) is left out on purpose: that model is trained on these same launches, so it
# would leak the later outcomes into the earlier half of the time split
DERIVED = {
    "buy_share": lambda r: _div(r["buy_sol"], (r["buy_sol"] or 0) + (r["sell_sol"] or 0)),
    "buyers_minus_sellers": lambda r: (r["n_buyers"] or 0) - (r["n_sellers"] or 0),
    "sol_per_buyer": lambda r: _div(r["buy_sol"], max(1, r["n_buyers"] or 0)),
    "mcap_change": lambda r: _div(r["mcap_decide_sol"], 30.0, -1.0) if r["mcap_decide_sol"] else None,
    "seller_share": lambda r: _div(r["n_sellers"], max(1, (r["n_buyers"] or 0) + (r["n_sellers"] or 0))),
}
FEATURES = BASE_FEATURES + list(DERIVED)
# bundle and insider checks, calibrated against rugs and doublings
RISK_FEATURES = ["unseen_pct", "bundle_pct", "dev_buy_pct", "dev_sold_pct", "top10_pct", "fast_buyers",
                 "dev_launches_24h", "bundle_wallets", "seller_share"]

EXIT_GRID = [  # (tp1_pct, tp1_frac, trail_pct, stop_pct, time_stop_min, max_hold_min)
    (tp, frac, trail, stop, ts, 60)
    for tp in (20, 40, 60, 100, 200)
    for frac in (0.5, 1.0)
    for trail in (15, 30)
    for stop in (15, 30, 50)
    for ts in (3, 12)
    if not (frac == 1.0 and trail == 30)   # the trail is irrelevant when everything is sold at tp1
]


def _div(a, b, sub=0.0):
    try:
        return a / b + sub if b else None
    except TypeError:
        return None


# ------------------------------------------------------------------ data
# a pump.fun coin on its curve is never worth less than at its creation (30 virtual SOL / 1.073e9 tokens =
# 27.96 SOL); a lower decision price is a broken quote, and a later normal price against it looked like +4,500 %
MIN_BASE_MCAP = 27.9
MAX_TRADE_NET = 5.0     # one trade counts at most +500 % in means (a single 50x must not carry a rule)


def pseudo_path(r):
    """Without a stored path: the four point samples after the decision (1/5/15/60 min) as a coarse path.
    They are clean, unlike max/min of launches recorded before the outcome fix (which counted the pre-decision
    pump as upside)."""
    dt = (r.get("decided_ts") or r["created_ts"]) - r["created_ts"]
    base = r["mcap_decide_sol"]
    pts = [[dt, base, base, base]]
    for h in (1, 5, 15, 60):
        v = r.get(f"ret_{h}m")
        if v is not None:
            m = base * (1 + v)
            pts.append([dt + 60 * h, m, m, m])
    return pts


def load(conn, since=None, min_trades=1, limit=30000):
    """Launches with outcomes and trade data at decision time (pump.fun stream), oldest first."""
    since = since or 0
    fix_ts = float(store.get_state(conn, "outcome_fix_ts") or 9e18)
    cur = conn.execute(
        "SELECT * FROM sniper_launches WHERE ret_60m IS NOT NULL AND mcap_decide_sol >= ? AND source = 'pumpportal' "
        # decided on a finished curve (migration before the decision): the base is the curve's end value
        "AND COALESCE(curve_progress, 0) < 0.95 AND COALESCE(migrated_ts, 9e18) > COALESCE(decided_ts, 0) "
        "AND COALESCE(n_trades, 0) >= ? AND COALESCE(created_ts, 0) >= ? ORDER BY created_ts DESC LIMIT ?",
        (MIN_BASE_MCAP, min_trades, since, limit))
    cols = [c[0] for c in cur.description]
    rows = []
    for t in cur.fetchall():
        r = dict(zip(cols, t))
        # the rule is matched live on the stage-1 features; stage 2 overwrites those columns later (more trades,
        # the enrichment), so research reads the stage-1 snapshot, and skips deep-checked rows from before it
        try:
            s1 = json.loads(r.get("stage1_json") or "null")
        except ValueError:
            s1 = None
        if s1:
            r.update({k: v for k, v in s1.items() if k in BASE_FEATURES})
        elif (r.get("stage") or 1) >= 2:
            continue
        for k, fn in DERIVED.items():
            try:
                r[k] = fn(r)
            except Exception:  # noqa: BLE001
                r[k] = None
        clean = (r.get("decided_ts") or 0) >= fix_ts
        pts = [r.get(f"ret_{h}m") for h in (1, 5, 15, 60) if r.get(f"ret_{h}m") is not None]
        r["max_clean"] = r.get("max_ret_60m") if clean else max(pts, default=0.0)
        r["x2"] = int((r["max_clean"] or 0) >= 1.0)
        r["rug"] = int(r.get("rugged") or 0)
        rows.append(r)
    rows.reverse()   # newest `limit` launches, oldest first (the time split needs the order)
    return rows


def costs(cfg):
    """Cost model per side, the same as the paper book: curve fee + adverse slippage, priority fee per tx."""
    side = cfg.curve_fee_pct / 100 + cfg.adverse_slippage
    tx = cfg.priority_fee_sol / cfg.size_sol if cfg.size_sol else 0.0
    return side, tx


# ------------------------------------------------------------------ exit replay
def replay(points, dt_entry, entry_mcap, rule, side_cost, tx_cost):
    """Net return of one paper trade: entry at dt_entry seconds after creation at entry_mcap, exits as in
    PaperBook.check (stop, tp1 + share, trailing stop after tp1, time stop, max hold). Within one bucket the
    stop is checked before the take-profit (pessimistic). Returns (net_return, exit_reason)."""
    tp1, frac, trail, stop, tstop, maxh = rule
    left, got, txs = 1.0, 0.0, 1
    tp_done, peak, reason, last = False, entry_mcap, None, entry_mcap
    stop_lvl = entry_mcap * (1 - stop / 100)
    for k, lst, lo, hi in points:
        if k < dt_entry:
            continue
        held = (k - dt_entry) / 60
        last = lst
        if not tp_done and lo <= stop_lvl:
            # a bucket that gaps through the stop and closes below it fills at its close (a rug on the curve is
            # sold after the crash, as PaperBook.check does), a wick that recovers at 5 % under the stop; never below lo
            got += left * max(lo, min(stop_lvl * 0.95, lst)) / entry_mcap
            left, txs, reason = 0.0, txs + 1, "stop"
            break
        if not tp_done and hi >= entry_mcap * (1 + tp1 / 100):
            sell = left * frac
            got += sell * (1 + tp1 / 100)
            left, txs, tp_done, peak = left - sell, txs + 1, True, hi
            if left <= 1e-9:
                reason = "tp1"
                break
            continue
        if tp_done:
            peak = max(peak, hi)
            lvl = peak * (1 - trail / 100)
            if lo <= lvl:
                got += left * max(lo, min(lvl * 0.95, lst)) / entry_mcap
                left, txs, reason = 0.0, txs + 1, "trail"
                break
        if not tp_done and held >= tstop:
            got += left * lst / entry_mcap
            left, txs, reason = 0.0, txs + 1, "time"
            break
        if held >= maxh:
            got += left * lst / entry_mcap
            left, txs, reason = 0.0, txs + 1, "max_hold"
            break
    if left > 0:
        got += left * last / entry_mcap
        txs += 1
        reason = reason or "end"
    gross = got * (1 - side_cost) * (1 - side_cost)   # entry and exit side
    return gross - 1 - tx_cost * txs, reason


def trade_returns(rows, paths, rule, side_cost, tx_cost):
    out = []
    for r in rows:
        p = paths.get(r["mint"]) or pseudo_path(r)
        dt = (r.get("decided_ts") or r["created_ts"]) - r["created_ts"]
        out.append(min(MAX_TRADE_NET, replay(p, dt, r["mcap_decide_sol"], rule, side_cost, tx_cost)[0]))
    return out


# ------------------------------------------------------------------ statistics
def _rank(xs):
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    rk = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for k in range(i, j + 1):
            rk[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return rk


def spearman(x, y):
    if len(x) < 10:
        return None
    rx, ry = _rank(x), _rank(y)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    sxy = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    sx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    sy = math.sqrt(sum((b - my) ** 2 for b in ry))
    return round(sxy / (sx * sy), 3) if sx and sy else None


def auc(x, y):
    """Probability that a positive (y=1) has a higher x than a negative; 0.5 = no information."""
    n1 = sum(y)
    n0 = len(y) - n1
    if not n1 or not n0:
        return None
    rk = _rank(x)
    return round((sum(r for r, t in zip(rk, y) if t) - n1 * (n1 + 1) / 2) / (n1 * n0), 3)


def mean(xs):
    return sum(xs) / len(xs) if xs else None


def trimmed_mean(xs, cut=0.1):
    s = sorted(xs)
    k = int(len(s) * cut)
    s = s[k:len(s) - k] or s
    return sum(s) / len(s)


def boot_ci(xs, n=1000, q=0.05, seed=7):
    """Bootstrap interval of the mean (q, 1-q)."""
    if len(xs) < 5:
        return None, None
    rnd = random.Random(seed)
    ms = sorted(mean([xs[rnd.randrange(len(xs))] for _ in xs]) for _ in range(n))
    return round(ms[int(q * n)], 4), round(ms[int((1 - q) * n) - 1], 4)


def quantiles(xs, k):
    s = sorted(xs)
    return [s[min(len(s) - 1, int(len(s) * i / k))] for i in range(1, k)]


# ------------------------------------------------------------------ analyses
def feature_power(rows, rets):
    """Per feature: Spearman IC against the 60 min return and against the net trade result, AUC for doubling
    and for rug, and the net result per quintile."""
    out = []
    for f in FEATURES:
        idx = [i for i, r in enumerate(rows) if r.get(f) is not None]
        xs = [float(rows[i][f]) for i in idx]
        if len(idx) < 50 or len(set(xs)) < 3:
            continue
        q = quantiles(xs, 5)
        buckets = []
        for b in range(5):
            lo = q[b - 1] if b else -math.inf
            hi = q[b] if b < 4 else math.inf
            sel = [i for i, x in zip(idx, xs) if lo < x <= hi]
            if not sel:
                continue
            rs = [rets[i] for i in sel]
            buckets.append({"from": None if b == 0 else round(lo, 4), "to": None if b == 4 else round(hi, 4),
                            "n": len(sel), "net": round(mean(rs), 4), "win": round(sum(1 for v in rs if v > 0) / len(rs), 3),
                            "x2": round(mean([rows[i]["x2"] for i in sel]), 3),
                            "rug": round(mean([rows[i]["rug"] for i in sel]), 3)})
        out.append({"feature": f, "n": len(idx),
                    "ic_ret60": spearman(xs, [rows[i]["ret_60m"] for i in idx]),
                    "ic_net": spearman(xs, [rets[i] for i in idx]),
                    "auc_x2": auc(xs, [rows[i]["x2"] for i in idx]),
                    "auc_rug": auc(xs, [rows[i]["rug"] for i in idx]),
                    "quintiles": buckets})
    out.sort(key=lambda d: -abs(d["ic_net"] or 0))
    return out


def exit_grid(rows, paths, side_cost, tx_cost, top=8):
    res = []
    for rule in EXIT_GRID:
        rs = trade_returns(rows, paths, rule, side_cost, tx_cost)
        res.append({"rule": dict(zip(("tp1_pct", "tp1_frac", "trail_pct", "stop_pct", "time_stop_min",
                                      "max_hold_min"), rule)),
                    "net": round(mean(rs), 4), "median": round(sorted(rs)[len(rs) // 2], 4),
                    "win": round(sum(1 for v in rs if v > 0) / len(rs), 3), "n": len(rs)})
    res.sort(key=lambda d: -d["net"])
    return res[:top]


def calibrate_risk(rows):
    """Rug and doubling rate per bucket of every bundle/insider feature, overall and among launches that
    pumped at least 30 % (a rug needs a pump first, so activity alone does not drown the signal). A threshold is
    suggested where the rug rate is at least 1.5x the base rate with enough launches above it."""
    base_rug = mean([r["rug"] for r in rows]) or 0.0
    pumped = [r for r in rows if (r.get("max_clean") or 0) >= 0.3]
    base_pumped = mean([r["rug"] for r in pumped]) or 0.0
    out = []
    for f in RISK_FEATURES:
        xs = [r.get(f) for r in rows if r.get(f) is not None]
        if len(xs) < 50 or len(set(xs)) < 3:
            continue
        cuts = sorted(set(round(v, 2) for v in quantiles(xs, 10)))
        steps = []
        for c in cuts:
            above = [r for r in rows if r.get(f) is not None and r[f] > c]
            above_p = [r for r in pumped if r.get(f) is not None and r[f] > c]
            if len(above) < 20:
                continue
            steps.append({"above": c, "n": len(above), "rug": round(mean([r["rug"] for r in above]), 3),
                          "x2": round(mean([r["x2"] for r in above]), 3),
                          "n_pumped": len(above_p),
                          "rug_pumped": round(mean([r["rug"] for r in above_p]), 3) if above_p else None})
        sug = next((s for s in steps if base_rug and s["rug"] >= 1.5 * base_rug and s["n"] >= 30), None)
        out.append({"feature": f, "base_rug": round(base_rug, 3), "base_rug_pumped": round(base_pumped, 3),
                    "steps": steps, "suggest_block_above": sug["above"] if sug else None,
                    "suggest_rug_lift": round(sug["rug"] / base_rug, 2) if sug and base_rug else None})
    return out


def _conds(rows, feats, k=6):
    """Candidate conditions (feature, op, threshold) at the deciles of each feature."""
    out = []
    for f in feats:
        xs = [r[f] for r in rows if r.get(f) is not None]
        if len(xs) < 50 or len(set(xs)) < 3:
            continue
        for c in sorted(set(round(v, 4) for v in quantiles(xs, k))):
            out.append((f, "<=", c))
            out.append((f, ">", c))
    return out


def _match(r, conds):
    for f, op, c in conds:
        v = r.get(f)
        if v is None or (v > c if op == "<=" else v <= c):
            return False
    return True


def _select(train, paths, cfg, min_n=30, top_singles=10):
    """Rank entry rules (one or two feature conditions, each with the best exits of the grid) on `train` by the
    bootstrap lower bound of their net result. Returns ([(ci_low, net, conditions, exit, n), ...], tested)."""
    side_cost, tx_cost = costs(cfg)
    ex = exit_grid(train, paths, side_cost, tx_cost, top=3)
    rules = [tuple(e["rule"].values()) for e in ex]
    cur = (cfg.tp1_pct, cfg.tp1_frac, cfg.trail_pct, cfg.stop_pct, cfg.time_stop_min, cfg.max_hold_min)
    if cur not in rules:
        rules.append(cur)
    ret_train = {rule: trade_returns(train, paths, rule, side_cost, tx_cost) for rule in rules}
    singles = []
    tested = 0
    for cond in _conds(train, FEATURES):
        sel = [i for i, r in enumerate(train) if _match(r, [cond])]
        if len(sel) < min_n:
            continue
        for rule in rules:
            tested += 1
            m = mean([ret_train[rule][i] for i in sel])
            singles.append((m, [cond], rule, len(sel)))
    singles.sort(key=lambda t: -t[0])
    cands = singles[:top_singles * 3]
    best_conds = []
    for _m, cs, _rule, _n in singles:
        if cs[0][0] not in [c[0] for c in best_conds]:
            best_conds.append(cs[0])
        if len(best_conds) >= top_singles:
            break
    for i, a in enumerate(best_conds):
        for b in best_conds[i + 1:]:
            sel = [j for j, r in enumerate(train) if _match(r, [a, b])]
            if len(sel) < min_n:
                continue
            for rule in rules:
                tested += 1
                cands.append((mean([ret_train[rule][j] for j in sel]), [a, b], rule, len(sel)))
    scored = []
    for m, cs, rule, n in cands:
        lo, _hi = boot_ci([ret_train[rule][j] for j, r in enumerate(train) if _match(r, cs)], n=300)
        scored.append((lo if lo is not None else -1, m, cs, rule, n))
    scored.sort(key=lambda t: -t[0])
    seen, uniq = set(), []
    for t in scored:   # one entry per condition set (its best exit)
        key = tuple(tuple(c) for c in t[2])
        if key not in seen:
            seen.add(key)
            uniq.append(t)
    return uniq, tested


def walk_forward(rows, paths, cfg, blocks=5, min_n=30, min_test=10):
    """The whole selection procedure, rerun on growing windows: pick the best rule on blocks [0, k), trade it on
    block k (k = 2 .. blocks-1). A procedure that only finds noise wins in some folds and loses in others."""
    side_cost, tx_cost = costs(cfg)
    size = len(rows) // blocks
    folds = []
    for k in range(2, blocks):
        train, test = rows[:k * size], rows[k * size:(k + 1) * size if k < blocks - 1 else len(rows)]
        uniq, _ = _select(train, paths, cfg, min_n=min_n)
        if not uniq:
            folds.append({"fold": k, "n": 0, "ok": False})
            continue
        lo, m, cs, rule, n = uniq[0]
        sel = [r for r in test if _match(r, cs)]
        rt = trade_returns(sel, paths, rule, side_cost, tx_cost) if sel else []
        med = sorted(rt)[len(rt) // 2] if rt else None
        ok = len(rt) >= min_test and mean(rt) > 0 and med > 0 and mean(rt) <= 1.0
        folds.append({"fold": k, "conditions": [list(c) for c in cs], "n": len(rt),
                      "net": round(mean(rt), 4) if rt else None, "median": round(med, 4) if med is not None else None,
                      "ok": ok})
    return folds


def rule_search(rows, paths, cfg, train_share=0.7, min_n=30, top_singles=10):
    """Entry rules (one or two feature conditions) chosen on the earlier part of the data with the best exit of
    the grid, then measured once on the later part. Accepted only if the later part is profitable after costs
    (bootstrap lower bound above zero, winning median and trimmed mean, plausible size, at least min_n trades)
    AND the selection procedure holds walk-forward: re-run on growing windows, its pick wins in at least two of
    three following blocks."""
    side_cost, tx_cost = costs(cfg)
    n_train = int(len(rows) * train_share)
    train, test = rows[:n_train], rows[n_train:]
    if len(train) < 100 or len(test) < min_n:
        return {"status": "zu wenig Daten", "n_train": len(train), "n_test": len(test), "accepted": None}
    uniq, tested = _select(train, paths, cfg, min_n, top_singles)
    results = []
    for lo, m, cs, rule, n in uniq[:5]:
        sel = [r for r in test if _match(r, cs)]
        rt = trade_returns(sel, paths, rule, side_cost, tx_cost) if sel else []
        tlo, thi = boot_ci(rt) if len(rt) >= 5 else (None, None)
        med = sorted(rt)[len(rt) // 2] if rt else None
        tm = trimmed_mean(rt) if rt else None
        plausible = bool(rt) and mean(rt) <= 1.0     # > +100 % per trade on average: a label error, not an edge
        # the median trade must win too: a mean carried by a few outliers is no rule to trade
        ok = (len(rt) >= min_n and tlo is not None and tlo > 0 and lo > 0 and med is not None and med > 0
              and tm is not None and tm > 0 and plausible)
        results.append({"conditions": [list(c) for c in cs],
                        "exit": dict(zip(("tp1_pct", "tp1_frac", "trail_pct", "stop_pct", "time_stop_min",
                                          "max_hold_min"), rule)),
                        "train": {"n": n, "net": round(m, 4), "ci_low": lo},
                        "test": {"n": len(rt), "net": round(mean(rt), 4) if rt else None, "ci_low": tlo, "ci_high": thi,
                                 "median": round(med, 4) if med is not None else None,
                                 "trimmed": round(tm, 4) if tm is not None else None, "plausible": plausible,
                                 "win": round(sum(1 for v in rt if v > 0) / len(rt), 3) if rt else None},
                        "accepted": ok})
    acc = next((r for r in results if r["accepted"]), None)
    folds = walk_forward(rows, paths, cfg, min_n=min_n) if acc else []
    wf_ok = sum(1 for f in folds if f["ok"]) >= 2
    if acc and not wf_ok:
        acc["accepted"] = False
        acc["rejected_by"] = "walk_forward"
        acc = None
    return {"status": "ok", "n_train": len(train), "n_test": len(test), "tested": tested,
            "split_ts": train[-1]["created_ts"], "candidates": results, "walk_forward": folds, "accepted": acc}


def signature_test(rows, paths, cfg, feat="block0_wallets", thr=3, blocks=5, min_test=5, conds=None):
    """A fixed entry condition (feat >= thr, or all of `conds` [(feature, min), ...]) instead of a searched one:
    the best exit is picked on older launches and traded on the next block (walk-forward), so the exit choice
    cannot see its own test data."""
    side_cost, tx_cost = costs(cfg)
    conds = conds or [(feat, thr)]
    sig = [r for r in rows if all((r.get(f) or 0) >= v for f, v in conds)]
    out = {"feature": feat, "min": thr, "conditions": conds, "n": len(sig),
           "measured": sum(1 for r in rows if all(r.get(f) is not None for f, _ in conds))}
    if not sig:
        return out
    out.update(rug=round(mean([r["rug"] for r in sig]), 3), x2=round(mean([r["x2"] for r in sig]), 3))
    size = len(rows) // blocks
    folds = []
    for k in range(2, blocks):
        lo, hi = rows[k * size]["created_ts"], (rows[(k + 1) * size]["created_ts"] if k < blocks - 1 else 9e18)
        train = [r for r in sig if r["created_ts"] < lo]
        test = [r for r in sig if lo <= r["created_ts"] < hi]
        if len(train) < min_test or len(test) < min_test:
            folds.append({"fold": k, "n": len(test), "ok": False})
            continue
        best = exit_grid(train, paths, side_cost, tx_cost, top=1)[0]
        rule = tuple(best["rule"][k2] for k2 in ("tp1_pct", "tp1_frac", "trail_pct", "stop_pct", "time_stop_min",
                                                 "max_hold_min"))
        rs = trade_returns(test, paths, rule, side_cost, tx_cost)
        folds.append({"fold": k, "n": len(rs), "exit": best["rule"], "train_net": best["net"],
                      "test_net": round(mean(rs), 4), "test_median": round(sorted(rs)[len(rs) // 2], 4),
                      "ok": mean(rs) > 0})
    out["folds"] = folds
    out["holds"] = sum(f["ok"] for f in folds) >= 2 and all(f.get("test_median", -1) > 0 for f in folds if f["ok"])
    return out


def run(conn, cfg=None, hours=0, save=False, now=None):
    from .config import SniperSettings
    cfg = cfg or SniperSettings()
    now = now or time.time()
    rows = load(conn, since=now - hours * 3600 if hours else None)
    side_cost, tx_cost = costs(cfg)
    paths = store.load_paths(conn, [r["mint"] for r in rows]) if rows else {}
    cur = (cfg.tp1_pct, cfg.tp1_frac, cfg.trail_pct, cfg.stop_pct, cfg.time_stop_min, cfg.max_hold_min)
    rets = trade_returns(rows, paths, cur, side_cost, tx_cost)
    rep = {"ts": int(now), "n": len(rows), "with_path": sum(1 for r in rows if r["mint"] in paths),
           "cost_round_trip": round(1 - (1 - side_cost) ** 2 + 2 * tx_cost, 4),
           "base": {"x2": round(mean([r["x2"] for r in rows]), 3) if rows else None,
                    "rug": round(mean([r["rug"] for r in rows]), 3) if rows else None,
                    "net_all_current_exit": round(mean(rets), 4) if rets else None}}
    if len(rows) >= 50:
        rep["features"] = feature_power(rows, rets)
        rep["exits"] = exit_grid(rows, paths, side_cost, tx_cost)
        rep["risk"] = calibrate_risk(rows)
        rep["rules"] = rule_search(rows, paths, cfg)
        rep["block0"] = signature_test(rows, paths, cfg)
        # candidate from the winners study: a running curve (MC >= 50 SOL) with >= 5 buyers at the decision
        rep["mc50_buyers5"] = signature_test(rows, paths, cfg, conds=[("mcap_decide_sol", 50), ("n_buyers", 5)])
    if save:
        store.set_state(conn, "research_report", json.dumps(rep))
        acc = (rep.get("rules") or {}).get("accepted")
        store.set_state(conn, "learned_rule", json.dumps({"ts": rep["ts"], **acc}) if acc else "")
    return rep


def learned_rule(conn, max_age_h=24, now=None):
    """The accepted rule from the last research run, if fresh; None otherwise."""
    raw = store.get_state(conn, "learned_rule")
    if not raw:
        return None
    try:
        r = json.loads(raw)
    except ValueError:
        return None
    if not r.get("accepted") or (now or time.time()) - r.get("ts", 0) > max_age_h * 3600:
        return None
    return r


def matches(rule, feats):
    """Does a launch (dict of stage-1 features) match the learned rule's conditions?"""
    r = dict(feats)
    for k, fn in DERIVED.items():
        if k not in r:
            try:
                r[k] = fn(r)
            except Exception:  # noqa: BLE001
                r[k] = None
    return _match(r, [tuple(c) for c in rule.get("conditions") or []])


def summary(rep, top=8):
    """German text report for the CLI and the log."""
    L = [f"Sniper-Forschung: {rep['n']} Launches mit Trades und Ausgang, {rep['with_path']} mit Preisverlauf "
         f"(Rest: nur die Stände nach 1/5/15/60 min). Kosten je Runde {100 * rep['cost_round_trip']:.1f} %."]
    b = rep["base"]
    if b.get("net_all_current_exit") is not None:
        L.append(f"Alle kaufen mit den aktuellen Ausstiegen: {100 * b['net_all_current_exit']:+.1f} % je Trade, "
                 f"Verdopplung {100 * b['x2']:.0f} %, Rug {100 * b['rug']:.0f} %.")
    if rep.get("features"):
        L.append("\nMerkmale (IC = Rangkorrelation mit dem Netto-Ergebnis, AUC 0,5 = keine Information):")
        for f in rep["features"][:top]:
            q = f["quintiles"]
            L.append(f"  {f['feature']:<22} IC {f['ic_net']:+.2f}  AUC 2x {f['auc_x2']}  AUC Rug {f['auc_rug']}  "
                     f"Netto unterstes/oberstes Fünftel {100 * q[0]['net']:+.1f} % / {100 * q[-1]['net']:+.1f} %")
    if rep.get("exits"):
        e = rep["exits"][0]
        L.append(f"\nBester Ausstieg für alle: {e['rule']} -> {100 * e['net']:+.1f} % je Trade "
                 f"(Gewinnquote {100 * e['win']:.0f} %).")
    for r in (rep.get("risk") or []):
        if r["suggest_block_above"] is not None:
            L.append(f"Risiko {r['feature']}: über {r['suggest_block_above']} Rug-Rate {r['suggest_rug_lift']}x der Basis")
    rs = rep.get("rules") or {}
    if rs.get("status") == "ok":
        L.append(f"\nRegelsuche: {rs['tested']} Varianten auf {rs['n_train']} älteren Launches, geprüft auf "
                 f"{rs['n_test']} neueren.")
        for c in rs["candidates"][:3]:
            cond = " und ".join(f"{f} {op} {v}" for f, op, v in c["conditions"])
            t = c["test"]
            L.append(f"  {cond}: Lernteil {100 * c['train']['net']:+.1f} % (n {c['train']['n']}), Testteil "
                     + (f"{100 * t['net']:+.1f} % (n {t['n']}, untere Grenze {100 * (t['ci_low'] or 0):+.1f} %)"
                        if t["net"] is not None else "keine Treffer")
                     + ("  ANGENOMMEN" if c["accepted"] else ""))
        wf = rs.get("walk_forward") or []
        if wf:
            L.append("Walk-forward: " + ", ".join(
                f"Block {f['fold']}: {'+' if (f.get('net') or 0) >= 0 else ''}{100 * (f.get('net') or 0):.1f} % (n {f['n']})"
                for f in wf) + f" -> {sum(1 for f in wf if f['ok'])} von {len(wf)} im Plus")
        if not rs.get("accepted"):
            L.append("Keine Regel hält auf ungesehenen Daten nach Kosten: der Sniper kauft weiter nur nach seinen "
                     "festen Regeln.")
    elif rs:
        L.append(f"\nRegelsuche: {rs.get('status')}.")
    for key, title in (("block0", "Block-0-Signatur (>= 3 Mini-Käufe in 5 s)"),
                       ("mc50_buyers5", "MC >= 50 SOL und >= 5 Käufer beim Entscheid")):
        b0 = rep.get(key)
        if not b0:
            continue
        L.append(f"\n{title}: {b0['n']} von {b0['measured']} gemessenen "
                 f"Launches" + (f", Rug {100 * b0['rug']:.0f} %, Verdopplung {100 * b0['x2']:.0f} %" if b0["n"] else ""))
        for f in b0.get("folds") or []:
            L.append(f"  Block {f['fold']}: " + (f"Test {100 * f['test_net']:+.1f} % (Median {100 * f['test_median']:+.1f} %, "
                     f"n {f['n']}), Ausstieg aus älteren Daten" if "test_net" in f else f"zu wenig Daten (n {f['n']})"))
        if b0.get("folds"):
            L.append("  -> trägt" if b0["holds"] else "  -> trägt nicht (oder noch zu wenig Daten): keine eigene Regel")
    return "\n".join(L)
