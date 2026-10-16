"""Replay the signal rules over stored trades and measure what each threshold set would have earned after costs.

    .venv/bin/python -m signals.backtest --hours 48            # grid over wallets / net / score / model p
    .venv/bin/python -m signals.backtest --db data/snapshot.db  # against a copy (never needs the live DB)

Every STEP seconds and for every coin with an Ultra/Smart buy in the last SIGNAL_WINDOW_MIN, it computes the same
inputs as signals.engine.token_metrics: distinct smart entities with a positive net, net smart USD and the heuristic
score. Each rule set fires with the live cooldown. Entry is the first price within 5 min after the signal. The result
is ret_1h, max_ret_1h and a +TP take-profit exit, each after COST.
Limits: tiers are today's tiers (a wallet may be smart partly because of these very coins), so absolute numbers are
optimistic; comparing rule sets with each other is fair. Model p comes from model_predictions stored live (no lookahead).
"""
import argparse
import bisect
import itertools
import math
import sqlite3
import time
from collections import defaultdict

from core import db
from core.config import settings

COST = 0.06
STEP = 300
TP = 0.5


def _has(conn, table):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def tier_versions(conn):
    """[(version_ts, {wallet: (tier, score)})] from model_tier_history (point in time), oldest first. Falls back to
    today's wallets.tier as one version at ts 0 (pit=False: tiers may know the future)."""
    out = []
    if _has(conn, "model_versions") and _has(conn, "model_tier_history"):
        for vid, vts in conn.execute("SELECT id, ts FROM model_versions ORDER BY ts").fetchall():
            t = {r[0]: (r[1], r[2] or 0.0) for r in conn.execute(
                "SELECT address, tier, score FROM model_tier_history WHERE version=? AND tier IN ('ultra','smart')", (vid,))}
            if t:
                out.append((vts, t))
    if out:
        return out, True
    return [(0, {r[0]: (r[1], r[2] or 0.0) for r in conn.execute(
        "SELECT address, tier, score FROM wallets WHERE tier IN ('ultra','smart') AND COALESCE(is_bot,0)=0")})], False


def tiers_for(versions, t):
    """The tier map of the newest version trained at or before t (the first one before any version existed)."""
    i = bisect.bisect_right([v[0] for v in versions], t) - 1
    return versions[max(i, 0)][1]


def load(conn, since, until, window):
    versions, pit = tier_versions(conn)
    clusters = {r[0]: r[1] for r in conn.execute("SELECT address, cluster_id FROM wallets WHERE cluster_id IS NOT NULL")}
    ever = sorted({w for _, t in versions for w in t})
    smart = defaultdict(list)  # mint -> [(ts, wallet, side, usd)] sorted
    for i in range(0, len(ever), 500):
        part = ever[i:i + 500]
        q = ",".join("?" * len(part))
        for r in conn.execute(f"SELECT mint, ts, wallet, side, usd_amount FROM trades WHERE ts>=? AND ts<=? "
                              f"AND wallet IN ({q})", (since - window, until, *part)):
            smart[r[0]].append((r[1], r[2], r[3], r[4] or 0.0))
    for m in smart:
        smart[m].sort()
    prices = defaultdict(list)
    mints = list(smart)
    for i in range(0, len(mints), 400):
        part = mints[i:i + 400]
        qm = ",".join("?" * len(part))
        for r in conn.execute(
                f"""SELECT mint, ts, price_usd FROM trades WHERE mint IN ({qm}) AND ts>=? AND ts<=? AND price_usd>0
                    UNION ALL SELECT mint, ts, price_usd FROM token_prices WHERE mint IN ({qm}) AND ts>=? AND ts<=?
                    AND price_usd>0""", (*part, since, until + 3700, *part, since, until + 3700)):
            prices[r[0]].append((r[1], r[2]))
    for m in prices:
        prices[m].sort()
    watched = defaultdict(list)
    for i in range(0, len(mints), 400):
        part = mints[i:i + 400]
        for r in conn.execute(f"SELECT mint, ts, price_usd FROM token_prices WHERE mint IN ({','.join('?' * len(part))}) "
                              "AND ts>=? AND ts<=? AND price_usd>0", (*part, since - 3600, until + 3700)):
            watched[r[0]].append((r[1], r[2]))
    for m in watched:
        watched[m].sort()
    preds = defaultdict(list)
    if _has(conn, "model_predictions"):
        for r in conn.execute("SELECT mint, ts, p_1h FROM model_predictions WHERE ts>=? AND ts<=? AND p_1h IS NOT NULL "
                              "ORDER BY ts", (since - 900, until)):
            preds[r[0]].append((r[1], r[2]))
    return dict(versions=versions, pit=pit, clusters=clusters, smart=smart, prices=prices, preds=preds, watched=watched)


