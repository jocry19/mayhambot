"""Smart-money inflow signals per token.

score = 100 * (0.4 * inflow + 0.3 * breadth + 0.3 * quality)   (stored per input in contributions_json)
  inflow  = 1 - exp(-net_smart_usd / INFLOW_REF_USD)      how much money
  breadth = 1 - exp(-smart_buyers / WALLETS_REF_COUNT)    how many smart wallets
  quality = USD-weighted mean wallet score / 100          how good they are
A wallet's score here is computed WITHOUT the token being judged, so a wallet is not called
smart just because this very coin pumped.
"""
import json
import logging
import math
import time

from core import scoring
from core.config import settings

log = logging.getLogger("mayhambot.signals")
MANUAL_DEFAULT_SCORE = 70.0


class QualityCache:
    def __init__(self, conn, cfg=settings):
        self.cfg = cfg
        self.positions = scoring.load_positions(conn)
        self.meta = {r["address"]: r for r in conn.execute("SELECT address, manual, is_bot, label FROM wallets")}
        self.model_version, self.model_tiers = scoring.model_tiers(conn)  # one truth once model/ is active
        self._c = {}

    def get(self, wallet, mint):
        k = (wallet, mint)
        if k not in self._c:
            m = self.meta.get(wallet)
            q = scoring.wallet_quality(self.positions.get(wallet, []), exclude_mint=mint, cfg=self.cfg)
            manual = bool(m and m["manual"])
            bot = bool(m and m["is_bot"])
            score = q["score"] if q["n"] else (MANUAL_DEFAULT_SCORE if manual else 0.0)
            if wallet in self.model_tiers:  # scored by the active model version: its tier is the truth
                tier = self.model_tiers[wallet][0]
                if manual and not bot and tier not in ("ultra", "smart"):
                    tier = "smart"
            else:  # no model yet, or not scored yet: heuristic
                tier = scoring.tier_for(score, q["n"], bot, manual, self.cfg)
            self._c[k] = (score, tier, q["n"])
        return self._c[k]

    def entity(self, wallet):
        """Wallets of one model cluster (same funder etc.) are one buyer."""
        c = (self.model_tiers.get(wallet) or (None, None, None))[2]
        return f"cluster:{c}" if c else wallet


TIER_RANK = {"ultra": 3, "smart": 2, "good": 1}
last_cluster_stats = {"wallets": 0, "entities": 0}


