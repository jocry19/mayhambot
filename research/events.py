"""Coin features at a moment t (only data up to t) and outcome labels after t.

features_at() is shared by the backtest and the live predictor, so both see exactly the same inputs.
"""
import math

from research.config import rsettings

FEATURES = (
    ("n_ultra", "Ultra-Smart-Käufer"),
    ("n_smart", "Smart-Käufer"),
    ("n_good", "Good-Käufer"),
    ("net_sol_ultra", "Netto-Inflow Ultra (SOL, log)"),
    ("net_sol_smart", "Netto-Inflow Smart (SOL, log)"),
    ("net_sol_good", "Netto-Inflow Good (SOL, log)"),
    ("n_tiered_5m", "Tier-Käufer in den letzten 5 min"),
    ("wq_mean", "Ø Wallet-Score der Käufer"),
    ("influence", "Einfluss der Käufer (log USD Follower-Volumen)"),
    ("log_mcap", "Market Cap jetzt (log10)"),
    ("log_age", "Coin-Alter (log10 Minuten)"),
    ("sell_ratio", "Verkaufsquote der Tier-Wallets"),
    ("entry_span", "Streuung der Einstiege (min)"),
    ("hhi", "Konzentration der Käufe (HHI)"),
    ("crowd_buyers", "Alle Käufer im Fenster (log)"),
    ("crowd_net", "Netto-Inflow alle Wallets (log USD)"),
    ("momentum", "Preis-Momentum im Fenster (log)"),
    # structure of the coin's order flow (deeper on-chain features)
    ("n_clusters", "Unabhängige Smart-Money-Gruppen"),
    ("smart_rank", "Smart Money vor der Masse (0 = als Erste)"),
    ("exit_quality", "Ausstiegs-Qualität der Käufer"),
    ("bundle_share", "Gebündelte Käufe (gleiche Sekunde)"),
    ("top10_share", "Anteil der 10 größten Käufer"),
    ("buyer_accel", "Beschleunigung neuer Käufer"),
    ("early_sell", "Verkäufe der frühesten Käufer"),
    ("first_tier_age", "Coin-Alter beim ersten Smart-Kauf (log10 min)"),
)
from research import intel_features as _intel  # noqa: E402

FEATURES = FEATURES + tuple((n, _intel.LABELS[n]) for n in _intel.NAMES)
FEATURE_GROUPS = {
    "intel": list(_intel.NAMES),
    "tiers": ["n_ultra", "n_smart", "n_good", "net_sol_ultra", "net_sol_smart", "net_sol_good", "n_tiered_5m",
              "wq_mean", "influence", "sell_ratio", "entry_span", "hhi", "n_clusters", "smart_rank", "exit_quality"],
    "crowd": ["crowd_buyers", "crowd_net", "buyer_accel", "top10_share", "bundle_share", "early_sell"],
    "price_time": ["log_mcap", "log_age", "momentum", "first_tier_age"],
}
FEATURE_NAMES = [f for f, _ in FEATURES]
# version stamp of the feature vector: stored with every model, so code and model can tell if they fit together
import hashlib as _hashlib  # noqa: E402
FEATURE_SET = _hashlib.sha1(",".join(FEATURE_NAMES).encode()).hexdigest()[:10]
FEATURE_LABELS = dict(FEATURES)


MAX_RET = 50.0  # +5000 %: anything above is treated as a data error and capped


def slog(x):
    return math.copysign(math.log1p(abs(x)), x)