def metrics_at(trades, tiers, clusters, t, window, cfg=settings):
    """Same inputs as engine.token_metrics, with the tiers known at t, plus timing features for later analysis."""
    lo = bisect.bisect_left(trades, (t - window,))
    hi = bisect.bisect_right(trades, (t, "\uffff"))
    per = {}
    for ts, w, side, usd in trades[lo:hi]:
        if w not in tiers:
            continue
        d = per.setdefault(w, [0.0, 0.0, ts])
        d[0 if side == "buy" else 1] += usd
    buy = sum(d[0] for d in per.values())
    net = buy - sum(d[1] for d in per.values())
    buyers = [w for w, d in per.items() if d[0] - d[1] > 0]
    ents = {clusters.get(w) or w for w in buyers}
    wsum = sum(per[w][0] for w in buyers)
    avg_q = sum(tiers[w][1] * per[w][0] for w in buyers) / wsum if wsum else 0.0
    score = 0.0
    if net > 0 and buyers:
        score = 100 * (0.4 * (1 - math.exp(-net / cfg.inflow_ref_usd))
                       + 0.3 * (1 - math.exp(-len(ents) / cfg.wallets_ref_count)) + 0.3 * avg_q / 100)
    firsts = sorted(per[w][2] for w in buyers)
    return dict(net=net, entities=len(ents), score=round(score, 1),
                sellers=sum(1 for d in per.values() if d[1] > d[0]),
                spread_s=(firsts[-1] - firsts[0]) if len(firsts) > 1 else None,
                ultra=any(tiers[w][0] == "ultra" for w in buyers))


DEAD_RET = -0.95  # a coin whose pool died and stopped trading


def price_near(pts, t, tol):
    """Price at t: the last point in [t - tol, t], else the first in (t, t + tol]. None if nothing that close."""
    best = None
    for ts, p in pts:
        if ts > t + tol:
            break
        if ts <= t:
            if ts >= t - tol:
                best = p
        elif best is None:
            return p
    return best


def outcome(series, t, tp=TP, watched=None):
    """Entry: first price within 5 min after t. ret_1h: price within 5 min of t + 1h (never a later print).
    A watched coin (DexScreener snapshots right before t) that has no price at all after t + 30 min counts as dead
    (DEAD_RET) instead of silently dropping out; an unwatched coin without prices is not measurable (None)."""
    i = bisect.bisect_left(series, (t,))
    if i >= len(series) or series[i][0] > t + 300:
        return None
    p0 = series[i][1]
    path = [p for ts, p in series[i:] if ts <= t + 3600]
    mx = max(path) / p0 - 1
    p1 = price_near(series[i:], t + 3600, 300)
    if p1 is None:
        was_watched = bool(watched) and price_near(watched, t, 1800) is not None
        if was_watched and not any(ts > t + 1800 for ts, _ in series[i:]):
            r1 = DEAD_RET
        else:
            return None
    else:
        r1 = p1 / p0 - 1
    tp_exit = tp if mx >= tp else r1
    return dict(ret_1h=r1, max_ret_1h=mx, tp_ret=tp_exit, dead=r1 == DEAD_RET)


def p_at(pr, t):
    i = bisect.bisect_right(pr, (t, 2.0)) - 1
    return pr[i][1] if i >= 0 and pr[i][0] >= t - 900 else None


DEFAULT_GRID = dict(wallets=[1, 2, 3], net=[100, 200, 300, 500], score=[0, 30, 45, 60], p=[None, 0.15, 0.25],
                    sellers=[None, 0])