def token_metrics(conn, mint, qc, now=None, cfg=settings):
    now = now or int(time.time())
    since = now - cfg.signal_window_min * 60
    rows = conn.execute("SELECT wallet, side, usd_amount, sol_amount, ts FROM trades WHERE mint=? AND ts>=? AND ts<=?",
                        (mint, since, now)).fetchall()
    per = {}
    for r in rows:
        score, tier, n = qc.get(r["wallet"], mint)
        if tier is None:
            continue
        d = per.setdefault(r["wallet"], dict(wallet=r["wallet"], score=score, tier=tier, n_tokens=n, buy_usd=0.0,
                                             sell_usd=0.0, buy_sol=0.0, first_ts=r["ts"]))
        if r["side"] == "buy":
            d["buy_usd"] += r["usd_amount"] or 0
            d["buy_sol"] += r["sol_amount"] or 0
        else:
            d["sell_usd"] += r["usd_amount"] or 0
            d["sell_sol"] = d.get("sell_sol", 0.0) + (r["sol_amount"] or 0)
        d["first_ts"] = min(d["first_ts"], r["ts"])
    smart = [d for d in per.values() if d["tier"] in ("ultra", "smart")]
    buy = sum(d["buy_usd"] for d in smart)
    sell = sum(d["sell_usd"] for d in smart)
    net = buy - sell
    buy_sol = sum(d["buy_sol"] for d in smart)
    net_sol = buy_sol - sum(d.get("sell_sol", 0.0) for d in smart)
    buyers = [d for d in per.values() if d["buy_usd"] - d["sell_usd"] > 0]
    # one entity per model cluster: its best tier counts once (money still adds up)
    ent = {}
    for d in buyers:
        d["entity"] = qc.entity(d["wallet"]) if hasattr(qc, "entity") else d["wallet"]
        if TIER_RANK.get(d["tier"], 0) > TIER_RANK.get(ent.get(d["entity"]), 0):
            ent[d["entity"]] = d["tier"]
    tiers = {t: sum(1 for et in ent.values() if et == t) for t in ("ultra", "smart", "good")}
    sbuyers = [d for d in buyers if d["tier"] in ("ultra", "smart")]
    n_smart_entities = len({d["entity"] for d in sbuyers})
    last_cluster_stats["wallets"] += len(sbuyers)
    last_cluster_stats["entities"] += n_smart_entities
    wsum = sum(d["buy_usd"] for d in sbuyers)
    avg_q = (sum(d["score"] * d["buy_usd"] for d in sbuyers) / wsum) if wsum else 0.0
    parts = {
        "inflow": (0.4, 1 - math.exp(-max(net, 0) / cfg.inflow_ref_usd), f"Netto-Inflow Smart ${net:,.0f}"),
        "breadth": (0.3, 1 - math.exp(-n_smart_entities / cfg.wallets_ref_count),
                    f"{tiers['ultra']} Ultra + {tiers['smart']} Smart kaufen"
                    + (f" ({len(sbuyers)} Wallets)" if len(sbuyers) != n_smart_entities else "")),
        "quality": (0.3, avg_q / 100, f"Ø Wallet-Score {avg_q:.0f}"),
    }
    contributions = {k: dict(weight=w, value=round(v, 4), points=round(100 * w * v, 1), why=why)
                     for k, (w, v, why) in parts.items()}
    score = round(sum(c["points"] for c in contributions.values()), 1) if net > 0 and sbuyers else 0.0
    return dict(ts=now, mint=mint, window_min=cfg.signal_window_min, buy_usd=buy, sell_usd=sell, net_usd=net,
                buy_sol=buy_sol, net_sol=net_sol, smart_buyers=n_smart_entities, avg_quality=avg_q, score=score,
                n_ultra=tiers["ultra"], n_smart=tiers["smart"], n_good=tiers["good"],
                contributions=contributions, model_version="core-heuristic-v1",
                wallets=sorted(buyers, key=lambda d: d["buy_usd"], reverse=True))


def _load_model():
    try:
        from model import score_token  # owned by the research thread, see model/README.md
        return score_token
    except Exception:  # noqa: BLE001 - model/ not present or broken: keep the core heuristic
        return None


_score_token = _load_model()


def apply_model(conn, mint, now, m):
    """Replace score / tiers / prediction / contributions with model.score_token when available."""
    if not _score_token:
        return m
    try:
        r = _score_token(mint, now=now, conn=conn, store=True)
    except Exception as e:  # noqa: BLE001 - incl. model.predict.ModelNotTrained before the first retrain
        (log.debug if type(e).__name__ == "ModelNotTrained" else log.warning)(
            "model.score_token %s not used, heuristic instead: %s", mint[:6], e)
        return m
    tiers = r.get("tiers") or {}
    h1 = (r.get("horizons") or {}).get("1h") or {}
    contrib = {}
    for c in (r.get("contributions") or {}).get("1h") or []:
        contrib[c["feature"]] = dict(weight=c.get("weight"), value=c.get("value"),
                                     contribution=c.get("contribution"), share=c.get("share"),
                                     points=round((c.get("share") or 0) * (r.get("score") or 0), 1),
                                     why=c.get("label", c["feature"]))
    m = dict(m)
    m.update(score=round(r.get("score") or 0, 1), model_version=f"model-v{r.get('version')}",
             n_ultra=(tiers.get("ultra") or {}).get("wallets", 0), n_smart=(tiers.get("smart") or {}).get("wallets", 0),
             n_good=(tiers.get("good") or {}).get("wallets", 0),
             net_sol=sum((t or {}).get("net_sol", 0) or 0 for t in tiers.values()),
             prediction=h1.get("p_pump"), prediction_label=f"P(+{(h1.get('threshold') or 0.5)*100:.0f}% in 1h), Note {r.get('grade', '')}",
             contributions=contrib or m["contributions"])
    m["smart_buyers"] = m["n_ultra"] + m["n_smart"]
    m["min_score"] = cfg_min_model()
    if r.get("wallets"):
        m["wallets"] = [dict(wallet=w["address"], tier=w.get("tier"), score=w.get("score", 0), n_tokens=None,
                             buy_usd=0.0, buy_sol=w.get("net_sol", 0), sell_usd=0.0, first_ts=w.get("first_ts"))
                        for w in r["wallets"]]
    return m