def features_at(mint, t, window_trades, tiers, pidx, created_at, to_sol, window_min, history=None, intel=None):
    """window_trades: trades of this coin with t - window < ts <= t (objects with wallet, side, ts, usd, sol).
    history: all known trades of the coin up to t (for order-flow structure), defaults to window_trades.
    tiers: wallet -> (tier, score, influence_usd[, cluster_id, exit_capture]).
    intel: optional research.intel_features.Source; without it the intel features are neutral.
    Returns (features dict, per-wallet list) or (None, None)."""
    px = pidx.price_at(mint, t)
    if not px:
        return None, None
    per = {}
    crowd = {}
    crowd_net = 0.0
    for tr in window_trades:
        sol = to_sol(tr)
        if tr.side == "buy":
            crowd[tr.wallet] = True
            crowd_net += tr.usd
        else:
            crowd_net -= tr.usd
        tinfo = tiers.get(tr.wallet)
        if not tinfo or not tinfo[0]:
            continue  # only tiered wallets count as smart money
        d = per.setdefault(tr.wallet, dict(address=tr.wallet, tier=tinfo[0], score=tinfo[1], infl=tinfo[2],
                                           buy_sol=0.0, sell_sol=0.0, first_ts=tr.ts, last_buy=0))
        if tr.side == "buy":
            d["buy_sol"] += sol
            d["last_buy"] = max(d["last_buy"], tr.ts)
        else:
            d["sell_sol"] += sol
        d["first_ts"] = min(d["first_ts"], tr.ts)
    f = {k: 0.0 for k in FEATURE_NAMES}
    buyers = [d for d in per.values() if d["buy_sol"] - d["sell_sol"] > 0]
    for tier in ("ultra", "smart", "good"):
        f["n_" + tier] = float(sum(1 for d in buyers if d["tier"] == tier))
        f["net_sol_" + tier] = slog(sum(d["buy_sol"] - d["sell_sol"] for d in per.values() if d["tier"] == tier))
    f["n_tiered_5m"] = float(sum(1 for d in per.values() if d["last_buy"] > t - 300))
    f["wq_mean"] = sum(d["score"] for d in buyers) / len(buyers) if buyers else 0.0
    f["influence"] = math.log1p(sum(d["infl"] for d in buyers))
    mcap = pidx.mcap(mint, px)
    f["log_mcap"] = math.log10(max(mcap, 1.0))
    age_min = (t - created_at) / 60 if created_at and created_at <= t else None
    f["log_age"] = math.log10(1 + age_min) if age_min is not None else 2.5
    bs = sum(d["buy_sol"] for d in per.values())
    ss = sum(d["sell_sol"] for d in per.values())
    f["sell_ratio"] = min(2.0, ss / bs) if bs > 0 else (1.0 if ss > 0 else 0.0)
    if len(buyers) >= 2:
        fts = [d["first_ts"] for d in buyers]
        f["entry_span"] = (max(fts) - min(fts)) / 60
    tb = sum(d["buy_sol"] for d in buyers)
    f["hhi"] = sum((d["buy_sol"] / tb) ** 2 for d in buyers) if tb > 0 else 0.0
    f["crowd_buyers"] = math.log1p(len(crowd))
    f["crowd_net"] = slog(crowd_net)
    p0 = pidx.price_at(mint, t - window_min * 60)
    f["momentum"] = max(-3.0, min(3.0, math.log(px / p0))) if p0 else 0.0
    _structure(f, t, window_trades, history or window_trades, tiers, buyers, created_at)
    wallets = sorted(({"address": d["address"], "tier": d["tier"], "score": d["score"],
                       "net_sol": round(d["buy_sol"] - d["sell_sol"], 4), "first_ts": d["first_ts"]}
                      for d in per.values()), key=lambda d: (-d["net_sol"]))
    f.update(intel(mint, t) if intel is not None else _intel.neutral())
    return f, wallets


