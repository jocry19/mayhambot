"""Learning: wallet component weights and per-horizon coin models (logistic for pump probability,
ridge for the expected size of the move). Falls back to a transparent prior when data is thin."""
import math

from model.config import PRIOR_WALLET_WEIGHTS, msettings
from research import stats
from research.events import FEATURE_NAMES
from research.wallet_stats import COMPONENTS

# Prior coin model in raw feature units (used until enough labelled events exist). New features start at 0.
PRIOR_COEF = dict(n_ultra=0.9, n_smart=0.5, n_good=0.2, net_sol_ultra=0.25, net_sol_smart=0.15,
                  net_sol_good=0.05, n_tiered_5m=0.2, wq_mean=0.01, influence=0.08, log_mcap=-0.35,
                  log_age=-0.1, sell_ratio=-0.8, entry_span=-0.01, hhi=-0.3, crowd_buyers=0.1,
                  crowd_net=0.03, momentum=0.2)
PRIOR_BIAS = {"15m": -1.6, "1h": -1.9, "6h": -2.3}
PRIOR_CENTER = dict(log_mcap=5.3, log_age=2.5)


def prior_model(horizon):
    return dict(kind="prior", bias=PRIOR_BIAS.get(horizon, -2.0),
        coef=[PRIOR_COEF.get(f, 0.0) for f in FEATURE_NAMES], mean=[PRIOR_CENTER.get(f, 0.0) for f in FEATURE_NAMES],
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
    """{h: (p_pump, exp_max_ret, contributions list over all features)}."""
    out = {}
    for name, m in models.items():
        p, con = predict_one(m, x)
        size = m.get("size")
        cols = m.get("cols")
        xs = [x[i] for i in cols] if cols is not None else x
        exp = math.expm1(stats.ridge_predict(size, xs)) if size else None
        out[name] = (p, exp, con)
    return out


# ---------------------------------------------------------------- model selection (automatic improvement)

def _cols(drop):
    from research.events import FEATURE_GROUPS
    gone = set(FEATURE_GROUPS.get(drop, [])) if drop else set()
    return [i for i, f in enumerate(FEATURE_NAMES) if f not in gone]


def _fit_cfg(cfg, X, y, l2):
    from research import gbm
    cols = _cols(cfg.get("drop"))
    Xs = [[r[i] for i in cols] for r in X]
    if cfg["algo"] == "gbm":
        m = gbm.fit(Xs, y)
    else:
        m = stats.logistic_fit(Xs, y, l2=l2)
        m["kind"] = "logistic"
    m["cols"] = cols
    m["cfg"] = dict(cfg)
    return m


def predict_one(m, x):
    """(p, contributions over ALL features) for any stored model kind."""
    from research import gbm
    cols = m.get("cols")
    xs = [x[i] for i in cols] if cols is not None else x
    if m.get("kind") == "gbm":
        p, c = gbm.predict(m, xs), gbm.contributions(m, xs)
    else:
        p, c = stats.logistic_predict(m, xs), stats.contributions(m, xs)
    if cols is None:
        return p, c
    full = [0.0] * len(FEATURE_NAMES)
    for i, ci in zip(cols, c):
        full[i] = ci
    return p, full


def weights_full(m):
    """One weight per feature for display: logistic coefficient (per std-dev) or tree importance (share of gain)."""
    vals = m.get("importance") if m.get("kind") == "gbm" else m.get("coef")
    cols = m.get("cols")
    if cols is None:
        return list(vals)
    full = [0.0] * len(FEATURE_NAMES)
    for i, v in zip(cols, vals):
        full[i] = v
    return full


def _logloss(ps, ys):
    e = 1e-6
    return -sum(y * math.log(max(p, e)) + (1 - y) * math.log(max(1 - p, e)) for p, y in zip(ps, ys)) / max(1, len(ys))


CANDIDATES = [{"algo": a, "drop": d} for a in ("logistic", "gbm") for d in (None, "tiers", "crowd", "price_time")]


def select_models(events, champion=None, horizons=None, l2=None, now=None, min_gain=0.005):
    """For each horizon: time-split the labelled events (first 70 % train, purged; last 30 % holdout), evaluate
    every candidate (algorithm x feature set) out of sample, keep the current champion unless a candidate
    beats it by more than `min_gain` relative log-loss, then refit the winner on all labelled events.
    Returns (models, report)."""
    horizons = horizons or msettings.horizons
    l2 = msettings.l2 if l2 is None else l2
    champion = champion or {}
    models, report = {}, {}
    for name, sec, _ in horizons:
        rows = sorted((e for e in events if e["labels"].get(name) is not None and (now is None or e["ts"] + sec <= now)),
                      key=lambda e: e["ts"])
        y_all = [e["labels"][name]["pumped"] for e in rows]
        pos = sum(y_all)
        if len(rows) < 2 * msettings.min_train_events or pos < 2 * msettings.min_train_pos or pos == len(rows):
            m = prior_model(name)
            m.update(n=len(rows), pos=pos)
            models[name] = m
            report[name] = dict(status="prior", n=len(rows), pos=pos, reason="zu wenig Fälle für einen ehrlichen Test",
                                need_n=2 * msettings.min_train_events, need_pos=2 * msettings.min_train_pos)
            continue
        cut = rows[int(len(rows) * 0.7)]["ts"]
        train = [e for e in rows if e["ts"] + sec <= cut]
        test = [e for e in rows if e["ts"] > cut]
        yt = [e["labels"][name]["pumped"] for e in test]
        ytr = [e["labels"][name]["pumped"] for e in train]
        res = []
        base_p = sum(ytr) / max(1, len(ytr))
        base_ll = _logloss([base_p] * len(yt), yt)
        if len(train) >= msettings.min_train_events and 0 < sum(ytr) < len(ytr) and test and 0 < sum(yt):
            for cfg in CANDIDATES:
                try:
                    m = _fit_cfg(cfg, [e["x"] for e in train], ytr, l2)
                    ps = [predict_one(m, e["x"])[0] for e in test]
                    res.append(dict(cfg=cfg, logloss=_logloss(ps, yt), auc=stats.auc(ps, yt)))
                except Exception as ex:  # noqa: BLE001
                    res.append(dict(cfg=cfg, error=str(ex)[:80]))
        skipped = None
        if not res:
            skipped = (f"Kandidaten nicht trainierbar: Training {len(train)} Fälle/{sum(ytr)} Pumps, "
                       f"Holdout {len(test)}/{sum(yt)} (nötig: Training ≥ {msettings.min_train_events}, "
                       "beide Klassen in Training und Holdout)")
        ok = [r for r in res if "logloss" in r]
        champ_cfg = champion.get(name) or {"algo": "logistic", "drop": None}
        champ = next((r for r in ok if r["cfg"] == champ_cfg), None)
        best = min(ok, key=lambda r: r["logloss"]) if ok else None
        if best and (champ is None or best["logloss"] < champ["logloss"] * (1 - min_gain)):
            chosen, why = best["cfg"], "neuer Sieger" if champ and best["cfg"] != champ_cfg else "bester Kandidat"
        else:
            chosen, why = champ_cfg, "Champion bleibt (kein Kandidat klar besser)"
        m = _fit_cfg(chosen, [e["x"] for e in rows], y_all, l2)
        if chosen["algo"] == "logistic":
            m["size"] = stats.ridge_fit([[e["x"][i] for i in m["cols"]] for e in rows],
                                        [math.log1p(max(-0.99, e["labels"][name]["max_ret"])) for e in rows], l2=l2 * 4)
        m.update(n=len(rows), pos=pos)
        models[name] = m
        report[name] = dict(status="selected", chosen=chosen, why=why, n=len(rows), pos=pos, n_train=len(train),
                            n_test=len(test), base_logloss=base_ll, candidates=res, skipped=skipped,
                            holdout=next((r for r in ok if r["cfg"] == chosen), None))
    return models, report