def _load_safety():
    try:
        import safety
        return safety
    except Exception as e:  # noqa: BLE001 - signals keep working without safety/
        log.info("safety/ not available, no risk penalty: %s", e)
        return None


_safety = _load_safety()


def apply_safety(conn, mint, m):
    """score x safety.signal_penalty factor (1.0 up to risk 25, 0.2 at 75, 0 when blocked, 1.0 unchecked).
    Adds a negative 'safety' contribution so the points still add up to the score."""
    if not _safety:
        return m
    try:
        p = _safety.signal_penalty(conn, mint)
    except Exception as e:  # noqa: BLE001
        log.warning("safety.signal_penalty %s failed: %s", mint[:6], e)
        return m
    m = dict(m, safety=p, score_raw=m["score"])
    if p.get("factor", 1.0) < 1.0:
        m["score"] = round(m["score"] * p["factor"], 1)
        m["contributions"] = dict(m["contributions"], safety=dict(
            weight=None, value=p["factor"], points=round(m["score"] - m["score_raw"], 1),
            why=f"Risiko {p.get('risk', 0):.0f} ({p.get('level')}): " + "; ".join(p.get("reasons") or [])))
    return m


def check_before_signal(conn, mint, m):
    """An unchecked coin about to fire gets a quick safety check (no deep checks, about 8 calls) first."""
    if not _safety or (m.get("safety") or {}).get("checked", True):
        return m
    try:
        _safety.assess(mint, conn, deep=False)
    except Exception as e:  # noqa: BLE001
        log.warning("safety.assess %s failed: %s", mint[:6], e)
        return m
    m = dict(m, score=m.get("score_raw", m["score"]))
    return apply_safety(conn, mint, m)


def cfg_min_model():
    return settings.model_signal_min_score  # model score = P(pump within 1h) x 100


def calibrated_prediction(conn, score, min_n=8):
    """Placeholder prediction until model/ exists: share of past signals with a similar score that reached
    +50% within 1h (Bayesian prior 0.3, strength 5). None if there is not enough history."""
    rows = conn.execute("SELECT max_ret_1h FROM signals WHERE max_ret_1h IS NOT NULL AND ABS(score-?)<=10",
                        (score,)).fetchall()
    if len(rows) < min_n:
        return None, f"zu wenig Historie ({len(rows)} Signale)"
    hits = sum(1 for r in rows if r[0] >= 0.5)
    p = (hits + 0.3 * 5) / (len(rows) + 5)
    return round(p, 3), f"Chance auf +50% in 1h, aus {len(rows)} ähnlichen Signalen"