def snapshots(data, since, until, window, cfg=settings):
    snaps = []  # (t, mint, metrics, model p)
    for mint, tr in data["smart"].items():
        ts_set = sorted({(ts // STEP + 1) * STEP for ts, w, side, _ in tr if side == "buy" and since <= ts <= until})
        for t in ts_set:
            m = metrics_at(tr, tiers_for(data["versions"], t), data["clusters"], t, window, cfg)
            if m["entities"] >= 1:
                snaps.append((t, mint, m, p_at(data["preds"].get(mint, []), t)))
    snaps.sort(key=lambda s: s[0])
    return snaps


def _keep(m, pm, c):
    return not (m["entities"] < c["wallets"] or m["net"] < c["net"] or m["score"] < c["score"]
                or (c["p"] is not None and (pm is None or pm < c["p"]))
                or (c["sellers"] is not None and m["sellers"] > c["sellers"]))


def replay(snaps, prices, c, cfg=settings, watched=None):
    last, res = {}, []
    for t, mint, m, pm in snaps:
        if not _keep(m, pm, c):
            continue
        if mint in last and t - last[mint] < cfg.signal_cooldown_min * 60:
            continue
        last[mint] = t
        o = outcome(prices.get(mint, []), t, watched=(watched or {}).get(mint))
        if o:
            res.append(dict(o, t=t, mint=mint, p=pm, **m))
    return res


def summary(res, cost=COST):
    k = len(res)
    if not k:
        return dict(n=0, sum_tp=0.0)
    tp = sorted(o["tp_ret"] - cost for o in res)
    return dict(n=k, hit_50=round(sum(1 for o in res if o["max_ret_1h"] >= 0.5) / k, 3),
                mean_ret_1h=round(sum(o["ret_1h"] for o in res) / k - cost, 4),
                mean_tp=round(sum(tp) / k, 4), median_tp=round(tp[k // 2], 4), sum_tp=round(sum(tp), 3),
                win_rate=round(sum(1 for x in tp if x > 0) / k, 3), dead=sum(1 for o in res if o.get("dead")))


def configs(grid):
    keys = list(grid)
    return [dict(zip(keys, v)) for v in itertools.product(*(grid[k] for k in keys))]


def walk_forward(snaps, prices, grid, folds=4, min_n=5, cfg=settings, cost=COST, watched=None):
    """Split the snapshots in time into `folds` equal parts. For fold k >= 1 the rule set with the best mean TP
    return (at least min_n signals) on folds < k is chosen and judged on fold k only. The out-of-sample sum is
    the honest estimate; the in-sample winner over everything is shown next to it as the overfit reference."""
    if not snaps:
        return dict(folds=[], oos=summary([]), chosen=[])
    t0, t1 = snaps[0][0], snaps[-1][0] + 1
    edges = [t0 + (t1 - t0) * i // folds for i in range(folds + 1)]
    cs = configs(grid)
    oos, chosen, fold_rows = [], [], []
    for k in range(1, folds):
        train = [s for s in snaps if s[0] < edges[k]]
        test = [s for s in snaps if edges[k] <= s[0] < edges[k + 1]]
        best, best_v = None, None
        for c in cs:
            sm = summary(replay(train, prices, c, cfg, watched), cost)
            if sm["n"] >= min_n and (best_v is None or sm["mean_tp"] > best_v):
                best, best_v = c, sm["mean_tp"]
        if best is None:
            fold_rows.append(dict(fold=k, chosen=None))
            continue
        r = replay(test, prices, best, cfg, watched)
        oos += r
        chosen.append(best)
        fold_rows.append(dict(fold=k, chosen=best, train_mean_tp=best_v, test=summary(r, cost)))
    return dict(folds=fold_rows, oos=summary(oos, cost), chosen=chosen)


def run(conn, hours=48, now=None, grid=None, cfg=settings, cost=COST, folds=4, min_n=5):
    now = now or int(time.time())
    window = cfg.signal_window_min * 60
    since = now - int(hours * 3600)
    until = now - 3600  # outcomes need one more hour
    data = load(conn, since, until, window)
    grid = grid or DEFAULT_GRID
    snaps = snapshots(data, since, until, window, cfg)
    out = []
    for c in configs(grid):
        out.append(dict(c, **summary(replay(snaps, data["prices"], c, cfg, data["watched"]), cost)))
    live = dict(wallets=cfg.signal_min_wallets, net=cfg.signal_min_net_usd, score=0, p=None, sellers=None)
    wf = walk_forward(snaps, data["prices"], grid, folds, min_n, cfg, cost, data["watched"]) if folds > 1 else None
    return dict(hours=hours, snapshots=len(snaps), coins=len(data["smart"]), pit=data["pit"],
                versions=len(data["versions"]), results=out, walk_forward=wf,
                live_like=dict(live, **summary(replay(snaps, data["prices"], live, cfg, data["watched"]), cost)))


def main():
    ap = argparse.ArgumentParser(description="replay the signal rules on stored trades")
    ap.add_argument("--hours", type=float, default=48)
    ap.add_argument("--db", default=None, help="path to a DB copy (default: the configured DB)")
    ap.add_argument("--min-n", type=int, default=5)
    ap.add_argument("--folds", type=int, default=4)
    a = ap.parse_args()
    conn = db.connect(a.db) if a.db else db.connect()
    r = run(conn, a.hours, folds=a.folds, min_n=a.min_n)
    print(f"{r['coins']} coins with smart trades, {r['snapshots']} snapshots, tiers point-in-time: {r['pit']} "
          f"({r['versions']} model versions)")
    print("live-like rules:", r["live_like"])
    if r["walk_forward"]:
        print("walk-forward out-of-sample:", r["walk_forward"]["oos"])
        for f in r["walk_forward"]["folds"]:
            print("  fold", f)
    print("in-sample top 15 (optimistic, for orientation only):")
    rows = sorted((x for x in r["results"] if x["n"] >= a.min_n), key=lambda x: -x["mean_tp"])
    for x in rows[:15]:
        print("  ", x)


if __name__ == "__main__":
    main()
