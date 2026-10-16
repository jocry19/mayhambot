"""Coin features at a moment t (only data up to t) and outcome labels after t.

features_at() is shared by the backtest and the live predictor, so both see exactly the same inputs.
"""
import math

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
)
FEATURE_NAMES = [f for f, _ in FEATURES]
FEATURE_LABELS = dict(FEATURES)


def slog(x):
    return math.copysign(math.log1p(abs(x)), x)


def features_at(mint, t, window_trades, tiers, pidx, created_at, to_sol, window_min):
    """window_trades: trades of this coin with t - window < ts <= t (objects with wallet, side, ts, usd, sol).
    tiers: wallet -> (tier, score, influence_usd). Returns (features dict, per-wallet list) or (None, None)."""
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
            continue
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
    wallets = sorted(({"address": d["address"], "tier": d["tier"], "score": d["score"],
                       "net_sol": round(d["buy_sol"] - d["sell_sol"], 4), "first_ts": d["first_ts"]}
                      for d in per.values()), key=lambda d: (-d["net_sol"]))
    return f, wallets


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
        mr = (mx / p0 - 1) if mx else 0.0
        out[name] = dict(max_ret=mr, ret=(end / p0 - 1) if end else None, pumped=int(mr >= thr))
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
