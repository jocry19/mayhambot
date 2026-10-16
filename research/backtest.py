"""Walk-forward backtest without look-ahead.

Timeline is cut into checkpoints C_0 < C_1 < ... For the window [C_k, C_k+1):
  1. wallet stats are computed only from trades/prices before C_k
  2. wallet component weights are learned only from wallet buys whose outcome was known before C_k
  3. tiers are assigned with those weights
  4. a coin event is created whenever a tiered wallet buys (deduped per coin)
  5. coin models are trained only on earlier events whose horizon ended before C_k, then predict the window
"""
import bisect
import logging
import random

from model import learn
from model.config import msettings
from model.tiers import tier_all
from research import events as ev
from research import stats, wallet_stats

log = logging.getLogger("research.backtest")


def checkpoints(ts_list, max_points=24, min_gap=1800):
    """Checkpoints at quantiles of the trade timestamps (from the 35 % to the 97 % quantile), so they follow
    where the data actually is (wallet backfills reach back months, live data is dense at the end)."""
    if len(ts_list) < 50 or ts_list[-1] - ts_list[0] < 2 * 3600:
        return []
    n = len(ts_list)
    pts = []
    for i in range(max_points):
        q = 0.35 + (0.97 - 0.35) * i / max(1, max_points - 1)
        t = ts_list[min(n - 1, int(q * n))]
        if not pts or t - pts[-1] >= min_gap:
            pts.append(t)
    return pts


def _wallet_events(ds, stats_k, a, b, h_name, horizons, limit=4000, rng=None):
    out = []
    buys = [x for x in wallet_stats.first_buys(ds, a, b) if x[0] in stats_k and stats_k[x[0]]["n_tokens"] >= 3]
    rng = rng or random.Random(7)
    if len(buys) > limit:
        buys = rng.sample(buys, limit)
    hz = [h for h in horizons if h[0] == h_name]
    for w, mint, ts, px in buys:
        lab = ev.labels_at(mint, ts, ds.pidx, hz).get(h_name)
        if lab is None:
            continue
        out.append(dict(ts=ts, sec=hz[0][1], comps=stats_k[w]["components"], y=lab["pumped"]))
    return out


def _coin_events(ds, tmap, a, b, ms):
    out = []
    last = {}
    W = ms.window_min * 60
    for t in ds.trades[bisect.bisect_left(ds.ts_list, a):]:
        if t.ts >= b:
            break
        if t.side != "buy":
            continue
        ti = tmap.get(t.wallet)
        if not ti or not (ti[0] or ti[1] >= ms.event_min_score):
            continue
        if t.ts - last.get(t.mint, -10 ** 12) < ms.event_dedupe_min * 60:
            continue
        f, wallets = ev.features_at(t.mint, t.ts, ds.window(t.mint, t.ts - W, t.ts), tmap, ds.pidx,
                                    ds.created.get(t.mint), ds.to_sol, ms.window_min,
                                    history=ds.window(t.mint, 0, t.ts))
        if f is None:
            continue
        last[t.mint] = t.ts
        out.append(dict(mint=t.mint, ts=t.ts, f=f, x=ev.vector(f), labels=ev.labels_at(t.mint, t.ts, ds.pidx, ms.horizons),
                        baseline=ev.baseline_score(f), trigger_tier=ti[0] or "scored", wallets=len(wallets)))
    return out


def run(ds, ms=msettings, max_points=24):
    cps = checkpoints(ds.ts_list, max_points)
    res = dict(checkpoints=cps, events=[], wallet_events=[], preds={h[0]: [] for h in ms.horizons},
               weights=[], tier_counts=[], tier_forward={})
    if not cps:
        log.info("not enough history for a walk-forward backtest (%.1f h)", (ds.end - ds.start) / 3600)
        return res
    h_wallet = "1h" if any(h[0] == "1h" for h in ms.horizons) else ms.horizons[0][0]
    bounds = cps + [ds.end + 1]
    for k, c in enumerate(cps):
        nxt = bounds[k + 1]
        st, _ = wallet_stats.compute(ds, c)
        known = [(we["comps"], we["y"]) for we in res["wallet_events"] if we["ts"] + we["sec"] <= c]
        weights, winfo = learn.learn_wallet_weights(known)
        tiers = tier_all(st, weights)
        keep = [w for w, (t, sc, s) in tiers.items() if t or sc >= ms.event_min_score]
        cl = wallet_stats.clusters(ds, keep, c)
        tmap = {w: (tiers[w][0], tiers[w][1], tiers[w][2]["influence_usd"] or 0.0, cl.get(w), tiers[w][2].get("exit_capture"))
                for w in keep}
        counts = {t: sum(1 for v in tiers.values() if v[0] == t) for t in ("ultra", "smart", "good")}
        res["weights"].append(dict(ts=c, weights=weights, info=winfo))
        res["tier_counts"].append(dict(ts=c, **counts, wallets=len(st)))
        # forward check of tiers: how often a tiered wallet's next buys pumped
        wev = _wallet_events(ds, st, c, nxt, h_wallet, ms.horizons)
        res["wallet_events"].extend(wev)
        for x in wallet_stats.first_buys(ds, c, nxt):
            tier = (tmap.get(x[0]) or (None,))[0] or "none"
            if x[0] not in st:
                continue
            lab = ev.labels_at(x[1], x[2], ds.pidx, [h for h in ms.horizons if h[0] == h_wallet]).get(h_wallet)
            if lab is None:
                continue
            d = res["tier_forward"].setdefault(tier, [0, 0, []])
            d[0] += 1
            d[1] += lab["pumped"]
            d[2].append(lab["max_ret"])
        cev = _coin_events(ds, tmap, c, nxt, ms)
        train = res["events"]
        for name, sec, _ in ms.horizons:
            hz = [h for h in ms.horizons if h[0] == name]
            m = learn.fit_token_models(train, hz, now=c)[name]
            mn = learn.fit_token_models(train, hz, now=c, target="win")[name]
            for e in cev:
                lab = e["labels"].get(name)
                if lab is None:
                    continue
                p = stats.logistic_predict(m, e["x"])
                p_net = stats.logistic_predict(mn, e["x"]) if mn["kind"] != "prior" else None
                res["preds"][name].append(dict(ts=e["ts"], p=p, y=lab["pumped"], max_ret=lab["max_ret"],
                                               net=lab.get("net"), win=lab.get("win"), p_net=p_net,
                                               baseline=e["baseline"], model=m["kind"]))
        res["events"].extend(cev)
        log.info("checkpoint %d/%d: %d wallets, tiers %s, %d coin events", k + 1, len(cps), len(st), counts, len(cev))
    res["metrics"] = metrics(res, ms)
    return res