def evaluate(conn, mints, cfg=settings, now=None):
    """Compute + store metrics for each mint; create signals that pass thresholds. Returns new signal rows."""
    now = now or int(time.time())
    qc = QualityCache(conn, cfg)
    new = []
    for mint in mints:
        tok = conn.execute("SELECT symbol, price_usd, mcap_usd, liquidity_usd FROM tokens WHERE mint=?",
                           (mint,)).fetchone()
        m = apply_safety(conn, mint, apply_model(conn, mint, now, token_metrics(conn, mint, qc, now, cfg)))
        if "prediction" in m:
            pred, pred_label = m["prediction"], m["prediction_label"]
        else:
            pred, pred_label = calibrated_prediction(conn, m["score"]) if m["score"] > 0 else (None, "")
        conn.execute(
            """INSERT OR REPLACE INTO signal_metrics(ts, mint, window_min, buy_usd, sell_usd, net_usd, smart_buyers,
                   avg_quality, score, price_usd, buy_sol, net_sol, mcap_usd, n_ultra, n_smart, n_good, prediction,
                   contributions_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (now, mint, m["window_min"], m["buy_usd"], m["sell_usd"], m["net_usd"], m["smart_buyers"],
             m["avg_quality"], m["score"], tok["price_usd"] if tok else None, m["buy_sol"], m["net_sol"],
             tok["mcap_usd"] if tok else None, m["n_ultra"], m["n_smart"], m["n_good"], pred,
             json.dumps(m["contributions"])))
        conn.commit()

        def passes(m):
            return (m["score"] >= m.get("min_score", cfg.signal_min_score) and m["smart_buyers"] >= cfg.signal_min_wallets
                    and m["net_usd"] >= cfg.signal_min_net_usd)
        if not passes(m):
            continue
        m = check_before_signal(conn, mint, m)
        if not passes(m):
            log.info("signal %s dropped by safety check: %s", mint[:6], (m.get("safety") or {}).get("reasons"))
            continue
        contrib = json.dumps(m["contributions"])
        sp = m.get("safety") or {}
        last = conn.execute("SELECT ts, score FROM signals WHERE mint=? ORDER BY ts DESC LIMIT 1", (mint,)).fetchone()
        if last and now - last["ts"] < cfg.signal_cooldown_min * 60 and m["score"] < last["score"] + 15:
            continue
        cur = conn.execute(
            """INSERT INTO signals(ts, mint, symbol, score, net_usd, buy_usd, smart_buyers, avg_quality, price_usd,
                                   mcap_usd, wallets_json, buy_sol, net_sol, liquidity_usd, n_ultra, n_smart, n_good,
                                   prediction, prediction_label, contributions_json, model_version,
                                   score_raw, safety_factor, safety_risk, safety_level, safety_reasons_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (now, mint, tok["symbol"] if tok else "", m["score"], m["net_usd"], m["buy_usd"], m["smart_buyers"],
             m["avg_quality"], tok["price_usd"] if tok else None, tok["mcap_usd"] if tok else None,
             json.dumps(m["wallets"]), m["buy_sol"], m["net_sol"], tok["liquidity_usd"] if tok else None,
             m["n_ultra"], m["n_smart"], m["n_good"], pred, pred_label, contrib, m["model_version"],
             m.get("score_raw", m["score"]), sp.get("factor"), sp.get("risk"), sp.get("level"),
             json.dumps(sp.get("reasons") or [], ensure_ascii=False)))
        new.append(conn.execute("SELECT * FROM signals WHERE id=?", (cur.lastrowid,)).fetchone())
    conn.commit()
    return new


def update_outcomes(conn, now=None):
    """Fill ret_5m / ret_15m / ret_1h / max_ret_1h for recent signals from later prices."""
    now = now or int(time.time())
    for s in conn.execute("SELECT * FROM signals WHERE ts > ? AND (ret_1h IS NULL OR max_ret_1h IS NULL "
                          "OR max_ret_24h IS NULL)", (now - 26 * 3600,)).fetchall():
        p0 = s["price_usd"]
        if not p0:
            continue
        pts = conn.execute(
            """SELECT ts, price_usd FROM token_prices WHERE mint=? AND ts>? AND ts<=? AND price_usd>0
               UNION ALL SELECT ts, price_usd FROM trades WHERE mint=? AND ts>? AND ts<=? AND price_usd>0
               ORDER BY ts""", (s["mint"], s["ts"], s["ts"] + 86400, s["mint"], s["ts"], s["ts"] + 86400)).fetchall()
        h1 = [r for r in pts if r["ts"] <= s["ts"] + 3600]

        def at(dt):
            for r in pts:
                if r["ts"] >= s["ts"] + dt:
                    return r["price_usd"] / p0 - 1
            return None
        upd = {"ret_5m": at(300), "ret_15m": at(900), "ret_1h": at(3600) if now >= s["ts"] + 3600 else None,
               "ret_6h": at(6 * 3600) if now >= s["ts"] + 6 * 3600 else None}
        if h1 and now >= s["ts"] + 3600:
            upd["max_ret_1h"] = max(r["price_usd"] for r in h1) / p0 - 1
            if s["mcap_usd"]:
                upd["max_mcap_1h"] = s["mcap_usd"] * (1 + upd["max_ret_1h"])
        if pts and now >= s["ts"] + 86400:
            upd["max_ret_24h"] = max(r["price_usd"] for r in pts) / p0 - 1
        sets = {k: v for k, v in upd.items() if v is not None}
        if sets:
            conn.execute(f"UPDATE signals SET {', '.join(k + '=?' for k in sets)} WHERE id=?",
                         (*sets.values(), s["id"]))
    conn.commit()
