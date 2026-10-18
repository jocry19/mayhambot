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
import time

from model import learn
from model.config import msettings
from model.tiers import tier_all, wallet_score
from research import events as ev
from research import exits, stats, wallet_stats

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
        if ts < getattr(ds, "hindsight", {}).get(mint, 0):
            continue  # the wallet was found as an early buyer of this coin because it pumped
        lab = ev.labels_at(mint, ts, ds.pidx, hz).get(h_name)
        if lab is None:
            continue
        out.append(dict(ts=ts, sec=hz[0][1], comps=stats_k[w]["components"], n_tokens=stats_k[w]["n_tokens"],
                        y=lab["pumped"], win=lab["win"], net=lab["net"], mint=mint))
    by = {}
    for we in out:
        by.setdefault(we["mint"], []).append(we["net"])
    for we in out:  # better than the other tracked buys of the same coin in this window (needs >= 3 of them)
        nets = by[we["mint"]]
        we["ex"] = int(we["net"] > stats.median(nets)) if len(nets) >= 3 else None
    return out


def _rank_forward(acc, wev, variants, pairs=None):
    """Walk-forward check of the wallet weights: for the next window's wallet buys, the net result after costs
    of the top fifth of wallets by score under each weight variant (learned on profit after costs, learned on
    'pumped' without a cap as before, fixed prior)."""
    if len(wev) < 10:
        return
    k = max(1, len(wev) // 5)
    acc.setdefault("all", []).extend(we["net"] for we in wev)
    for name, w in variants.items():
        scored = [(wallet_score(dict(components=we["comps"], n_tokens=we["n_tokens"]), w), we["net"]) for we in wev]
        top = sorted(scored, key=lambda x: -x[0])[:k]
        acc.setdefault(name, []).extend(n for _, n in top)
        if pairs is not None:  # every buy with its score: rank correlation score vs net, walk-forward
            pairs.setdefault(name, []).extend(scored)


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
        if t.ts < getattr(ds, "hindsight", {}).get(t.mint, 0):
            continue  # coin was picked for research by its later move: no events from before the pick
        f, wallets = ev.features_at(t.mint, t.ts, ds.window(t.mint, t.ts - W, t.ts), tmap, ds.pidx,
                                    ds.created.get(t.mint), ds.to_sol, ms.window_min,
                                    history=ds.window(t.mint, 0, t.ts), intel=getattr(ds, "intel", None))
        if f is None:
            continue
        last[t.mint] = t.ts
        out.append(dict(mint=t.mint, ts=t.ts, f=f, x=ev.vector(f), labels=ev.labels_at(t.mint, t.ts, ds.pidx, ms.horizons),
                        baseline=ev.baseline_score(f), trigger_tier=ti[0] or "scored", wallets=len(wallets)))
    return out


def run(ds, ms=msettings, max_points=24):
    cps = checkpoints(ds.ts_list, max_points)
    res = dict(checkpoints=cps, events=[], wallet_events=[], preds={h[0]: [] for h in ms.horizons},
               weights=[], tier_counts=[], tier_forward={}, wallet_rank={}, wallet_rank_h={})
    if not cps:
        log.info("not enough history for a walk-forward backtest (%.1f h)", (ds.end - ds.start) / 3600)
        return res
    h_wallet = "1h" if any(h[0] == "1h" for h in ms.horizons) else ms.horizons[0][0]
    bounds = cps + [ds.end + 1]
    for k, c in enumerate(cps):
        nxt = bounds[k + 1]
        st, _ = wallet_stats.compute(ds, c, use_flags=False)
        done = [we for we in res["wallet_events"] if we["ts"] + we["sec"] <= c]
        variants = learn.wallet_weight_variants(done)
        chosen = learn.pick_weight_variant(res["wallet_rank"])
        weights = variants[chosen]
        winfo = dict(variant=chosen, n=len(done))
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
        _rank_forward(res["wallet_rank"], wev, variants)
        for hn in [h[0] for h in ms.horizons if h[0] in ("1h", "6h")]:
            d = res["wallet_rank_h"].setdefault(hn, dict(top={}, pairs={}))
            evh = wev if hn == h_wallet else _wallet_events(ds, st, c, nxt, hn, ms.horizons)
            _rank_forward(d["top"], evh, variants, d["pairs"])
        for x in wallet_stats.first_buys(ds, c, nxt):
            tier = (tmap.get(x[0]) or (None,))[0] or "none"
            if x[0] not in st or x[2] < getattr(ds, "hindsight", {}).get(x[1], 0):
                continue
            lab = ev.labels_at(x[1], x[2], ds.pidx, [h for h in ms.horizons if h[0] == h_wallet]).get(h_wallet)
            if lab is None:
                continue
            d = res["tier_forward"].setdefault(tier, [0, 0, [], []])
            d[0] += 1
            d[1] += lab["pumped"]
            d[2].append(lab["max_ret"])
            d[3].append(lab["net"])
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
                res["preds"][name].append(dict(ts=e["ts"], mint=e["mint"], p=p, y=lab["pumped"], max_ret=lab["max_ret"],
                                               net=lab.get("net"), win=lab.get("win"), p_net=p_net,
                                               baseline=e["baseline"], model=m["kind"]))
        res["events"].extend(cev)
        log.info("checkpoint %d/%d: %d wallets, tiers %s, %d coin events", k + 1, len(cps), len(st), counts, len(cev))
    res["metrics"] = metrics(res, ms)
    main = "1h" if res["preds"].get("1h") else ms.horizons[0][0]
    t0 = time.time()
    res["metrics"]["exits"] = exits.study(res["preds"][main][-1500:], ds.pidx)
    log.info("exit study: %d rules x %d signals in %.0f s", len(exits.GRID), len(res["preds"][main][-1500:]), time.time() - t0)
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


SCORE_BANDS = ((0, 20), (20, 30), (30, 45), (45, 101))  # the score bands the strategy trades on (score = 100 p)


def score_bands(rows, key):
    """Per band of 100 * rows[key]: cases, median and mean net after costs, win rate. Shows whether a higher
    score means more money (the live finding of 10/2026: P(pump) ranked volatility, the top band lost most)."""
    out = []
    for lo, hi in SCORE_BANDS:
        nets = [p["net"] for p in rows if lo <= 100 * p[key] < hi]
        if nets:
            out.append(dict(band=f"{lo}-{min(hi, 100)}", n=len(nets), median_net=stats.median(nets),
                            mean_net=sum(nets) / len(nets), win_rate=sum(1 for x in nets if x > 0) / len(nets)))
    return out


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
    k5 = sorted(rows, key=lambda p: p["p"])
    out.update(rank_corr_pump_net=stats.spearman([p["p"] for p in rows], [p["net"] for p in rows]),
               mean_net_bottom_pump=mean([p["net"] for p in k5[:k]]),
               bands_pump=score_bands(rows, "p"))
    nm = [p for p in rows if p.get("p_net") is not None]
    if nm:
        out.update(rank_corr_netmodel_net=stats.spearman([p["p_net"] for p in nm], [p["net"] for p in nm]),
                   bands_net=score_bands(nm, "p_net"))
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
    for tier, (n, k, mr, nets) in res["tier_forward"].items():
        r, lo, hi = stats.wilson(k, n)
        wins = sum(1 for x in nets if x > 0)
        tf[tier] = dict(n=n, pump_rate=r, ci_low=lo, ci_high=hi, median_max_ret=stats.median(mr),
                        mean_net=sum(nets) / len(nets) if nets else None, win_rate_net=wins / len(nets) if nets else None)
    out["tier_forward"] = tf
    wr = {}
    for name, nets in res.get("wallet_rank", {}).items():
        if not nets:
            continue
        mu = sum(nets) / len(nets)
        sd = (sum((x - mu) ** 2 for x in nets) / max(1, len(nets) - 1)) ** 0.5
        wr[name] = dict(n=len(nets), mean_net=mu, ci_low=mu - 1.96 * sd / len(nets) ** 0.5,
                        median_net=stats.median(nets), win_rate=sum(1 for x in nets if x > 0) / len(nets))
    out["wallet_rank"] = wr
    out["wallet_rank_h"] = {}
    for hn, d in res.get("wallet_rank_h", {}).items():
        allnets = d["top"].get("all") or []
        if not allnets:
            continue
        base = sum(allnets) / len(allnets)
        row = dict(all=dict(n=len(allnets), mean_net=base))
        for name, nets in d["top"].items():
            if name == "all" or not nets:
                continue
            pr = d["pairs"].get(name) or []
            row[name] = dict(n=len(nets), mean_net_top=sum(nets) / len(nets), lift=sum(nets) / len(nets) - base,
                             rank_corr=stats.spearman([a for a, _ in pr], [b for _, b in pr]))
        out["wallet_rank_h"][hn] = row
    return out
