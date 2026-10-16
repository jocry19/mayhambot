"""Live coin prediction with explainable weights + the coin's signal history."""
import json
import time

from model import learn
from model.config import msettings
from model.tiers import tier_map
from research import data, events as ev
from research.store import connect, ensure_schema


class ModelNotTrained(RuntimeError):
    """Raised by score_token before the first retrain; callers keep their own heuristic."""


def active_version(conn):
    return conn.execute("SELECT * FROM model_versions WHERE active=1 ORDER BY id DESC LIMIT 1").fetchone()


_cache = {"id": None, "models": None}


def _models(conn):
    v = active_version(conn)
    if v is None:
        return 0, {h[0]: learn.prior_model(h[0]) for h in msettings.horizons}
    if _cache["id"] != v["id"]:
        _cache.update(id=v["id"], models=json.loads(v["token_model_json"]))
    return v["id"], _cache["models"]


def _grade(p):
    return "A" if p >= 0.6 else "B" if p >= 0.45 else "C" if p >= 0.3 else "D" if p >= 0.15 else "E"


def score_token(mint, now=None, conn=None, store=False, tiers=None):
    conn = ensure_schema(conn) if conn is not None else connect()
    now = int(now or time.time())
    W = msettings.window_min * 60
    version, models = _models(conn)
    if not version:
        raise ModelNotTrained("no model version yet: run python -m model retrain")
    tiers = tiers if tiers is not None else tier_map(conn)
    ds = data.load(conn, mints=[mint])
    tok = conn.execute("SELECT symbol, pair_created_at, price_usd, mcap_usd FROM tokens WHERE mint=?", (mint,)).fetchone()
    created = ds.created.get(mint) or (tok["pair_created_at"] if tok else None)
    f, wallets = ev.features_at(mint, now, ds.window(mint, now - W, now), tiers, ds.pidx, created, ds.to_sol,
                                msettings.window_min, history=ds.window(mint, 0, now))
    if f is None:
        return None
    x = ev.vector(f)
    preds = learn.predict(models, x)
    horizons, contribs = {}, {}
    wfull = {n: learn.weights_full(m) for n, m in models.items()}
    for name, sec, thr in msettings.horizons:
        p, exp, con = preds[name]
        horizons[name] = dict(p_pump=round(p, 4), threshold=thr,
                              exp_max_ret=round(exp, 4) if exp is not None else round(p * thr * 1.5, 4),
                              model=models[name].get("kind", "logistic"),
                              algo=(models[name].get("cfg") or {}).get("algo", models[name].get("kind")))
        if f"{name}_net" in preds:  # P(profit after costs) and expected net return, see events.labels_at
            pn, en, _ = preds[f"{name}_net"]
            horizons[name].update(p_win_net=round(pn, 4), exp_net_ret=round(en, 4) if en is not None else None)
        tot = sum(abs(c) for c in con) or 1.0
        contribs[name] = sorted(({"feature": fn, "label": ev.FEATURE_LABELS[fn], "value": round(f[fn], 4),
                                  "weight": round(wfull[name][i], 4), "contribution": round(con[i], 4),
                                  "share": round(abs(con[i]) / tot, 4)} for i, fn in enumerate(ev.FEATURE_NAMES)),
                                key=lambda d: -abs(d["contribution"]))
    main = "1h" if "1h" in horizons else msettings.horizons[0][0]
    p_main = horizons[main]["p_pump"]
    price = ds.pidx.price_at(mint, now)
    tiers_out = {t: {"wallets": sum(1 for w in wallets if w["tier"] == t and w["net_sol"] > 0),
                     "net_sol": round(sum(w["net_sol"] for w in wallets if w["tier"] == t), 4)}
                 for t in ("ultra", "smart", "good")}
    res = dict(mint=mint, symbol=tok["symbol"] if tok else "", ts=now, version=version,
               score=round(100 * p_main, 1), grade=_grade(p_main), price_usd=price,
               mcap_usd=ds.pidx.mcap(mint, price), horizons=horizons, features=f,
               contributions=contribs, tiers=tiers_out, wallets=wallets)
    if store:
        hp = lambda n: horizons.get(n, {}).get("p_pump")  # noqa: E731
        conn.execute(
            """INSERT INTO model_predictions(ts, mint, symbol, version, score, p_15m, p_1h, p_6h, exp_ret_1h,
                   price_usd, mcap_usd, n_ultra, n_smart, n_good, net_sol, features_json, contributions_json,
                   wallets_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (now, mint, res["symbol"], version, res["score"], hp("15m"), hp("1h"), hp("6h"),
             horizons.get("1h", {}).get("exp_max_ret"), price, res["mcap_usd"],
             tiers_out["ultra"]["wallets"], tiers_out["smart"]["wallets"], tiers_out["good"]["wallets"],
             sum(t["net_sol"] for t in tiers_out.values()), json.dumps(f), json.dumps(contribs), json.dumps(wallets)))
        conn.commit()
    return res


def token_signal_history(mint, conn=None):
    conn = ensure_schema(conn) if conn is not None else connect()
    out = []
    for r in conn.execute("SELECT * FROM model_predictions WHERE mint=? ORDER BY ts", (mint,)):
        d = dict(r)
        for k in ("features_json", "contributions_json", "wallets_json"):
            d[k[:-5]] = json.loads(d.pop(k) or "null")
        out.append(d)
    return out


def update_outcomes(conn=None, now=None):
    conn = ensure_schema(conn) if conn is not None else connect()
    now = int(now or time.time())
    rows = conn.execute("SELECT id, ts, mint, price_usd FROM model_predictions WHERE ts > ? AND max_ret_6h IS NULL",
                        (now - 8 * 86400,)).fetchall()
    if not rows:
        return 0
    from research.prices import PriceIndex
    pidx = PriceIndex.from_db(conn, {r["mint"] for r in rows})
    n = 0
    for r in rows:
        p0 = r["price_usd"] or pidx.price_at(r["mint"], r["ts"])
        if not p0:
            continue
        upd = {}
        for col, sec in (("ret_15m", 900), ("ret_1h", 3600), ("ret_6h", 21600)):
            if now >= r["ts"] + sec:
                p = pidx.price_at(r["mint"], r["ts"] + sec)
                if p and pidx.coverage_until(r["mint"]) >= r["ts"] + sec:
                    upd[col] = p / p0 - 1
        for col, sec in (("max_ret_1h", 3600), ("max_ret_6h", 21600)):
            if now >= r["ts"] + sec and pidx.coverage_until(r["mint"]) >= r["ts"] + sec:
                mx = pidx.max_between(r["mint"], r["ts"], r["ts"] + sec)
                if mx:
                    upd[col] = mx / p0 - 1
        if upd:
            conn.execute(f"UPDATE model_predictions SET {', '.join(k + '=?' for k in upd)} WHERE id=?",
                         (*upd.values(), r["id"]))
            n += 1
    conn.commit()
    return n


def score_many(mints, conn=None, store=True, now=None):
    conn = ensure_schema(conn) if conn is not None else connect()
    tiers = tier_map(conn)
    out = []
    for m in mints:
        try:
            r = score_token(m, now=now, conn=conn, store=store, tiers=tiers)
        except ModelNotTrained:
            return []
        if r:
            out.append(r)
    return sorted(out, key=lambda r: -r["score"])