def calibration(ps, ys, bins=5):
    """Equal-count bins of the predicted probability: mean prediction vs observed rate, plus the expected
    calibration error (weighted mean gap). A well calibrated model has mean_p close to rate in every bin."""
    pairs = sorted(zip(ps, ys))
    out, ece = [], 0.0
    for i in range(bins):
        part = pairs[i * len(pairs) // bins:(i + 1) * len(pairs) // bins]
        if not part:
            continue
        mp = sum(p for p, _ in part) / len(part)
        rate = sum(y for _, y in part) / len(part)
        ece += len(part) / len(pairs) * abs(mp - rate)
        out.append(dict(bin=i + 1, n=len(part), mean_p=mp, rate=rate))
    return dict(bins=out, ece=ece)


def net_metrics(P):
    """After-cost view: mean net return of all events, of the top fifth by pump score, and (once trained) of the
    top fifth by the after-cost model, with that model's AUC on 'profit after costs'."""
    rows = [p for p in P if p.get("net") is not None]
    if not rows:
        return {}

    def mean(xs):
        return sum(xs) / len(xs) if xs else None
    k = max(1, len(rows) // 5)
    out = dict(n=len(rows), mean_net_all=mean([p["net"] for p in rows]),
               win_rate_all=mean([float(p["net"] > 0) for p in rows]),
               mean_net_top_pump=mean([p["net"] for p in sorted(rows, key=lambda p: -p["p"])[:k]]))
    nm = [p for p in rows if p.get("p_net") is not None]
    if len(nm) >= 20 and 0 < sum(p["win"] for p in nm) < len(nm):
        kk = max(1, len(nm) // 5)
        top = sorted(nm, key=lambda p: -p["p_net"])[:kk]
        out.update(n_net_model=len(nm), auc_net=stats.auc([p["p_net"] for p in nm], [p["win"] for p in nm]),
                   mean_net_top_net=mean([p["net"] for p in top]), win_rate_top_net=mean([float(p["net"] > 0) for p in top]),
                   calibration_net=calibration([p["p_net"] for p in nm], [p["win"] for p in nm]))
    return out


def metrics(res, ms):
    out = {}
    for name, sec, thr in ms.horizons:
        P = res["preds"][name]
        if not P:
            out[name] = dict(n=0)
            continue
        y = [p["y"] for p in P]
        ps = [p["p"] for p in P]
        base = sum(y) / len(y)
        srt = sorted(P, key=lambda p: -p["p"])
        top = srt[:max(1, len(srt) // 10)]
        q = []
        for i in range(5):
            part = srt[i * len(srt) // 5:(i + 1) * len(srt) // 5]
            if part:
                nets = [p["net"] for p in part if p.get("net") is not None]
                q.append(dict(quintile=i + 1, n=len(part), pump_rate=sum(p["y"] for p in part) / len(part),
                              median_max_ret=stats.median([p["max_ret"] for p in part]),
                              mean_net=sum(nets) / len(nets) if nets else None, median_net=stats.median(nets),
                              win_rate=sum(1 for x in nets if x > 0) / len(nets) if nets else None))
        out[name] = dict(
            n=len(P), base_rate=base, auc=stats.auc(ps, y), auc_baseline=stats.auc([p["baseline"] for p in P], y),
            brier=sum((a - b) ** 2 for a, b in zip(ps, y)) / len(P),
            top10_pump_rate=sum(p["y"] for p in top) / len(top), top10_n=len(top),
            top10_median_max_ret=stats.median([p["max_ret"] for p in top]),
            precision_p50=(lambda s: (sum(p["y"] for p in s) / len(s), len(s)) if s else (None, 0))(
                [p for p in P if p["p"] >= 0.5]),
            quintiles=q, learned_share=sum(1 for p in P if p["model"] != "prior") / len(P),
            calibration=calibration(ps, y), net=net_metrics(P))
    tf = {}
    for tier, (n, k, mr) in res["tier_forward"].items():
        r, lo, hi = stats.wilson(k, n)
        tf[tier] = dict(n=n, pump_rate=r, ci_low=lo, ci_high=hi, median_max_ret=stats.median(mr))
    out["tier_forward"] = tf
    return out