def _structure(f, t, window, history, tiers, buyers, created_at):
    # independent groups: wallets of one cluster (same entity / bundle) count once
    cl = set()
    caps = []
    for d in buyers:
        ti = tiers.get(d["address"]) or ()
        cl.add(ti[3] if len(ti) > 3 and ti[3] is not None else d["address"])
        if len(ti) > 4 and ti[4] is not None:
            caps.append(ti[4])
    f["n_clusters"] = float(len(cl))
    f["exit_quality"] = sum(caps) / len(caps) if caps else 0.0
    # order of entry among all buyers of the coin so far
    first = {}
    for tr in history:
        if tr.side == "buy" and tr.wallet not in first:
            first[tr.wallet] = len(first)
    n = max(1, len(first) - 1)
    ranks = [first[d["address"]] / n for d in buyers if d["address"] in first]
    f["smart_rank"] = sorted(ranks)[len(ranks) // 2] if ranks else 1.0
    tier_first = [tr.ts for tr in history if tr.side == "buy" and (tiers.get(tr.wallet) or (None,))[0]]
    if tier_first and created_at and created_at <= tier_first[0]:
        f["first_tier_age"] = math.log10(1 + (tier_first[0] - created_at) / 60)
    else:
        f["first_tier_age"] = f["log_age"]
    # bundles: buys of 3+ different wallets in the same second
    by_sec = {}
    buy_usd = 0.0
    per_w = {}
    for tr in window:
        if tr.side != "buy":
            continue
        by_sec.setdefault(tr.ts, set()).add(tr.wallet)
        buy_usd += tr.usd
        per_w[tr.wallet] = per_w.get(tr.wallet, 0.0) + tr.usd
    bundled = {s_ for s_, ws in by_sec.items() if len(ws) >= 3}
    f["bundle_share"] = (sum(tr.usd for tr in window if tr.side == "buy" and tr.ts in bundled) / buy_usd) if buy_usd else 0.0
    f["top10_share"] = (sum(sorted(per_w.values(), reverse=True)[:10]) / buy_usd) if buy_usd else 0.0
    fts = {}
    for tr in history:
        if tr.side == "buy":
            fts.setdefault(tr.wallet, tr.ts)
    last5 = sum(1 for v in fts.values() if v > t - 300)
    prev25 = sum(1 for v in fts.values() if t - 1800 < v <= t - 300)
    f["buyer_accel"] = math.log((last5 + 1) / (prev25 / 5 + 1))
    # did the coin's earliest buyers start selling in the window?
    early = {w for w, i in first.items() if i < 20}
    eb = sum(tr.usd for tr in history if tr.side == "buy" and tr.wallet in early)
    es = sum(tr.usd for tr in window if tr.side == "sell" and tr.wallet in early)
    f["early_sell"] = min(2.0, es / eb) if eb else 0.0


def labels_at(mint, t, pidx, horizons):
    """horizon name -> dict(max_ret, ret, pumped) or None when price data does not reach t + h yet."""
    p0 = pidx.price_at(mint, t)
    out = {}
    cov = pidx.coverage_until(mint)
    for name, sec, thr in horizons:
        if not p0 or cov < t + sec:
            out[name] = None
            continue
        mx = pidx.max_between(mint, t, t + sec)
        end = pidx.price_at(mint, t + sec)
        mr = min(MAX_RET, (mx / p0 - 1)) if mx else 0.0
        ret = (end / p0 - 1) if end else None
        # tradeable outcome: sell at the pump threshold if it was reached, else at the horizon end, minus costs
        gross = thr if mr >= thr else (ret if ret is not None else 0.0)
        net = min(MAX_RET, gross) - rsettings.cost_roundtrip
        out[name] = dict(max_ret=mr, ret=ret, pumped=int(mr >= thr), net=net, win=int(net > 0))
    return out


def vector(f):
    return [f[k] for k in FEATURE_NAMES]


def baseline_score(f):
    """The classic signal score (inflow + breadth + quality) from the same inputs, for comparison."""
    net = sum(math.copysign(math.expm1(abs(f[k])), f[k]) for k in ("net_sol_ultra", "net_sol_smart", "net_sol_good"))
    n = f["n_ultra"] + f["n_smart"] + f["n_good"]
    if net <= 0 or n <= 0:
        return 0.0
    return 100 * (0.4 * (1 - math.exp(-net / 30)) + 0.3 * (1 - math.exp(-n / 4)) + 0.3 * f["wq_mean"] / 100)
