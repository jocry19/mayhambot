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
from signals import ev as ev_mod
from signals.backtest import DEAD_RET, price_near

log = logging.getLogger("mayhambot.signals")
MANUAL_DEFAULT_SCORE = 70.0


class QualityCache:
    def __init__(self, conn, cfg=settings):
        self.cfg = cfg
        self.positions = scoring.load_positions(conn)
        self.meta = {r["address"]: r for r in conn.execute("SELECT address, manual, is_bot, label FROM wallets")}
        self.model_version, self.model_tiers = scoring.model_tiers(conn)  # one truth once model/ is active
        self.sol_usd = recent_sol_usd(conn)
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


def recent_sol_usd(conn, n=200):
    """Median USD per SOL of the latest trades that carry both: fills usd for trades stored with SOL only
    (copytrade / wallet history while the SOL price fetch failed), so their inflow is not counted as $0."""
    xs = sorted(r[0] for r in conn.execute(
        "SELECT usd_amount / sol_amount FROM trades WHERE usd_amount > 0 AND sol_amount > 0 ORDER BY ts DESC LIMIT ?",
        (n,)))
    return xs[len(xs) // 2] if xs else 0.0


def min_net_usd(conn, cfg=settings, now=None):
    """Net inflow minimum scaled to how big Smart buys really are: min(SIGNAL_MIN_NET_USD, SIGNAL_MIN_WALLETS x
    median Ultra/Smart buy of the last 24h), never below SIGNAL_NET_FLOOR_USD. On the Mac a Smart buy is about
    $140, so a fixed $500 needed four buys and passed 4 of 139 windows with 2+ Smart buyers in 10 h."""
    now = now or int(time.time())
    xs = sorted(r[0] for r in conn.execute(
        """SELECT t.usd_amount FROM trades t JOIN wallets w ON w.address=t.wallet
           WHERE w.tier IN ('ultra','smart') AND t.side='buy' AND t.usd_amount > 0 AND t.ts > ?""",
        (now - 86400,)))
    if len(xs) < 20:
        return cfg.signal_min_net_usd
    return round(min(cfg.signal_min_net_usd, max(cfg.signal_net_floor_usd, cfg.signal_min_wallets * xs[len(xs) // 2])), 0)


def _usd(r, sol_usd):
    return r["usd_amount"] or (r["sol_amount"] or 0) * sol_usd


TIER_RANK = {"ultra": 3, "smart": 2, "good": 1}
last_cluster_stats = {"wallets": 0, "entities": 0}
FUNNEL_KEYS = ("coins", "no_smart", "few_entities", "low_net", "low_score", "low_ev", "safety_drop", "cooldown",
               "fired")
last_funnel = {}  # per evaluate(): where coins dropped out (first failing condition), see signals.funnel


EV_MIN_SAFETY_FACTOR = 0.6  # safety factor 1.0 up to risk 25, 0.2 at 75, 0 blocked: about risk 45 and above vetoes


def _eur(x):
    return f"{x:+.0%}".replace("%", " %")


def explain(m, cfg=settings):
    """Plain German why (and what speaks against it), stored with the signal and shown in alerts."""
    pro, con = [], []
    ent = m.get("entities", m["smart_buyers"])
    pro.append(f"{ent} Smart-Käufer {'kauft' if ent == 1 else 'kaufen'} netto ${m['net_usd']:,.0f}")
    if m.get("n_ultra"):
        pro.append(f"{m['n_ultra']} Ultra dabei")
    if m.get("prediction") is not None:
        (pro if m["prediction"] >= 0.3 else con).append(f"Modell: {m['prediction'] * 100:.0f} % Chance auf +50 % in 1 h")
    if m.get("model_exp_net") is not None:
        (pro if m["model_exp_net"] > 0 else con).append(
            f"Modell erwartet {_eur(m['model_exp_net'])} nach Kosten"
            + (f" ({m['model_p_net'] * 100:.0f} % Gewinnchance)" if m.get("model_p_net") is not None else ""))
    if m.get("ev") is not None:
        (pro if m["ev"] > 0 else con).append(
            f"ähnliche Setups brachten im Schnitt {_eur(m['ev'])} nach Kosten (n={m.get('ev_n') or 0})")
    if m.get("sellers"):
        con.append(f"{m['sellers']} Smart-Wallet{' verkauft' if m['sellers'] == 1 else 's verkaufen'} schon")
    sp = m.get("safety") or {}
    if sp.get("risk") is not None and sp.get("factor", 1.0) < 1.0:
        con.append(f"Rug-Risiko {sp['risk']:.0f}" + (f": {sp['reasons'][0]}" if sp.get("reasons") else ""))
    elif sp.get("checked") is False:
        con.append("Rug-Check steht noch aus")
    out = "Dafür: " + "; ".join(pro)
    if con:
        out += ". Dagegen: " + "; ".join(con)
    return out


def _reason(m, cfg, strict=False):
    """First failing condition or None. strict=True after check_before_signal: an unchecked coin is judged by its
    safety factor too (before that it still gets its quick check instead of an early veto)."""
    if m["smart_buyers"] <= 0:
        return "no_smart"
    if m.get("ev") is not None:  # an out-of-sample-checked EV table replaces wallets / net / score minimums
        sp = m.get("safety") or {}
        if sp.get("factor", 1.0) < EV_MIN_SAFETY_FACTOR and (strict or sp.get("checked") is not False):
            return "safety_drop"  # rug protection still vetoes: high risk, blocked, or still unchecked
        return None if m["ev"] > ev_mod.MIN_EV else "low_ev"
    if m["smart_buyers"] < cfg.signal_min_wallets:
        return "few_entities"
    if m["net_usd"] < m.get("min_net", cfg.signal_min_net_usd):
        return "low_net"
    if m["score"] < m.get("min_score", cfg.signal_min_score):
        return "low_score"
    return None


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
        usd = _usd(r, getattr(qc, "sol_usd", 0.0))
        if r["side"] == "buy":
            d["buy_usd"] += usd
            d["buy_sol"] += r["sol_amount"] or 0
        else:
            d["sell_usd"] += usd
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
    n_sellers = sum(1 for d in smart if d["sell_usd"] > d["buy_usd"])
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
                entities=n_smart_entities, sellers=n_sellers,
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
    m["model_p_net"], m["model_exp_net"] = h1.get("p_win_net"), h1.get("exp_net_ret")  # profit after costs
    m["smart_buyers"] = m["n_ultra"] + m["n_smart"]
    m["min_score"] = cfg_min_model(conn)
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


def _load_market():
    try:
        import market
        return market
    except Exception as e:  # noqa: BLE001 - signals keep working without market/
        log.info("market/ not available, no market weight: %s", e)
        return None


_market = _load_market()


def apply_market(conn, mint, m, now=None):
    """score x market.signal_factor (market phase + sector heat, 1 +- 0.15). Does nothing unless
    MARKET_SIGNAL_WEIGHT=true; adds a 'market' contribution so the points still add up to the score."""
    if not _market:
        return m
    try:
        f = _market.signal_factor(conn, mint, now)
    except Exception as e:  # noqa: BLE001
        log.warning("market.signal_factor %s failed: %s", mint[:6], e)
        return m
    if not f or f["factor"] == 1.0:
        return m
    before = m["score"]
    m = dict(m, market=f, score=round(before * f["factor"], 1))
    m["contributions"] = dict(m["contributions"], market=dict(
        weight=None, value=f["factor"], points=round(m["score"] - before, 1), why=f["why"]))
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
    return apply_market(conn, mint, apply_safety(conn, mint, m))


_model_thr = {"version": None, "value": None, "base": None}


def cfg_min_model(conn=None, cfg=settings):
    """Model score = P(pump within 1h) x 100, a calibrated probability: a fixed 40 needs 4x a 10% base rate and
    almost never fires. Threshold = min(MODEL_SIGNAL_MIN_SCORE, MODEL_SIGNAL_MIN_LIFT x the active version's
    backtested 1h base rate), never below MODEL_SIGNAL_FLOOR. Without a base rate: MODEL_SIGNAL_MIN_SCORE."""
    fixed = cfg.model_signal_min_score
    if conn is None:
        return fixed
    try:
        v = conn.execute("SELECT id, metrics_json FROM model_versions WHERE active=1 ORDER BY id DESC LIMIT 1").fetchone()
    except Exception:  # noqa: BLE001 - no model tables yet
        return fixed
    if v is None:
        return fixed
    if _model_thr["version"] != v[0]:
        try:
            base = ((json.loads(v[1] or "{}") or {}).get("1h") or {}).get("base_rate")
        except ValueError:
            base = None
        val = fixed if not base else max(cfg.model_signal_floor, min(fixed, round(100 * base * cfg.model_signal_min_lift, 1)))
        _model_thr.update(version=v[0], value=val, base=base)
    return _model_thr["value"]


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
    f = last_funnel
    f.clear()
    min_net = min_net_usd(conn, cfg, now)
    ev_table = ev_mod.active_table(conn)
    f.update({k: 0 for k in FUNNEL_KEYS}, max_score=0.0, min_score=None, min_net=min_net, ev_active=bool(ev_table),
             max_ev=None)
    for mint in mints:
        tok = conn.execute("SELECT symbol, price_usd, mcap_usd, liquidity_usd FROM tokens WHERE mint=?",
                           (mint,)).fetchone()
        m = apply_market(conn, mint, apply_safety(conn, mint, apply_model(conn, mint, now, token_metrics(conn, mint, qc, now, cfg))), now)
        m = dict(m, min_net=min_net)
        if ev_table:
            e, n, k = ev_mod.predict(ev_table, m.get("entities", m["smart_buyers"]), m["net_usd"], m.get("sellers", 0))
            e = ev_mod.blend(e, n, m.get("model_exp_net"))
            m.update(ev=e, ev_n=n, ev_bucket=k)
            f["max_ev"] = e if f["max_ev"] is None else max(f["max_ev"], e)
        m["reason"] = explain(m, cfg)
        if "prediction" in m:
            pred, pred_label = m["prediction"], m["prediction_label"]
        else:
            pred, pred_label = calibrated_prediction(conn, m["score"]) if m["score"] > 0 else (None, "")
        conn.execute(
            """INSERT OR REPLACE INTO signal_metrics(ts, mint, window_min, buy_usd, sell_usd, net_usd, smart_buyers,
                   avg_quality, score, price_usd, buy_sol, net_sol, mcap_usd, n_ultra, n_smart, n_good, prediction,
                   contributions_json, ev, model_exp_net) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (now, mint, m["window_min"], m["buy_usd"], m["sell_usd"], m["net_usd"], m["smart_buyers"],
             m["avg_quality"], m["score"], tok["price_usd"] if tok else None, m["buy_sol"], m["net_sol"],
             tok["mcap_usd"] if tok else None, m["n_ultra"], m["n_smart"], m["n_good"], pred,
             json.dumps(m["contributions"]), m.get("ev"), m.get("model_exp_net")))
        conn.commit()

        f["coins"] += 1
        f["max_score"] = max(f["max_score"], m["score"])
        f["min_score"] = m.get("min_score", cfg.signal_min_score)
        why = _reason(m, cfg)
        if why:
            f[why] += 1
            continue
        m = check_before_signal(conn, mint, m)
        if _reason(m, cfg, strict=True):
            f["safety_drop"] += 1
            log.info("signal %s dropped by safety check: %s", mint[:6], (m.get("safety") or {}).get("reasons"))
            continue
        contrib = json.dumps(m["contributions"])
        sp = m.get("safety") or {}
        last = conn.execute("SELECT ts, score FROM signals WHERE mint=? ORDER BY ts DESC LIMIT 1", (mint,)).fetchone()
        if last and now - last["ts"] < cfg.signal_cooldown_min * 60 and m["score"] < last["score"] + 15:
            f["cooldown"] += 1
            continue
        f["fired"] += 1
        cur = conn.execute(
            """INSERT INTO signals(ts, mint, symbol, score, net_usd, buy_usd, smart_buyers, avg_quality, price_usd,
                                   mcap_usd, wallets_json, buy_sol, net_sol, liquidity_usd, n_ultra, n_smart, n_good,
                                   prediction, prediction_label, contributions_json, model_version,
                                   score_raw, safety_factor, safety_risk, safety_level, safety_reasons_json,
                                   ev, ev_n, reason)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (now, mint, tok["symbol"] if tok else "", m["score"], m["net_usd"], m["buy_usd"], m["smart_buyers"],
             m["avg_quality"], tok["price_usd"] if tok else None, tok["mcap_usd"] if tok else None,
             json.dumps(m["wallets"]), m["buy_sol"], m["net_sol"], tok["liquidity_usd"] if tok else None,
             m["n_ultra"], m["n_smart"], m["n_good"], pred, pred_label, contrib, m["model_version"],
             m.get("score_raw", m["score"]), sp.get("factor"), sp.get("risk"), sp.get("level"),
             json.dumps(sp.get("reasons") or [], ensure_ascii=False), m.get("ev"), m.get("ev_n"), explain(m, cfg)))
        new.append(conn.execute("SELECT * FROM signals WHERE id=?", (cur.lastrowid,)).fetchone())
    conn.commit()
    return new


def update_outcomes(conn, now=None, cfg=settings):
    """Fill ret_5m / ret_15m / ret_1h / ret_6h / max_ret_1h / max_ret_24h for signals of the last 3 days.

    Unbiased on purpose: a return is the price within a tolerance around the horizon (2 min for 5m, 5 min for
    15m / 1h, 30 min for 6h), never an arbitrary later print. A coin that died (liquidity below a quarter of
    MIN_LIQUIDITY_USD) with no price near or after the horizon counts as DEAD_RET instead of dropping out of the
    stats, so rugs are not survivorship-filtered. outcome_flag: ok | dead | gap (no price near the horizon).
    Prices: token_prices (DexScreener) first; pool trade prints only where no snapshot is near."""
    now = now or int(time.time())
    dead_liq = cfg.min_liquidity_usd / 4
    for s in conn.execute("SELECT * FROM signals WHERE ts > ? AND (ret_1h IS NULL OR max_ret_1h IS NULL "
                          "OR max_ret_24h IS NULL OR ret_6h IS NULL)", (now - 72 * 3600,)).fetchall():
        p0 = s["price_usd"]
        if not p0:
            continue
        snap = [(r[0], r[1]) for r in conn.execute(
            "SELECT ts, price_usd FROM token_prices WHERE mint=? AND ts>? AND ts<=? AND price_usd>0 ORDER BY ts",
            (s["mint"], s["ts"], s["ts"] + 86400))]
        prints = [(r[0], r[1]) for r in conn.execute(
            "SELECT ts, price_usd FROM trades WHERE mint=? AND ts>? AND ts<=? AND price_usd>0 ORDER BY ts",
            (s["mint"], s["ts"], s["ts"] + 86400))]
        pts = sorted(snap + prints)
        tok = conn.execute("SELECT liquidity_usd FROM tokens WHERE mint=?", (s["mint"],)).fetchone()
        dead = bool(tok and tok[0] is not None and tok[0] < dead_liq)
        flags = []

        def ret(dt, tol):
            if now < s["ts"] + dt + tol:
                return None  # not due yet
            p = price_near(snap, s["ts"] + dt, tol)
            if p is None:
                p = price_near(pts, s["ts"] + dt, tol)
            if p is not None:
                return p / p0 - 1
            if dead and not any(t > s["ts"] + dt for t, _ in pts):
                flags.append("dead")
                return DEAD_RET
            flags.append("gap")
            return None
        upd = {"ret_5m": ret(300, 120), "ret_15m": ret(900, 300), "ret_1h": ret(3600, 300),
               "ret_6h": ret(6 * 3600, 1800)}
        h1 = [v for t, v in pts if t <= s["ts"] + 3600]
        if now >= s["ts"] + 3600:
            if h1:
                upd["max_ret_1h"] = max(h1) / p0 - 1
                if s["mcap_usd"]:
                    upd["max_mcap_1h"] = s["mcap_usd"] * (1 + upd["max_ret_1h"])
            elif upd["ret_1h"] == DEAD_RET:
                upd["max_ret_1h"] = DEAD_RET
        if now >= s["ts"] + 86400 and pts:
            upd["max_ret_24h"] = max(v for _, v in pts) / p0 - 1
        upd["outcome_flag"] = "dead" if "dead" in flags else "gap" if "gap" in flags else "ok"
        sets = {k: v for k, v in upd.items() if v is not None}
        conn.execute(f"UPDATE signals SET {', '.join(k + '=?' for k in sets)} WHERE id=?", (*sets.values(), s["id"]))
    conn.commit()
