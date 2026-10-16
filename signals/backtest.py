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


def load(conn, since, until, window):
    tiers = {r[0]: (r[1], r[2] or 0.0, r[3]) for r in conn.execute(
        "SELECT address, tier, score, cluster_id FROM wallets WHERE tier IN ('ultra','smart') AND COALESCE(is_bot,0)=0")}
    smart = defaultdict(list)  # mint -> [(ts, wallet, side, usd)] sorted
    q = ",".join("?" * len(tiers)) or "''"
    for r in conn.execute(f"SELECT mint, ts, wallet, side, usd_amount FROM trades WHERE ts>=? AND ts<=? "
                          f"AND wallet IN ({q}) ORDER BY ts", (since - window, until, *tiers)):
        smart[r[0]].append((r[1], r[2], r[3], r[4] or 0.0))
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
    preds = defaultdict(list)
    try:
        for r in conn.execute("SELECT mint, ts, p_1h FROM model_predictions WHERE ts>=? AND ts<=? AND p_1h IS NOT NULL "
                              "ORDER BY ts", (since - 900, until)):
            preds[r[0]].append((r[1], r[2]))
    except sqlite3.OperationalError:  # no model tables in this DB
        pass
    return tiers, smart, prices, preds


def metrics_at(trades, tiers, t, window, cfg=settings):
    lo = bisect.bisect_left(trades, (t - window,))
    hi = bisect.bisect_right(trades, (t, "￿"))
    per = {}
    for ts, w, side, usd in trades[lo:hi]:
        d = per.setdefault(w, [0.0, 0.0])
        d[0 if side == "buy" else 1] += usd
    buy = sum(d[0] for d in per.values())
    net = buy - sum(d[1] for d in per.values())
    buyers = [w for w, d in per.items() if d[0] - d[1] > 0]
    ents = {tiers[w][2] or w for w in buyers}
    wsum = sum(per[w][0] for w in buyers)
    avg_q = sum(tiers[w][1] * per[w][0] for w in buyers) / wsum if wsum else 0.0
    score = 0.0
    if net > 0 and buyers:
        score = 100 * (0.4 * (1 - math.exp(-net / cfg.inflow_ref_usd))
                       + 0.3 * (1 - math.exp(-len(ents) / cfg.wallets_ref_count)) + 0.3 * avg_q / 100)
    return dict(net=net, entities=len(ents), score=round(score, 1))


def outcome(series, t, tp=TP):
    i = bisect.bisect_left(series, (t,))
    if i >= len(series) or series[i][0] > t + 300:
        return None
    p0 = series[i][1]
    path = [p for ts, p in series[i:] if ts <= t + 3600]
    if not path or series[-1][0] < t + 3000:  # no price near the 1h mark: not measurable
        return None
    r1 = path[-1] / p0 - 1
    mx = max(path) / p0 - 1
    tp_exit = tp if mx >= tp else r1
    return dict(ret_1h=r1, max_ret_1h=mx, tp_ret=tp_exit)


def p_at(pr, t):
    i = bisect.bisect_right(pr, (t, 2.0)) - 1
    return pr[i][1] if i >= 0 and pr[i][0] >= t - 900 else None


def run(conn, hours=48, now=None, grid=None, cfg=settings, cost=COST):
    now = now or int(time.time())
    window = cfg.signal_window_min * 60
    since = now - int(hours * 3600)
    until = now - 3600  # outcomes need one more hour
    tiers, smart, prices, preds = load(conn, since, until, window)
    grid = grid or dict(wallets=[1, 2, 3], net=[100, 200, 300, 500], score=[0, 30, 40, 50, 60], p=[None, 0.15, 0.25])
    snaps = []  # (t, mint, metrics, model p)
    for mint, tr in smart.items():
        ts_set = sorted({(ts // STEP + 1) * STEP for ts, w, side, _ in tr if side == "buy" and since <= ts <= until})
        for t in ts_set:
            m = metrics_at(tr, tiers, t, window, cfg)
            if m["entities"] >= 1:
                snaps.append((t, mint, m, p_at(preds.get(mint, []), t)))
    snaps.sort(key=lambda s: s[0])
    out = []
    for w, n, s, p in itertools.product(grid["wallets"], grid["net"], grid["score"], grid["p"]):
        last, res = {}, []
        for t, mint, m, pm in snaps:
            if m["entities"] < w or m["net"] < n or m["score"] < s or (p is not None and (pm is None or pm < p)):
                continue
            if mint in last and t - last[mint] < cfg.signal_cooldown_min * 60:
                continue
            last[mint] = t
            o = outcome(prices.get(mint, []), t)
            if o:
                res.append(o)
        if not res:
            out.append(dict(wallets=w, net=n, score=s, p=p, n=0))
            continue
        k = len(res)
        out.append(dict(wallets=w, net=n, score=s, p=p, n=k,
                        hit_50=round(sum(1 for o in res if o["max_ret_1h"] >= 0.5) / k, 3),
                        mean_ret_1h=round(sum(o["ret_1h"] for o in res) / k - cost, 4),
                        mean_tp=round(sum(o["tp_ret"] for o in res) / k - cost, 4),
                        median_tp=round(sorted(o["tp_ret"] for o in res)[k // 2] - cost, 4),
                        sum_tp=round(sum(o["tp_ret"] for o in res) - cost * k, 3)))
    return dict(hours=hours, snapshots=len(snaps), coins=len(smart), smart_wallets=len(tiers), results=out)


def main():
    ap = argparse.ArgumentParser(description="replay the signal rules on stored trades")
    ap.add_argument("--hours", type=float, default=48)
    ap.add_argument("--db", default=None, help="path to a DB copy (default: the configured DB)")
    ap.add_argument("--min-n", type=int, default=5)
    a = ap.parse_args()
    conn = db.connect(a.db) if a.db else db.connect()
    r = run(conn, a.hours)
    print(f"{r['coins']} coins with smart trades, {r['smart_wallets']} smart wallets, {r['snapshots']} snapshots")
    rows = sorted((x for x in r["results"] if x["n"] >= a.min_n), key=lambda x: -x["sum_tp"])
    for x in rows[:40]:
        print(x)


if __name__ == "__main__":
    main()
