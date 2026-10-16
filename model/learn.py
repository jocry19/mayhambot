"""Learning: wallet component weights and per-horizon coin models (logistic for pump probability,
ridge for the expected size of the move). Falls back to a transparent prior when data is thin."""
import math

from model.config import PRIOR_WALLET_WEIGHTS, msettings
from research import stats
from research.events import FEATURE_NAMES
from research.wallet_stats import COMPONENTS

# Prior coin model in raw feature units (used until enough labelled events exist).
PRIOR_COEF = dict(n_ultra=0.9, n_smart=0.5, n_good=0.2, net_sol_ultra=0.25, net_sol_smart=0.15,
                  net_sol_good=0.05, n_tiered_5m=0.2, wq_mean=0.01, influence=0.08, log_mcap=-0.35,
                  log_age=-0.1, sell_ratio=-0.8, entry_span=-0.01, hhi=-0.3, crowd_buyers=0.1,
                  crowd_net=0.03, momentum=0.2)
PRIOR_BIAS = {"15m": -1.6, "1h": -1.9, "6h": -2.3}
PRIOR_CENTER = dict(log_mcap=5.3, log_age=2.5)


def prior_model(horizon):
    return dict(kind="prior", bias=PRIOR_BIAS.get(horizon, -2.0),
        coef=[PRIOR_COEF[f] for f in FEATURE_NAMES], mean=[PRIOR_CENTER.get(f, 0.0) for f in FEATURE_NAMES],
        std=[1.0] * len(FEATURE_NAMES), n=0, pos=0)


def learn_wallet_weights(wallet_events, prior_n=None):
    """wallet_events: list of (components dict, pumped 0/1). Returns (weights, info)."""
    prior_n = msettings.wallet_weight_prior_n if prior_n is None else prior_n
    n = len(wallet_events)
    pos = sum(y for _, y in wallet_events)
    info = dict(n=n, pos=pos, learned=None, coef=None, lam=0.0)
    if n < 50 or pos < 5 or pos == n:
        return dict(PRIOR_WALLET_WEIGHTS), info
    X = [[c[k] for k in COMPONENTS] for c, _ in wallet_events]
    y = [yy for _, yy in wallet_events]
    m = stats.logistic_fit(X, y, l2=msettings.l2)
    coef = dict(zip(COMPONENTS, m["coef"]))
    posc = {k: max(0.0, v) for k, v in coef.items()}
    tot = sum(posc.values())
    if tot <= 1e-9:
        return dict(PRIOR_WALLET_WEIGHTS), dict(info, coef=coef)
    learned = {k: v / tot for k, v in posc.items()}
    lam = n / (n + prior_n)
    w = {k: (1 - lam) * PRIOR_WALLET_WEIGHTS[k] + lam * learned[k] for k in COMPONENTS}
    s = sum(w.values())
    return {k: v / s for k, v in w.items()}, dict(info, learned=learned, coef=coef, lam=lam)


def fit_token_models(events, horizons=None, l2=None, now=None):
    """events: dicts with 'x' (feature vector), 'ts' and 'labels' {h: {...} | None}.
    Returns {h: model} using only events whose label is known (and that ended before `now`)."""
    horizons = horizons or msettings.horizons
    l2 = msettings.l2 if l2 is None else l2
    out = {}
    for name, sec, _ in horizons:
        rows = [e for e in events if e["labels"].get(name) is not None and (now is None or e["ts"] + sec <= now)]
        y = [e["labels"][name]["pumped"] for e in rows]
        pos = sum(y)
        if len(rows) < msettings.min_train_events or pos < msettings.min_train_pos or pos == len(rows):
            m = prior_model(name)
            m.update(n=len(rows), pos=pos)
        else:
            X = [e["x"] for e in rows]
            m = stats.logistic_fit(X, y, l2=l2)
            m.update(kind="logistic", n=len(rows), pos=pos)
            size = stats.ridge_fit(X, [math.log1p(max(-0.99, e["labels"][name]["max_ret"])) for e in rows], l2=l2 * 4)
            m["size"] = size
        out[name] = m
    return out


def predict(models, x):
    """{h: (p_pump, exp_max_ret, contributions list)}."""
    out = {}
    for name, m in models.items():
        p = stats.logistic_predict(m, x)
        size = m.get("size")
        exp = math.expm1(stats.ridge_predict(size, x)) if size else None
        out[name] = (p, exp, stats.contributions(m, x))
    return out
