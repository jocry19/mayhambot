"""All read models for the dashboard (web API) and the terminal (cli/).

Sources, all optional (see web/db.py):
  core       tokens, token_prices, trades, wallets, wallet_positions, signal_metrics, signals
  model      model_wallet_stats (tiers), model_predictions (coin prognoses), model_versions, research_*
  copytrade  ct_positions, ct_equity, ct_wallet_rank, ct_buy_outcomes, ct_alerts, ct_events, ct_control ...

Wallet tiers come from model/ (model_wallet_stats). Until the model has run once, a fallback applies
the same tier rules (model/README.md) to core's wallet score, marked as tier_source="fallback".
Every function returns plain JSON-able dicts/lists.
"""
from __future__ import annotations

import bisect
import json
import math
import os
import statistics
import time
from collections import defaultdict

from .db import cache, has, one, rows, scalar, tables

TIERS = ("ultra", "smart", "good")
TIER_LABELS = {"ultra": "Ultra Smart", "smart": "Smart", "good": "Good"}
PUMP_1H = float(os.getenv("WEB_PUMP_1H", "0.5"))  # a signal "hit" = max price within 1 h >= +50 %

# fallback tier rules, same thresholds as model/README.md, applied to core's wallet score
FALLBACK_RULES = [
    ("ultra", float(os.getenv("MODEL_ULTRA_MIN_SCORE", 72)), int(os.getenv("MODEL_ULTRA_MIN_TOKENS", 6)),
     float(os.getenv("MODEL_ULTRA_MIN_WINRATE", 0.45))),
    ("smart", float(os.getenv("MODEL_SMART_MIN_SCORE", 60)), int(os.getenv("MODEL_SMART_MIN_TOKENS", 4)),
     float(os.getenv("MODEL_SMART_MIN_WINRATE", 0.35))),
    ("good", float(os.getenv("MODEL_GOOD_MIN_SCORE", 48)), int(os.getenv("MODEL_GOOD_MIN_TOKENS", 3)),
     float(os.getenv("MODEL_GOOD_MIN_WINRATE", 0.25))),
]


def _j(s, default=None):
    if s in (None, ""):
        return default
    try:
        return json.loads(s)
    except (TypeError, ValueError):
        return default


def _f(x, nd=None):
    if x is None:
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(x) or math.isinf(x):
        return None
    return round(x, nd) if nd is not None else x


def grade(score):
    """Same cut-offs as model.predict (score = P(pump 1 h) x 100)."""
    if score is None:
        return None
    return "A" if score >= 60 else "B" if score >= 45 else "C" if score >= 30 else "D" if score >= 15 else "E"


def now_ts() -> int:
    return int(time.time())


# ====================================================================== basics
def sol_usd(conn) -> float:
    def calc():
        r = rows(conn, """SELECT usd_amount, sol_amount FROM trades WHERE sol_amount>0 AND usd_amount>0
                          ORDER BY ts DESC LIMIT 300""")
        xs = [a["usd_amount"] / a["sol_amount"] for a in r if a["sol_amount"]]
        xs = [x for x in xs if 20 < x < 2000]
        return statistics.median(xs) if xs else 150.0
    return cache.get(("sol_usd",), 60, calc)


def tier_source(conn) -> str:
    """model = model/ tiers, core = wallets.tier set by core/signals, fallback = README rules on core score."""
    def calc():
        if has(conn, "model_wallet_stats") and scalar(conn, "SELECT COUNT(*) FROM model_wallet_stats WHERE tier IS NOT NULL", default=0):
            return "model"
        if has(conn, "wallets", "tier") and scalar(conn, "SELECT COUNT(*) FROM wallets WHERE tier IS NOT NULL", default=0):
            return "core"
        return "fallback"
    return cache.get(("tier_source",), 30, calc)


def tier_map(conn) -> dict[str, dict]:
    """{address: {tier, score, influence_followers, influence_usd}} for every tiered wallet."""
    def calc():
        out = {}
        if tier_source(conn) == "model":
            for r in rows(conn, """SELECT address, tier, score, influence_followers, influence_usd, inflow_lift_usd
                                   FROM model_wallet_stats WHERE tier IS NOT NULL"""):
                out[r["address"]] = r
            return out
        if not has(conn, "wallets"):
            return out
        if tier_source(conn) == "core":
            for r in rows(conn, "SELECT address, tier, score FROM wallets WHERE tier IS NOT NULL"):
                out[r["address"]] = r
            return out
        for r in rows(conn, """SELECT address, score, tokens_traded, win_rate, total_pnl_usd, is_bot, manual
                               FROM wallets WHERE score>0 AND COALESCE(is_bot,0)=0"""):
            for name, smin, nmin, wmin in FALLBACK_RULES:
                if ((r["score"] or 0) >= smin and (r["tokens_traded"] or 0) >= nmin
                        and (r["win_rate"] or 0) >= wmin and (r["total_pnl_usd"] or 0) > 0):
                    out[r["address"]] = {"address": r["address"], "tier": name, "score": r["score"]}
                    break
        return out
    return cache.get(("tier_map",), 30, calc)


def model_active(conn) -> dict | None:
    if not has(conn, "model_versions"):
        return None
    r = one(conn, "SELECT * FROM model_versions WHERE active=1 ORDER BY id DESC LIMIT 1") or \
        one(conn, "SELECT * FROM model_versions ORDER BY id DESC LIMIT 1")
    if not r:
        return None
    v = _norm_version(r)
    v.pop("coefs", None)
    v.pop("metrics", None)
    return v


def _supply(tok) -> float | None:
    if tok and tok.get("price_usd") and tok.get("mcap_usd"):
        return tok["mcap_usd"] / tok["price_usd"]
    return None


# ====================================================================== flows by tier
def _trades_since(conn, since, mints=None, until=None):
    until = until or now_ts() + 60
    if mints is not None:
        mints = list(mints)
        if not mints:
            return []
        out = []
        for i in range(0, len(mints), 400):
            chunk = mints[i:i + 400]
            q = ",".join("?" * len(chunk))
            out += rows(conn, f"""SELECT wallet, mint, side, ts, usd_amount, sol_amount, price_usd FROM trades
                                  WHERE mint IN ({q}) AND ts>=? AND ts<=?""", (*chunk, since, until))
        return out
    return rows(conn, """SELECT wallet, mint, side, ts, usd_amount, sol_amount, price_usd FROM trades
                         WHERE ts>=? AND ts<=?""", (since, until))


def _tier(tm, wallet):
    t = tm.get(wallet)
    return t["tier"] if t else "other"


def flow_series(conn, since, bucket, mint=None):
    """Net USD inflow per bucket and tier (+ 'other' = everybody else) and distinct tiered buyers."""
    tm = tier_map(conn)
    trades = _trades_since(conn, since, [mint] if mint else None)
    b = defaultdict(lambda: {"ultra": 0.0, "smart": 0.0, "good": 0.0, "other": 0.0, "buyers": set(),
                             "buy_usd": 0.0, "sell_usd": 0.0})
    for t in trades:
        k = (t["ts"] // bucket) * bucket
        tier = _tier(tm, t["wallet"])
        usd = t["usd_amount"] or 0.0
        sgn = 1 if t["side"] == "buy" else -1
        b[k][tier] += sgn * usd
        if tier != "other":
            if t["side"] == "buy":
                b[k]["buyers"].add(t["wallet"])
                b[k]["buy_usd"] += usd
            else:
                b[k]["sell_usd"] += usd
    start = (since // bucket) * bucket
    end = (now_ts() // bucket) * bucket if not mint else max(b) if b else start
    out = []
    k = start
    while k <= end:
        d = b.get(k)
        out.append({"ts": k, "ultra": round(d["ultra"], 2) if d else 0, "smart": round(d["smart"], 2) if d else 0,
                    "good": round(d["good"], 2) if d else 0, "other": round(d["other"], 2) if d else 0,
                    "tiered_buyers": len(d["buyers"]) if d else 0})
        k += bucket
        if len(out) > 2000:
            break
    return out


def _mint_flows(conn, since, mints):
    """Per mint: tiered net usd, per-tier net usd, distinct tiered net buyers, all buy/sell usd."""
    tm = tier_map(conn)
    per = defaultdict(lambda: {"net_tiered": 0.0, "ultra": 0.0, "smart": 0.0, "good": 0.0, "buy_all": 0.0,
                               "sell_all": 0.0, "w": defaultdict(float), "wt": {}, "traders": set()})
    for t in _trades_since(conn, since, mints):
        d = per[t["mint"]]
        usd = t["usd_amount"] or 0.0
        sgn = 1 if t["side"] == "buy" else -1
        d["traders"].add(t["wallet"])
        if sgn > 0:
            d["buy_all"] += usd
        else:
            d["sell_all"] += usd
        tier = _tier(tm, t["wallet"])
        if tier == "other":
            continue
        d["net_tiered"] += sgn * usd
        d[tier] += sgn * usd
        d["w"][t["wallet"]] += sgn * usd
        d["wt"][t["wallet"]] = tier
    out = {}
    for m, d in per.items():
        buyers = {"ultra": 0, "smart": 0, "good": 0}
        for w, v in d["w"].items():
            if v > 0:
                buyers[d["wt"][w]] += 1
        out[m] = {"net_tiered_usd": round(d["net_tiered"], 2),
                  "net_by_tier": {k: round(d[k], 2) for k in TIERS}, "buyers": buyers,
                  "buy_usd": round(d["buy_all"], 2), "sell_usd": round(d["sell_all"], 2),
                  "traders": len(d["traders"])}
    return out


# ====================================================================== signals (engine + model)
def _engine_breakdown(s):
    """Inputs of the engine's heuristic score, recomputed for signals stored without contributions_json."""
    try:
        from core.config import settings as cs  # type: ignore
        inflow_ref, wallets_ref = cs.inflow_ref_usd, cs.wallets_ref_count
    except Exception:
        inflow_ref, wallets_ref = 5000.0, 4.0
    net = s.get("net_usd") or 0.0
    n = s.get("smart_buyers") or 0
    q = s.get("avg_quality") or 0.0
    return {"inflow": {"weight": 0.4, "value": 1 - math.exp(-max(net, 0) / inflow_ref), "why": f"Netto-Inflow Smart ${net:,.0f}"},
            "breadth": {"weight": 0.3, "value": 1 - math.exp(-n / wallets_ref), "why": f"{n} Smart Wallets kaufen"},
            "quality": {"weight": 0.3, "value": q / 100, "why": f"Ø Wallet-Score {q:.0f}"}}


FEATURE_LABELS = {"inflow": "Netto-Inflow Smart Money", "breadth": "Anzahl Smart Wallets", "quality": "Ø Wallet-Qualität",
                  "safety": "Abzug Rug-Schutz"}


def breakdown_from(contrib, score=None):
    """Normalize contributions_json (core dict form or model list form) to a sorted list.

    Each item: feature, label, why, weight, value, points (share of the score), contribution, share.
    """
    items = []
    if isinstance(contrib, dict) and contrib and all(isinstance(v, dict) for v in contrib.values()):
        for k, c in contrib.items():
            items.append({"feature": k, "label": FEATURE_LABELS.get(k, c.get("why") or k), "why": c.get("why"),
                          "weight": _f(c.get("weight"), 4), "value": _f(c.get("value"), 4),
                          "points": _f(c.get("points"), 2), "contribution": _f(c.get("contribution"), 4),
                          "share": _f(c.get("share"), 4)})
    elif isinstance(contrib, dict) and contrib:
        lst = contrib.get("1h") or next(iter(contrib.values()), [])
        return breakdown_from(lst, score)
    elif isinstance(contrib, list):
        for c in contrib:
            items.append({"feature": c.get("feature"), "label": c.get("label") or c.get("feature"), "why": c.get("label"),
                          "weight": _f(c.get("weight"), 4), "value": _f(c.get("value"), 4),
                          "points": None,  # logistic model: contributions are log-odds, not additive score points
                          "contribution": _f(c.get("contribution"), 4), "share": _f(c.get("share"), 4)})
    tot = sum(abs(i["points"] or 0) for i in items)
    for i in items:
        if i["share"] is None and tot:
            i["share"] = _f(abs(i["points"] or 0) / tot, 4)
    items.sort(key=lambda i: -abs(i["points"] if i["points"] is not None else (i["contribution"] or 0)))
    return items


def _norm_signal(conn, s, tm):
    wl = _j(s.get("wallets_json"), []) or []
    wallets = []
    for w in wl:
        addr = w.get("wallet") or w.get("address")
        tier = w.get("tier") or (tm.get(addr) or {}).get("tier")
        wallets.append({"address": addr, "tier": tier, "score": _f(w.get("score"), 1),
                        "buy_usd": _f(w.get("buy_usd"), 2), "buy_sol": _f(w.get("buy_sol"), 3),
                        "net_usd": _f((w.get("buy_usd") or 0) - (w.get("sell_usd") or 0), 2),
                        "first_ts": w.get("first_ts")})
    if s.get("n_ultra") is not None or s.get("n_smart") is not None:
        tiers = {"ultra": s.get("n_ultra") or 0, "smart": s.get("n_smart") or 0, "good": s.get("n_good") or 0}
    else:
        tiers = {"ultra": 0, "smart": 0, "good": 0}
        for w in wallets:
            if w["tier"] in tiers:
                tiers[w["tier"]] += 1
    contrib = _j(s.get("contributions_json"), None) or _engine_breakdown(s)
    mv = s.get("model_version") or "core-heuristic-v1"
    return {"kind": "signal", "id": s["id"], "ts": s["ts"], "mint": s["mint"], "symbol": s.get("symbol") or "",
            "score": _f(s.get("score"), 1), "grade": grade(s.get("score")), "price_usd": s.get("price_usd"),
            "mcap_usd": _f(s.get("mcap_usd"), 0), "liquidity_usd": _f(s.get("liquidity_usd"), 0),
            "inflow_usd": _f(s.get("net_usd"), 2), "inflow_sol": _f(s.get("net_sol"), 3),
            "buy_usd": _f(s.get("buy_usd"), 2), "buy_sol": _f(s.get("buy_sol"), 3),
            "smart_buyers": s.get("smart_buyers") or 0, "wallets_total": sum(tiers.values()), "tiers": tiers,
            "avg_quality": _f(s.get("avg_quality"), 1),
            "prediction": _f(s.get("prediction"), 4), "prediction_label": s.get("prediction_label") or "",
            "model_version": mv, "is_model": mv.startswith("model"),
            "breakdown": breakdown_from(contrib, s.get("score")), "wallets": wallets, "notified": bool(s.get("notified")),
            "score_raw": _f(s.get("score_raw"), 1),
            # rug protection when the signal fired (signals/ stores it; NULL level = safety/ not installed then)
            "safety": {"level": s.get("safety_level"), "score": _f(s.get("safety_risk"), 1),
                       "factor": _f(s.get("safety_factor"), 3), "blocked": (s.get("safety_factor") == 0),
                       "reasons": _j(s.get("safety_reasons_json"), []) or []} if s.get("safety_level") else None,
            "outcome": {k: _f(s.get(k), 4) for k in ("ret_5m", "ret_15m", "ret_1h", "ret_6h", "max_ret_1h",
                                                      "max_ret_24h", "max_mcap_1h")}}


def _norm_prediction(p, sol):
    wallets = []
    for w in _j(p.get("wallets_json"), []) or []:
        wallets.append({"address": w.get("address") or w.get("wallet"), "tier": w.get("tier"),
                        "score": _f(w.get("score"), 1), "net_sol": _f(w.get("net_sol"), 3),
                        "net_usd": _f((w.get("net_sol") or 0) * sol, 2), "first_ts": w.get("first_ts")})
    tiers = {"ultra": p.get("n_ultra") or 0, "smart": p.get("n_smart") or 0, "good": p.get("n_good") or 0}
    net_sol = p.get("net_sol")
    return {"kind": "prediction", "id": p.get("id"), "ts": p.get("ts"), "mint": p.get("mint"), "symbol": p.get("symbol") or "",
            "score": _f(p.get("score"), 1), "grade": grade(p.get("score")), "price_usd": p.get("price_usd"),
            "mcap_usd": _f(p.get("mcap_usd"), 0), "inflow_sol": _f(net_sol, 3),
            "inflow_usd": _f((net_sol or 0) * sol, 2) if net_sol is not None else None,
            "wallets_total": sum(tiers.values()), "tiers": tiers, "version": p.get("version"),
            "p_15m": _f(p.get("p_15m"), 4), "p_1h": _f(p.get("p_1h"), 4), "p_6h": _f(p.get("p_6h"), 4),
            "exp_ret_1h": _f(p.get("exp_ret_1h"), 4), "features": _j(p.get("features_json"), {}),
            "breakdown": breakdown_from(_j(p.get("contributions_json"), {}), p.get("score")), "wallets": wallets,
            "outcome": {k: _f(p.get(k), 4) for k in ("ret_15m", "ret_1h", "ret_6h", "max_ret_1h", "max_ret_6h")}}


def signals(conn, since=None, mint=None, min_score=0.0, limit=300, tier=None, min_wallets=0):
    """Fired signals of the signal engine (table `signals`), newest first, normalized for the UI."""
    if not has(conn, "signals"):
        return []
    tm = tier_map(conn)
    q, a = "SELECT * FROM signals WHERE ts>=? AND score>=?", [since or 0, min_score]
    if mint:
        q += " AND mint=?"
        a.append(mint)
    out = [_norm_signal(conn, s, tm) for s in rows(conn, q + " ORDER BY ts DESC LIMIT ?", (*a, limit))]
    if tier in TIERS:
        out = [s for s in out if s["tiers"][tier] > 0]
    if min_wallets:
        out = [s for s in out if s["wallets_total"] >= min_wallets]
    sm = safety_map(conn, [s["mint"] for s in out])
    for s in out:  # current state too: the buy button and "gesperrt" follow it, not the state at the signal
        s["safety_now"] = sm.get(s["mint"])
    return out


def predictions(conn, mint, since=0, limit=2000):
    """Stored model prognoses for one coin (model_predictions), oldest first."""
    if not has(conn, "model_predictions"):
        return []
    sol = sol_usd(conn)
    r = rows(conn, "SELECT * FROM model_predictions WHERE mint=? AND ts>=? ORDER BY ts DESC LIMIT ?", (mint, since, limit))
    return [_norm_prediction(p, sol) for p in reversed(r)]


def signal_performance(conn, since=None):
    """How good were past signals: hit rate and returns per score bucket, per tier mix, per market cap."""
    items = [x for x in signals(conn, since=since, limit=20000) if x["outcome"].get("max_ret_1h") is not None]

    def grp(xs):
        hits = sum(1 for x in xs if x["outcome"]["max_ret_1h"] >= PUMP_1H)
        r1 = [x["outcome"]["ret_1h"] for x in xs if x["outcome"].get("ret_1h") is not None]
        mx = [x["outcome"]["max_ret_1h"] for x in xs]
        return {"n": len(xs), "hits": hits, "hit_rate": _f(hits / len(xs), 3) if xs else None,
                "median_ret_1h": _f(statistics.median(r1), 4) if r1 else None,
                "median_max_1h": _f(statistics.median(mx), 4) if mx else None}
    by_bucket = [{"bucket": f"{lo}–{min(hi, 100)}", **grp([x for x in items if lo <= (x["score"] or 0) < hi])}
                 for lo, hi in [(0, 50), (50, 60), (60, 70), (70, 80), (80, 90), (90, 101)]]
    by_ultra = [{"group": lab, **grp([x for x in items if f(x)])} for lab, f in
                (("0 Ultra", lambda x: x["tiers"]["ultra"] == 0), ("1 Ultra", lambda x: x["tiers"]["ultra"] == 1),
                 ("2+ Ultra", lambda x: x["tiers"]["ultra"] >= 2))]
    by_wallets = [{"group": lab, **grp([x for x in items if lo <= x["wallets_total"] < hi])} for lab, lo, hi in
                  (("2–3", 0, 4), ("4–5", 4, 6), ("6–9", 6, 10), ("10+", 10, 10**6))]
    by_mcap = [{"group": lab, **grp([x for x in items if lo <= (x["mcap_usd"] or 0) < hi])} for lab, lo, hi in
               (("< 50k", 0, 5e4), ("50–250k", 5e4, 2.5e5), ("250k–1M", 2.5e5, 1e6), ("1–10M", 1e6, 1e7), ("> 10M", 1e7, 1e15))]
    scatter = [{"id": x["id"], "symbol": x["symbol"], "mint": x["mint"], "score": x["score"],
                "max_ret_1h": _f(x["outcome"]["max_ret_1h"], 4), "mcap_usd": x["mcap_usd"],
                "hit": x["outcome"]["max_ret_1h"] >= PUMP_1H} for x in items[:2000]]
    # daily hit rate = "does it get better over time"
    days = defaultdict(list)
    for x in items:
        days[x["ts"] // 86400 * 86400].append(x)
    daily = [{"ts": d, **grp(xs)} for d, xs in sorted(days.items())]
    return {"pump_threshold": PUMP_1H, **grp(items), "by_bucket": by_bucket, "by_ultra": by_ultra,
            "by_wallets": by_wallets, "by_mcap": by_mcap, "scatter": scatter, "daily": daily}


# ====================================================================== safety (rug / honeypot / insider)
SAFETY_LEVEL_DE = {"low": "niedrig", "caution": "erhöht", "high": "hoch", "critical": "kritisch", "unchecked": "ungeprüft"}


def _overrides(conn, mints=None):
    if not has(conn, "safety_overrides"):
        return {}
    if mints is None:
        rs = rows(conn, "SELECT * FROM safety_overrides")
    else:
        ms = list(mints)
        rs = []
        for i in range(0, len(ms), 500):
            chunk = ms[i:i + 500]
            rs += rows(conn, f"SELECT * FROM safety_overrides WHERE mint IN ({','.join('?' * len(chunk))})", chunk)
    return {r["mint"]: r for r in rs if r.get("action") in ("block", "allow")}


def safety_map(conn, mints):
    """Short risk info per mint for badges: {mint: {score, level, blocked, override, reasons[:3], ts}}.
    Manual overrides win over the automatic result (same rule as safety.is_blocked)."""
    ms = list({m for m in mints if m})
    if not ms:
        return {}
    out = {}
    if has(conn, "safety_tokens"):
        for i in range(0, len(ms), 500):
            chunk = ms[i:i + 500]
            for r in rows(conn, f"""SELECT mint, ts, score, level, blocked, reasons_json FROM safety_tokens
                                    WHERE mint IN ({','.join('?' * len(chunk))})""", chunk):
                out[r["mint"]] = {"score": _f(r["score"], 1), "level": r["level"], "blocked": bool(r["blocked"]),
                                  "override": None, "ts": r["ts"],
                                  "reasons": [x.get("text") for x in (_j(r["reasons_json"], []) or [])[:3] if isinstance(x, dict)]}
    for m, o in _overrides(conn, ms).items():
        d = out.setdefault(m, {"score": None, "level": "unchecked", "blocked": False, "reasons": [], "ts": None})
        d["override"] = o["action"]
        d["blocked"] = o["action"] == "block"
    return out


def safety_detail(conn, mint):
    """Full stored assessment for the coin page, with manual override and score history. No network."""
    ov = _overrides(conn, [mint]).get(mint)
    row = one(conn, "SELECT * FROM safety_tokens WHERE mint=?", (mint,)) if has(conn, "safety_tokens") else None
    hist = rows(conn, "SELECT ts, score, level FROM safety_history WHERE mint=? ORDER BY ts", (mint,)) \
        if has(conn, "safety_history") else []
    if not row:
        return {"checked": False, "level": "unchecked", "level_de": SAFETY_LEVEL_DE["unchecked"],
                "blocked": bool(ov and ov["action"] == "block"), "override": ov, "history": hist,
                "available": has(conn, "safety_tokens")}
    checks = _j(row.get("checks_json"), {}) or {}
    blocked = bool(row["blocked"])
    if ov:
        blocked = ov["action"] == "block"
    return {"checked": True, "available": True, "ts": row["ts"], "score": _f(row["score"], 1), "level": row["level"],
            "level_de": SAFETY_LEVEL_DE.get(row["level"], row["level"]), "blocked": blocked,
            "auto_blocked": bool(row["blocked"]), "override": ov, "coverage": _f(row.get("coverage"), 3),
            "hard_flags": _j(row.get("hard_flags_json"), []) or [], "reasons": _j(row.get("reasons_json"), []) or [],
            "positives": _j(row.get("positives_json"), []) or [], "creator": row.get("creator"),
            "version": row.get("version"), "error": row.get("error"),
            # per check without the raw data blob (can be large and carries wallet lists)
            "checks": [{"check": k, "label": v.get("label") or k, "status": v.get("status"), "risk": _f(v.get("risk"), 1),
                        "hard": bool(v.get("hard"))} for k, v in checks.items() if isinstance(v, dict)],
            "history": hist}


def safety_list(conn):
    """Blocked coins and manual decisions for the control page."""
    ovs = _overrides(conn)
    auto = rows(conn, "SELECT mint FROM safety_tokens WHERE blocked=1 ORDER BY ts DESC LIMIT 300") \
        if has(conn, "safety_tokens") else []
    mints = list(dict.fromkeys([r["mint"] for r in auto] + list(ovs)))
    sm = safety_map(conn, mints)
    syms = {}
    if mints and has(conn, "tokens"):
        syms = {r["mint"]: r["symbol"] for r in rows(conn, f"SELECT mint, symbol FROM tokens WHERE mint IN ({','.join('?' * len(mints))})", mints)}
    counts = {}
    if has(conn, "safety_tokens"):
        counts = {r["level"]: r["n"] for r in rows(conn, "SELECT level, COUNT(*) n FROM safety_tokens GROUP BY level")}
    items = [{"mint": m, "symbol": syms.get(m), **sm.get(m, {}), "override_reason": (ovs.get(m) or {}).get("reason"),
              "override_ts": (ovs.get(m) or {}).get("ts")} for m in mints]
    return {"available": has(conn, "safety_tokens"), "counts": counts, "items": items}


# ====================================================================== coins
def coins(conn, sort="strength", limit=100, q=None, window_min=30):
    if not has(conn, "tokens"):
        return []
    now = now_ts()

    def calc():
        toks = {t["mint"]: t for t in rows(conn, "SELECT * FROM tokens")}
        # coins that ever had a signal or a prognosis, plus tracked ones
        last_metric = {}
        if has(conn, "signal_metrics"):
            for r in rows(conn, """SELECT m.* FROM signal_metrics m
                                   JOIN (SELECT mint, MAX(ts) ts FROM signal_metrics GROUP BY mint) x
                                   ON x.mint=m.mint AND x.ts=m.ts"""):
                last_metric[r["mint"]] = r
        last_pred = {}
        if has(conn, "model_predictions"):
            for r in rows(conn, """SELECT p.* FROM model_predictions p JOIN
                                   (SELECT mint, MAX(id) id FROM model_predictions GROUP BY mint) x ON x.id=p.id"""):
                last_pred[r["mint"]] = r
        sig_stats = defaultdict(lambda: {"n": 0, "last": None, "first_mcap": None, "best": None})
        if has(conn, "signals"):
            for r in rows(conn, "SELECT mint, ts, score, mcap_usd FROM signals ORDER BY ts"):
                d = sig_stats[r["mint"]]
                d["n"] += 1
                d["last"] = r["ts"]
                d["first_mcap"] = d["first_mcap"] or r["mcap_usd"]
                d["best"] = max(d["best"] or 0, r["score"] or 0)
        mints = [m for m, t in toks.items() if t.get("tracked") or m in sig_stats or m in last_pred]
        f_win = _mint_flows(conn, now - window_min * 60, mints)
        f_24 = _mint_flows(conn, now - 86400, mints)
        out = []
        for m in mints:
            t = toks[m]
            lp = last_pred.get(m)
            lm = last_metric.get(m)
            fresh_metric = lm if lm and now - lm["ts"] < 3 * 3600 else None
            strength = (fresh_metric or {}).get("score") or 0
            pred = (fresh_metric or {}).get("prediction")
            if pred is None and lp and now - lp["ts"] < 3 * 3600:
                pred = lp.get("p_1h")
            fw, f24 = f_win.get(m, {}), f_24.get(m, {})
            ss = sig_stats.get(m, {"n": 0, "last": None, "first_mcap": None, "best": None})
            out.append({
                "mint": m, "symbol": t.get("symbol") or m[:6], "name": t.get("name") or "",
                "price_usd": t.get("price_usd"), "mcap_usd": _f(t.get("mcap_usd"), 0),
                "liquidity_usd": _f(t.get("liquidity_usd"), 0), "volume_h1": _f(t.get("volume_h1"), 0),
                "change_m5": _f(t.get("price_change_m5"), 2), "change_h1": _f(t.get("price_change_h1"), 2),
                "change_h24": _f(t.get("price_change_h24"), 2),
                "age_h": _f((now - t["pair_created_at"]) / 3600, 1) if t.get("pair_created_at") else None,
                "last_updated": t.get("last_updated"),
                "strength": _f(strength, 1), "grade": grade(strength),
                "metric_score": _f(lm["score"], 1) if lm else None, "metric_ts": lm["ts"] if lm else None,
                "metric_tiers": {"ultra": lm.get("n_ultra") or 0, "smart": lm.get("n_smart") or 0,
                                 "good": lm.get("n_good") or 0} if lm else None,
                "p_1h": _f(pred, 4),
                "net_tiered_win": fw.get("net_tiered_usd", 0.0), "buyers_win": fw.get("buyers", {"ultra": 0, "smart": 0, "good": 0}),
                "net_tiered_24h": f24.get("net_tiered_usd", 0.0), "net_by_tier_24h": f24.get("net_by_tier", {}),
                "buyers_24h": f24.get("buyers", {"ultra": 0, "smart": 0, "good": 0}),
                "traders_24h": f24.get("traders", 0), "volume_24h": _f((f24.get("buy_usd", 0) + f24.get("sell_usd", 0)), 0),
                "signals": ss["n"], "last_signal_ts": ss["last"], "first_signal_mcap": _f(ss["first_mcap"], 0),
                "best_score": _f(ss["best"], 1),
                "since_first_signal": _f(t["mcap_usd"] / ss["first_mcap"] - 1, 4) if ss["first_mcap"] and t.get("mcap_usd") else None,
            })
        return out
    data = cache.get(("coins", window_min), 15, calc)
    sm = safety_map(conn, [c["mint"] for c in data])
    data = [{**c, "safety": sm.get(c["mint"])} for c in data]
    if q:
        ql = q.lower()
        data = [c for c in data if ql in (c["symbol"] or "").lower() or ql in (c["name"] or "").lower() or ql in c["mint"].lower()]
    keyf = {
        "strength": lambda c: (c["strength"] or 0, c["net_tiered_win"]),
        "inflow": lambda c: c["net_tiered_win"],
        "inflow24": lambda c: c["net_tiered_24h"],
        "wallets": lambda c: sum(c["buyers_24h"].values()),
        "signals": lambda c: (c["signals"], c["last_signal_ts"] or 0),
        "recent": lambda c: c["last_signal_ts"] or 0,
        "mcap": lambda c: c["mcap_usd"] or 0,
        "change": lambda c: c["change_h1"] or -1e9,
    }.get(sort, lambda c: c["strength"] or 0)
    return sorted(data, key=keyf, reverse=True)[:limit]


def _price_points(conn, mint, since, tok):
    """[ts, price_usd] from candles (best), else trades + DexScreener snapshots, 1 point per minute."""
    pts = []
    if has(conn, "research_candles"):
        pts = [(r["ts"], r["c"]) for r in rows(conn, """SELECT ts, c FROM research_candles WHERE mint=? AND tf_sec=60
                                                        AND ts>=? ORDER BY ts""", (mint, since)) if r["c"]]
    if len(pts) < 5:
        raw = rows(conn, """SELECT ts, price_usd FROM trades WHERE mint=? AND ts>=? AND price_usd>0
                            UNION ALL SELECT ts, price_usd FROM token_prices WHERE mint=? AND ts>=? AND price_usd>0
                            ORDER BY ts""", (mint, since, mint, since))
        per = defaultdict(list)
        for r in raw:
            per[r["ts"] // 60 * 60].append(r["price_usd"])
        pts = [(k, statistics.median(v)) for k, v in sorted(per.items())]
        # drop single-trade outliers (bad price parses): > 4x away from the rolling median
        if len(pts) > 6:
            clean = []
            for i, (k, p) in enumerate(pts):
                win = [x for _, x in pts[max(0, i - 5): i + 6]]
                med = statistics.median(win)
                if med and 0.25 < p / med < 4:
                    clean.append((k, p))
            pts = clean
    sup = _supply(tok)
    return [{"ts": k, "price_usd": p, "mcap_usd": _f(p * sup, 0) if sup else None} for k, p in pts]


def coin_detail(conn, mint):
    tok = one(conn, "SELECT * FROM tokens WHERE mint=?", (mint,)) if has(conn, "tokens") else None
    tm = tier_map(conn)
    sigs = signals(conn, mint=mint, limit=1000)
    first_trade = scalar(conn, "SELECT MIN(ts) FROM trades WHERE mint=?", (mint,), None)
    last_trade = scalar(conn, "SELECT MAX(ts) FROM trades WHERE mint=?", (mint,), None)
    t0 = min([x for x in [first_trade, *(s["ts"] for s in sigs)] if x] or [now_ts() - 86400])
    t0 = max(t0 - 600, now_ts() - 7 * 86400)
    price = _price_points(conn, mint, t0, tok)
    span = max(60, (max(last_trade or now_ts(), now_ts() if tok and tok.get("tracked") else 0) - t0))
    bucket = 60 if span <= 4 * 3600 else 300 if span <= 24 * 3600 else 900 if span <= 72 * 3600 else 3600
    flows = flow_series(conn, t0, bucket, mint=mint)
    sup = _supply(tok)

    # wallets in this coin
    per = {}
    for t in rows(conn, "SELECT wallet, side, ts, usd_amount, price_usd FROM trades WHERE mint=? ORDER BY ts", (mint,)):
        d = per.setdefault(t["wallet"], {"address": t["wallet"], "buy_usd": 0.0, "sell_usd": 0.0, "trades": 0,
                                         "first_ts": None, "first_price": None, "last_ts": t["ts"]})
        d["trades"] += 1
        d["last_ts"] = t["ts"]
        if t["side"] == "buy":
            d["buy_usd"] += t["usd_amount"] or 0
            if d["first_ts"] is None:
                d["first_ts"], d["first_price"] = t["ts"], t["price_usd"]
        else:
            d["sell_usd"] += t["usd_amount"] or 0
    wp = {r["wallet"]: r for r in rows(conn, "SELECT * FROM wallet_positions WHERE mint=?", (mint,))} if has(conn, "wallet_positions") else {}
    wl = []
    for w, d in per.items():
        tier = _tier(tm, w)
        p = wp.get(w) or {}
        wl.append({**d, "tier": tier if tier != "other" else None, "score": _f((tm.get(w) or {}).get("score"), 1),
                   "net_usd": _f(d["buy_usd"] - d["sell_usd"], 2), "buy_usd": _f(d["buy_usd"], 2), "sell_usd": _f(d["sell_usd"], 2),
                   "entry_mcap_usd": _f(d["first_price"] * sup, 0) if sup and d["first_price"] else None,
                   "pnl_usd": _f((p.get("realized_pnl_usd") or 0) + (p.get("unrealized_pnl_usd") or 0), 2) if p else None,
                   "roi": _f(p.get("roi"), 4) if p else None})
    order = {"ultra": 0, "smart": 1, "good": 2, None: 3}
    wl.sort(key=lambda d: (order[d["tier"]], -(d["buy_usd"] or 0)))
    tiered = [w for w in wl if w["tier"]]
    tape = rows(conn, """SELECT sig, wallet, side, ts, usd_amount, sol_amount, price_usd FROM trades WHERE mint=?
                         ORDER BY ts DESC LIMIT 400""", (mint,))
    tape = [dict(t, tier=_tier(tm, t["wallet"]) if t["wallet"] in tm else None,
                 mcap_usd=_f(t["price_usd"] * sup, 0) if sup and t["price_usd"] else None) for t in tape]
    tiered_tape = [t for t in tape if t["tier"]][:150]

    # stats
    first_tiered = min((w["first_ts"] for w in tiered if w["first_ts"]), default=None)
    first_tiered_mcap = next((w["entry_mcap_usd"] for w in sorted(tiered, key=lambda w: w["first_ts"] or 1e12)), None)
    mc = [p["mcap_usd"] for p in price if p["mcap_usd"]]
    stats = {
        "traders": len(per), "tiered_wallets": len(tiered),
        "tiers": {k: sum(1 for w in tiered if w["tier"] == k) for k in TIERS},
        "buy_usd": _f(sum(d["buy_usd"] for d in per.values()), 0), "sell_usd": _f(sum(d["sell_usd"] for d in per.values()), 0),
        "tiered_net_usd": _f(sum(w["net_usd"] or 0 for w in tiered), 0),
        "first_tiered_ts": first_tiered, "first_tiered_mcap": first_tiered_mcap,
        "ath_mcap": _f(max(mc), 0) if mc else None, "low_mcap": _f(min(mc), 0) if mc else None,
        "signals": len(sigs), "best_score": max((s["score"] or 0 for s in sigs), default=None),
        "first_signal_mcap": next((s["mcap_usd"] for s in reversed(sigs) if s["mcap_usd"]), None),
        "trades": scalar(conn, "SELECT COUNT(*) FROM trades WHERE mint=?", (mint,), 0),
        "first_trade_ts": first_trade, "last_trade_ts": last_trade,
    }
    metrics = rows(conn, "SELECT * FROM signal_metrics WHERE mint=? AND ts>=? ORDER BY ts", (mint, t0)) \
        if has(conn, "signal_metrics") else []
    for m in metrics:
        m.pop("contributions_json", None)
    stats["best_score"] = max([stats["best_score"] or 0] + [m.get("score") or 0 for m in metrics]) or None
    preds = predictions(conn, mint, since=t0)
    alerts = rows(conn, "SELECT id, ts, strategy, action, reason, score, mcap_usd, position_id FROM ct_alerts WHERE mint=? ORDER BY ts DESC LIMIT 100",
                  (mint,)) if has(conn, "ct_alerts") else []
    cpos = rows(conn, "SELECT * FROM ct_positions WHERE mint=? ORDER BY opened_at DESC", (mint,)) if has(conn, "ct_positions") else []
    token = None
    if tok:
        token = {k: tok.get(k) for k in ("mint", "symbol", "name", "pool_address", "dex", "price_usd", "mcap_usd",
                                         "liquidity_usd", "pair_created_at", "price_change_m5", "price_change_h1",
                                         "price_change_h24", "volume_h1", "first_seen", "last_updated", "tracked")}
        token["supply"] = sup
    return {"mint": mint, "token": token, "stats": stats, "signals": sigs, "price": price, "flows": flows,
            "flow_bucket_s": bucket, "metrics": metrics,
            "predictions": [{k: p[k] for k in ("ts", "score", "p_1h", "p_6h", "mcap_usd", "tiers", "inflow_usd")} for p in preds],
            "alerts": alerts, "wallets": wl[:300], "tiered_trades": tiered_tape,
            "trades": tape[:150], "positions": [_pos(p) for p in cpos], "tier_source": tier_source(conn),
            "prediction_live": _live_prediction(mint), "safety": safety_detail(conn, mint)}


def _live_prediction(mint):
    """Current model prognosis for one coin (model.score_token), if the model is installed."""
    if os.getenv("WEB_LIVE_PREDICT", "1") != "1":
        return None
    try:
        from model import score_token  # type: ignore
    except Exception:
        return None
    try:
        r = cache.get(("score_token", mint), 60, lambda: score_token(mint))
        out = _norm_prediction({"id": 0, "ts": r.get("ts"), "mint": mint, "symbol": "", "score": r.get("score"),
                                 "p_15m": (r.get("horizons", {}).get("15m") or {}).get("p_pump"),
                                 "p_1h": (r.get("horizons", {}).get("1h") or {}).get("p_pump"),
                                 "p_6h": (r.get("horizons", {}).get("6h") or {}).get("p_pump"),
                                 "exp_ret_1h": (r.get("horizons", {}).get("1h") or {}).get("exp_max_ret"),
                                 "price_usd": r.get("price_usd"), "mcap_usd": r.get("mcap_usd"),
                                 "n_ultra": (r.get("tiers", {}).get("ultra") or {}).get("wallets"),
                                 "n_smart": (r.get("tiers", {}).get("smart") or {}).get("wallets"),
                                 "n_good": (r.get("tiers", {}).get("good") or {}).get("wallets"),
                                 "net_sol": sum((v or {}).get("net_sol") or 0 for v in (r.get("tiers") or {}).values()),
                                 "contributions_json": json.dumps(r.get("contributions") or {}),
                                 "wallets_json": json.dumps(r.get("wallets") or []),
                                 "features_json": json.dumps(r.get("features") or {}), "version": r.get("version")}, 0)
        out["algo"] = (r.get("horizons", {}).get("1h") or {}).get("algo")
        return out
    except Exception as e:  # never break the coin page because of the model
        return {"error": str(e)[:200]}


# ====================================================================== wallets
WALLET_SORTS = {"score", "win_rate", "roi", "total_pnl_usd", "influence_followers", "influence_usd",
                "inflow_lift_usd", "n_tokens", "early_rate", "last_trade_ts", "copy_score", "exit_capture",
                "median_hold_min"}


def _fallback_influence(conn, addresses, window_s=600):
    """Followers and their buy USD in the window after each of the wallet's first buys per coin."""
    if not addresses:
        return {}
    addresses = set(addresses)

    def calc():
        buys = rows(conn, "SELECT wallet, mint, ts, usd_amount FROM trades WHERE side='buy' ORDER BY mint, ts")
        by_mint = defaultdict(list)
        for b in buys:
            by_mint[b["mint"]].append(b)
        out = defaultdict(lambda: {"followers": 0, "usd": 0.0, "coins": 0})
        for m, bl in by_mint.items():
            ts = [b["ts"] for b in bl]
            seen = set()
            for b in bl:
                if b["wallet"] not in addresses or b["wallet"] in seen:
                    continue
                seen.add(b["wallet"])
                i, j = bisect.bisect_right(ts, b["ts"]), bisect.bisect_right(ts, b["ts"] + window_s)
                fol = {x["wallet"] for x in bl[i:j] if x["wallet"] != b["wallet"]}
                o = out[b["wallet"]]
                o["followers"] += len(fol)
                o["usd"] += sum(x["usd_amount"] or 0 for x in bl[i:j] if x["wallet"] != b["wallet"])
                o["coins"] += 1
        return {a: {"influence_followers": round(v["followers"] / v["coins"], 2) if v["coins"] else 0,
                    "influence_usd": round(v["usd"] / v["coins"], 2) if v["coins"] else 0} for a, v in out.items()}
    return cache.get(("influence", len(addresses)), 120, calc)


def wallets(conn, tier=None, sort="score", q=None, limit=200, offset=0, include_untiered=False):
    src = tier_source(conn)
    tm = tier_map(conn)
    ct = {r["address"]: r for r in rows(conn, "SELECT * FROM ct_wallet_rank")} if has(conn, "ct_wallet_rank") else {}
    meta = {}
    if has(conn, "wallets"):
        for r in rows(conn, "SELECT address, label, manual, is_bot, last_trade_ts, source FROM wallets WHERE label!='' OR manual=1"):
            meta[r["address"]] = r
    if src == "model":
        base = rows(conn, "SELECT * FROM model_wallet_stats" + ("" if include_untiered else " WHERE tier IS NOT NULL"))
        lt = {}
        if has(conn, "wallets"):
            addrs = [b["address"] for b in base]
            for i in range(0, len(addrs), 500):
                ch = addrs[i:i + 500]
                for r in rows(conn, f"SELECT address, last_trade_ts FROM wallets WHERE address IN ({','.join('?' * len(ch))})", ch):
                    lt[r["address"]] = r["last_trade_ts"]
        out = []
        for b in base:
            comp = _j(b.get("components_json"), {})
            out.append({**{k: b.get(k) for k in ("address", "tier", "score", "n_tokens", "win_rate", "early_rate", "roi",
                                                  "realized_pnl_usd", "total_pnl_usd", "median_entry_mcap",
                                                  "median_min_to_2x", "influence_followers", "influence_usd",
                                                  "inflow_lift_usd", "consistency", "rug_rate", "cluster_id",
                                                  "exit_capture", "median_hold_min")},
                        "components": comp, "last_trade_ts": lt.get(b["address"])})
    else:
        if not has(conn, "wallets"):
            return {"tier_source": src, "total": 0, "items": [], "counts": {}}
        where = "" if include_untiered else ""
        base = rows(conn, f"""SELECT address, score, tokens_traded n_tokens, win_rate, early_rate, roi, realized_pnl_usd,
                                    total_pnl_usd, median_peak_multiple, consistency, last_trade_ts, is_bot
                             FROM wallets WHERE score>0 {where} ORDER BY score DESC LIMIT 3000""")
        if not include_untiered:
            base = [b for b in base if b["address"] in tm]
        infl = _fallback_influence(conn, [b["address"] for b in base[:600]])
        out = []
        for b in base:
            out.append({**b, "tier": (tm.get(b["address"]) or {}).get("tier"), **infl.get(b["address"], {})})
    for w in out:
        m = meta.get(w["address"]) or {}
        w["label"] = m.get("label") or ""
        w["manual"] = bool(m.get("manual"))
        c = ct.get(w["address"])
        w["copy_score"] = _f(c.get("score"), 4) if c else None
        w["copy_hit_rate"] = _f(c.get("hit_rate"), 3) if c else None
        w["copy_samples"] = c.get("samples") if c else None
        w["copy_first_pump"] = _f(c.get("median_first_pump"), 3) if c else None
    counts = {k: sum(1 for w in out if w.get("tier") == k) for k in TIERS}
    if tier:
        out = [w for w in out if w.get("tier") == tier]
    if q:
        ql = q.lower()
        out = [w for w in out if ql in w["address"].lower() or ql in (w["label"] or "").lower()]
    sort = sort if sort in WALLET_SORTS else "score"
    out.sort(key=lambda w: (w.get(sort) is not None, w.get(sort) or 0), reverse=True)
    total = len(out)
    return {"tier_source": src, "total": total, "counts": counts, "items": out[offset:offset + limit]}


def wallet_stats_overview(conn):
    """Distributions for the wallet research page: tier counts, score histogram, scatter win rate vs ROI."""
    allw = wallets(conn, include_untiered=True, limit=100000)["items"]
    tiered = [w for w in allw if w.get("tier")]
    hist = [0] * 10
    for w in allw:
        s = w.get("score") or 0
        hist[min(9, int(s // 10))] += 1
    scatter = [{"address": w["address"], "tier": w.get("tier") or "other", "win_rate": _f(w.get("win_rate"), 3),
                "roi": _f(max(-1, min(w.get("roi") or 0, 5)), 3), "score": _f(w.get("score"), 1),
                "n_tokens": w.get("n_tokens")}
               for w in sorted(allw, key=lambda w: -(w.get("score") or 0))[:800] if w.get("win_rate") is not None]
    by_tier = {}
    for k in TIERS:
        xs = [w for w in tiered if w["tier"] == k]

        def med(f):
            v = [w.get(f) for w in xs if w.get(f) is not None]
            return _f(statistics.median(v), 4) if v else None
        by_tier[k] = {"n": len(xs), "win_rate": med("win_rate"), "roi": med("roi"), "early_rate": med("early_rate"),
                      "influence_followers": med("influence_followers"), "influence_usd": med("influence_usd"),
                      "median_entry_mcap": med("median_entry_mcap"), "total_pnl_usd": med("total_pnl_usd")}
    return {"tier_source": tier_source(conn), "total_wallets": scalar(conn, "SELECT COUNT(*) FROM wallets", default=0),
            "scored_wallets": len(allw), "tiered": len(tiered),
            "counts": {k: by_tier[k]["n"] for k in TIERS}, "by_tier": by_tier,
            "score_hist": [{"bucket": f"{i * 10}–{i * 10 + 10}", "n": n} for i, n in enumerate(hist)],
            "scatter": scatter}


def wallet_detail(conn, address):
    tm = tier_map(conn)
    base = one(conn, "SELECT * FROM wallets WHERE address=?", (address,)) if has(conn, "wallets") else None
    mstats = one(conn, "SELECT * FROM model_wallet_stats WHERE address=?", (address,)) if has(conn, "model_wallet_stats") else None
    if mstats:
        mstats["components"] = _j(mstats.pop("components_json", None), {})
    toks = {t["mint"]: t for t in rows(conn, "SELECT mint, symbol, mcap_usd, price_usd FROM tokens")} if has(conn, "tokens") else {}
    pos = rows(conn, "SELECT * FROM wallet_positions WHERE wallet=? ORDER BY first_buy_ts DESC", (address,)) if has(conn, "wallet_positions") else []
    for p in pos:
        t = toks.get(p["mint"]) or {}
        p["symbol"] = t.get("symbol") or p["mint"][:6]
        sup = _supply(t)
        p["entry_mcap_usd"] = _f(p["first_buy_price"] * sup, 0) if sup and p.get("first_buy_price") else None
        p["pnl_usd"] = _f((p.get("realized_pnl_usd") or 0) + (p.get("unrealized_pnl_usd") or 0), 2)
    trades = rows(conn, "SELECT * FROM trades WHERE wallet=? ORDER BY ts DESC LIMIT 300", (address,))
    for t in trades:
        t["symbol"] = (toks.get(t["mint"]) or {}).get("symbol") or t["mint"][:6]
    history = rows(conn, """SELECT h.version, h.tier, h.score, v.ts FROM model_tier_history h
                            LEFT JOIN model_versions v ON v.id=h.version WHERE h.address=? ORDER BY h.version""",
                   (address,)) if has(conn, "model_tier_history") else []
    ct = one(conn, "SELECT * FROM ct_wallet_rank WHERE address=?", (address,)) if has(conn, "ct_wallet_rank") else None
    outc = rows(conn, "SELECT * FROM ct_buy_outcomes WHERE wallet=? ORDER BY ts DESC LIMIT 200", (address,)) if has(conn, "ct_buy_outcomes") else []
    for o in outc:
        o["symbol"] = (toks.get(o["mint"]) or {}).get("symbol") or o["mint"][:6]
    copies = [_pos(p) for p in rows(conn, "SELECT * FROM ct_positions WHERE wallet=? ORDER BY opened_at DESC", (address,))] if has(conn, "ct_positions") else []
    tracked = bool(one(conn, "SELECT 1 x FROM ct_wallets WHERE address=?", (address,))) if has(conn, "ct_wallets") else False
    # cumulative realized PnL over time (by first buy)
    cum, acc = [], 0.0
    for p in sorted((p for p in pos if p.get("first_buy_ts")), key=lambda p: p["first_buy_ts"]):
        acc += p["pnl_usd"] or 0
        cum.append({"ts": p.get("first_buy_ts"), "pnl_usd": _f(acc, 2), "symbol": p["symbol"]})
    return {"address": address, "tier": (tm.get(address) or {}).get("tier"), "tier_source": tier_source(conn),
            "core": base, "model": mstats, "positions": pos, "trades": trades, "tier_history": history,
            "copy_rank": ct, "copy_outcomes": outc, "copies": copies, "copy_tracked": tracked, "pnl_curve": cum}


# ====================================================================== copy trading
def _pos(p):
    p = dict(p)
    meta = _j(p.pop("meta", None), {}) or {}
    p["meta"] = {k: meta.get(k) for k in ("fill_source", "impact", "liq_usd", "fdv", "signal_score", "smart_buyers",
                                           "wallet_score", "model", "manual_buy", "take_profit") if k in meta}
    now = now_ts()
    p["hold_min"] = _f(((p.get("closed_at") or now) - (p.get("opened_at") or now)) / 60, 1)
    p["safety_reasons"] = _j(p.get("safety_reasons"), []) or []
    if p.get("safety_level") is None and isinstance(meta.get("safety"), dict):
        sf = meta["safety"]
        p.update(safety_level=sf.get("level"), safety_score=sf.get("score"), safety_reasons=sf.get("reasons") or [])
    if p.get("whale_ts") and p.get("opened_at"):
        p["entry_delay_s"] = p["opened_at"] - p["whale_ts"]
    if p.get("status") == "open" and p.get("entry_price_sol") and p.get("last_price_sol"):
        p["unrealized_pct"] = _f(p["last_price_sol"] / p["entry_price_sol"] - 1, 4)
        p["unrealized_sol"] = _f((p["last_price_sol"] - p["entry_price_sol"]) * (p.get("tokens") or 0), 4)
    return p


def _summary(ps):
    closed = [p for p in ps if p.get("status") == "closed" and p.get("pnl_sol") is not None]
    opn = [p for p in ps if p.get("status") == "open"]
    s = {"trades": len(closed), "open": len(opn),
         "open_value_sol": _f(sum((p.get("last_price_sol") or 0) * (p.get("tokens") or 0) for p in opn), 4),
         "unrealized_sol": _f(sum(p.get("unrealized_sol") or 0 for p in opn), 4)}
    if not closed:
        return s
    wins = [p for p in closed if p["pnl_sol"] > 0]
    losses = [p for p in closed if p["pnl_sol"] <= 0]
    gw, gl = sum(p["pnl_sol"] for p in wins), -sum(p["pnl_sol"] for p in losses)
    eq, peak, dd = 0.0, 0.0, 0.0
    for p in sorted(closed, key=lambda p: p.get("closed_at") or 0):
        eq += p["pnl_sol"]
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    delays = [p["entry_delay_s"] for p in closed if p.get("entry_delay_s") is not None]
    s.update({"win_rate": _f(len(wins) / len(closed), 4), "pnl_sol": _f(sum(p["pnl_sol"] for p in closed), 4),
              "avg_pnl_pct": _f(statistics.mean(p.get("pnl_pct") or 0 for p in closed), 4),
              "avg_win_pct": _f(statistics.mean(p.get("pnl_pct") or 0 for p in wins), 4) if wins else None,
              "avg_loss_pct": _f(statistics.mean(p.get("pnl_pct") or 0 for p in losses), 4) if losses else None,
              "profit_factor": _f(gw / gl, 3) if gl > 0 else None, "max_drawdown_sol": _f(dd, 4),
              "avg_hold_min": _f(statistics.mean(p["hold_min"] or 0 for p in closed), 1),
              "avg_entry_delay_s": _f(statistics.mean(delays), 1) if delays else None,
              "best_pct": _f(max(p.get("pnl_pct") or 0 for p in closed), 4),
              "worst_pct": _f(min(p.get("pnl_pct") or 0 for p in closed), 4),
              "volume_sol": _f(sum(p.get("size_sol") or 0 for p in closed), 3)})
    return s


def control_state(conn):
    out = {"available": False, "modes": {}, "presets": {}, "active_presets": {}, "live": {}, "paused": False,
           "live_ready": False, "live_master": False, "strategies": {}, "mode_labels": {}}
    try:
        from copytrade import control  # type: ignore
        from copytrade.config import load  # type: ignore
    except Exception as e:
        out["error"] = f"copytrade nicht installiert: {e}"
        return out
    s = load()
    out["live_ready"] = s.live_ready
    out["live_master"] = s.live_trading
    out["mode_labels"] = dict(control.MODE_LABELS)
    out["strategies"] = {k: control.STRATEGY_LABELS.get(k, k) for k in control.STRATEGIES}
    if not has(conn, "ct_control"):
        out["available"] = True
        out["modes"] = {k: control.DEFAULTS.get(f"mode:{k}", "off") for k in control.STRATEGIES}
        out["active_presets"] = {k: control.DEFAULTS.get(f"preset:{k}", "standard") for k in control.STRATEGIES}
        out["live"] = {k: False for k in control.STRATEGIES}
        from dataclasses import asdict
        out["presets"] = {n: {**asdict(p), "builtin": True} for n, p in control.BUILTIN.items()}
        out["initialized"] = False
        return out
    out["available"] = True
    out["initialized"] = True
    out["modes"] = {k: control.mode(conn, k) for k in control.STRATEGIES}
    out["active_presets"] = {k: control.get(conn, f"preset:{k}") for k in control.STRATEGIES}
    out["live"] = {k: control.live_wanted(conn, k) for k in control.STRATEGIES}
    out["paused"] = control.paused(conn)
    for r in rows(conn, "SELECT name, data, builtin, updated_at FROM ct_presets ORDER BY builtin DESC, name"):
        out["presets"][r["name"]] = {**(_j(r["data"], {}) or {}), "builtin": bool(r["builtin"]), "updated_at": r["updated_at"]}
    return out


def copytrade(conn, mode="paper", strategy=None):
    if not has(conn, "ct_positions"):
        return {"available": False, "control": control_state(conn)}
    cols = tables(conn)["ct_positions"]
    q, a = "SELECT * FROM ct_positions WHERE mode=?", [mode]
    if strategy and "strategy" in cols:
        q += " AND strategy=?"
        a.append(strategy)
    ps = [_pos(p) for p in rows(conn, q + " ORDER BY opened_at", a)]
    summ = _summary(ps)
    by_strategy = {}
    if "strategy" in cols:
        for st in sorted({p.get("strategy") or "whale" for p in ps}):
            by_strategy[st] = _summary([p for p in ps if (p.get("strategy") or "whale") == st])
    eq = rows(conn, "SELECT ts, balance_sol, open_value_sol, equity_sol FROM ct_equity WHERE mode=? ORDER BY ts", (mode,)) \
        if has(conn, "ct_equity") else []
    if len(eq) > 1500:
        step = len(eq) // 1500 + 1
        eq = eq[::step] + [eq[-1]]
    closed = [p for p in ps if p.get("status") == "closed"]
    # realized PnL curve from closed trades (works even without equity snapshots)
    curve, acc = [], 0.0
    for p in sorted(closed, key=lambda p: p.get("closed_at") or 0):
        acc += p.get("pnl_sol") or 0
        curve.append({"ts": p["closed_at"], "pnl_sol": _f(acc, 4), "id": p["id"], "symbol": p.get("symbol")})
    reasons = defaultdict(lambda: {"n": 0, "pnl_sol": 0.0, "wins": 0})
    for p in closed:
        r = reasons[p.get("exit_reason") or "?"]
        r["n"] += 1
        r["pnl_sol"] += p.get("pnl_sol") or 0
        r["wins"] += 1 if (p.get("pnl_sol") or 0) > 0 else 0
    per_wallet = defaultdict(lambda: {"n": 0, "pnl_sol": 0.0, "wins": 0, "pcts": []})
    for p in closed:
        if not p.get("wallet"):
            continue
        w = per_wallet[p["wallet"]]
        w["n"] += 1
        w["pnl_sol"] += p.get("pnl_sol") or 0
        w["wins"] += 1 if (p.get("pnl_sol") or 0) > 0 else 0
        w["pcts"].append(p.get("pnl_pct") or 0)
    tm = tier_map(conn)
    pw = sorted(({"wallet": k, "n": v["n"], "pnl_sol": _f(v["pnl_sol"], 4), "win_rate": _f(v["wins"] / v["n"], 3),
                  "avg_pct": _f(statistics.mean(v["pcts"]), 4), "tier": (tm.get(k) or {}).get("tier")}
                 for k, v in per_wallet.items()), key=lambda r: -(r["pnl_sol"] or 0))
    hist = defaultdict(int)
    for p in closed:
        pct = (p.get("pnl_pct") or 0) * 100
        b = max(-50, min(150, int(math.floor(pct / 10) * 10)))
        hist[b] += 1
    pnl_hist = [{"bucket": b, "n": hist.get(b, 0)} for b in range(-50, 160, 10)]
    by_hour = defaultdict(lambda: {"n": 0, "pnl_sol": 0.0})
    for p in closed:
        h = time.gmtime(p.get("opened_at") or 0).tm_hour
        by_hour[h]["n"] += 1
        by_hour[h]["pnl_sol"] += p.get("pnl_sol") or 0
    rank = []
    if has(conn, "ct_wallet_rank"):
        rank = rows(conn, """SELECT r.*, w.label, w.source, w.active FROM ct_wallet_rank r
                             LEFT JOIN ct_wallets w ON w.address=r.address ORDER BY r.score DESC LIMIT 200""")
        for r in rank:
            r["tier"] = (tm.get(r["address"]) or {}).get("tier")
    outc = rows(conn, """SELECT first_pump_gain, first_pump_min, max_gain, min_before_peak, inflow_lead_min,
                                achievable_gain, pumped FROM ct_buy_outcomes WHERE pumped IS NOT NULL""") \
        if has(conn, "ct_buy_outcomes") else []

    def dist(vals, edges):
        out = []
        for i in range(len(edges) - 1):
            out.append({"bucket": f"{edges[i]}–{edges[i + 1]}", "n": sum(1 for v in vals if edges[i] <= v < edges[i + 1])})
        return out
    timing = {
        "n": len(outc), "pumped": sum(1 for o in outc if o["pumped"]),
        "first_pump_pct": dist([(o["first_pump_gain"] or 0) * 100 for o in outc if o["pumped"]], [25, 35, 50, 75, 100, 150, 200, 300, 10000]),
        "minutes_to_peak": dist([o["first_pump_min"] or 0 for o in outc if o["pumped"]], [0, 1, 2, 3, 5, 10, 20, 40, 120, 10000]),
        "lead_minutes": dist([o["inflow_lead_min"] for o in outc if o.get("inflow_lead_min") is not None], [-60, 0, 0.5, 1, 2, 3, 5, 10, 30, 10000]),
        "achievable_pct": dist([(o["achievable_gain"] or 0) * 100 for o in outc], [-100, -20, -10, 0, 10, 20, 35, 50, 100, 10000]),
    }
    alerts = rows(conn, "SELECT * FROM ct_alerts ORDER BY ts DESC LIMIT 150") if has(conn, "ct_alerts") else []
    for a_ in alerts:
        sf = (_j(a_.get("data"), {}) or {}).get("safety") if isinstance(a_.get("data"), str) else None
        a_["safety"] = {k: sf.get(k) for k in ("level", "score", "blocked", "reasons")} if isinstance(sf, dict) else None
        a_["data"] = None  # can be large; the alert row itself carries the essentials
    events = rows(conn, "SELECT ts, level, kind, message FROM ct_events ORDER BY ts DESC LIMIT 200") if has(conn, "ct_events") else []
    cmds = rows(conn, "SELECT * FROM ct_commands ORDER BY id DESC LIMIT 30") if has(conn, "ct_commands") else []
    balance = None
    if mode == "paper" and has(conn, "ct_state"):
        v = scalar(conn, "SELECT value FROM ct_state WHERE key='paper_balance_sol'")
        balance = _f(v, 4) if v is not None else None
    return {"available": True, "mode": mode, "strategy": strategy, "summary": summ, "by_strategy": by_strategy,
            "balance_sol": balance, "equity": eq, "pnl_curve": curve,
            "open": [p for p in ps if p.get("status") == "open"][::-1],
            "closed": closed[::-1][:500], "exit_reasons": [{"reason": k, **{kk: _f(vv, 4) if isinstance(vv, float) else vv for kk, vv in v.items()}} for k, v in sorted(reasons.items(), key=lambda kv: -kv[1]["n"])],
            "per_wallet": pw[:100], "pnl_hist": pnl_hist,
            "by_hour": [{"hour": h, "n": by_hour[h]["n"], "pnl_sol": _f(by_hour[h]["pnl_sol"], 4)} for h in range(24)],
            "whale_rank": rank, "timing": timing, "alerts": alerts, "events": events, "commands": cmds,
            "control": control_state(conn), "sol_usd": _f(sol_usd(conn), 2)}


# ====================================================================== model / research
def _feature_names():
    try:
        from research.events import FEATURE_LABELS, FEATURE_NAMES  # type: ignore
        return FEATURE_NAMES, FEATURE_LABELS
    except Exception:
        return [], {}


def _norm_version(r):
    names, labels = _feature_names()
    metrics = _j(r.get("metrics_json"), {}) or {}
    ww = _j(r.get("wallet_weights_json"), {}) or {}
    weights = ww.get("weights", ww) if isinstance(ww, dict) else {}
    tm = _j(r.get("token_model_json"), {}) or {}
    coefs = {}
    for h, m in (tm.items() if isinstance(tm, dict) else []):
        c = (m or {}).get("coef")
        if isinstance(c, list) and names and len(c) == len(names):
            coefs[h] = sorted(({"feature": f, "label": labels.get(f, f), "weight": _f(w, 4)} for f, w in zip(names, c)),
                              key=lambda d: -abs(d["weight"] or 0))
        elif isinstance(c, dict):
            coefs[h] = sorted(({"feature": f, "label": labels.get(f, f), "weight": _f(w, 4)} for f, w in c.items()),
                              key=lambda d: -abs(d["weight"] or 0))
        cfg = (m or {}).get("cfg") or {}
        coefs.setdefault("_kind", {})[h] = {"kind": (m or {}).get("kind"), "n": (m or {}).get("n"), "pos": (m or {}).get("pos"),
                                            "algo": cfg.get("algo") or (m or {}).get("kind"), "drop": cfg.get("drop")}
    h1 = metrics.get("1h") or {}
    return {"id": r["id"], "ts": r["ts"], "active": bool(r.get("active")), "n_events": r.get("n_events"),
            "n_wallets": r.get("n_wallets"), "tier_counts": _j(r.get("tier_counts_json"), {}) or {},
            "wallet_weights": {k: _f(v, 4) for k, v in weights.items() if isinstance(v, (int, float))},
            "coefs": coefs, "metrics": metrics, "tier_rules": _j(r.get("tier_rules_json"), {}),
            "algo_1h": ((coefs.get("_kind") or {}).get("1h") or {}).get("algo"),
            "selection": metrics.get("selection") or {},
            "auc_1h": _f(h1.get("auc"), 4), "auc_baseline_1h": _f(h1.get("auc_baseline"), 4),
            "top10_1h": _f(h1.get("top10_pump_rate"), 4), "base_rate_1h": _f(h1.get("base_rate"), 4),
            "n_test_1h": h1.get("n"), "notes": r.get("notes")}


def model_view(conn):
    versions = [_norm_version(r) for r in rows(conn, "SELECT * FROM model_versions ORDER BY id")] if has(conn, "model_versions") else []
    report = None
    if has(conn, "research_reports"):
        report = one(conn, "SELECT id, ts, version, title, markdown, json FROM research_reports ORDER BY id DESC LIMIT 1")
        if report:
            report["data"] = _j(report.pop("json", None), {}) or {}
    # calibration of stored prognoses: predicted P(pump 1h) vs realized
    calib = []
    if has(conn, "model_predictions"):
        pr = rows(conn, "SELECT p_1h, max_ret_1h FROM model_predictions WHERE p_1h IS NOT NULL AND max_ret_1h IS NOT NULL")
        for lo in range(0, 100, 10):
            xs = [p for p in pr if lo <= p["p_1h"] * 100 < lo + 10]
            if xs:
                calib.append({"bucket": f"{lo}–{lo + 10} %", "predicted": _f(statistics.mean(p["p_1h"] for p in xs), 4),
                              "realized": _f(sum(1 for p in xs if p["max_ret_1h"] >= PUMP_1H) / len(xs), 4), "n": len(xs)})
    research = {}
    for t, label in (("research_wallets", "Wallets in der Forschung"), ("research_tokens", "Coins erforscht"),
                     ("research_candles", "Kerzen geladen"), ("model_predictions", "Prognosen gespeichert"),
                     ("model_wallet_stats", "Wallets bewertet")):
        if has(conn, t):
            research[t] = {"label": label, "n": scalar(conn, f"SELECT COUNT(*) FROM {t}", default=0)}
    if has(conn, "research_tokens"):
        research["pumped_tokens"] = {"label": "davon gepumpt", "n": scalar(conn, "SELECT COUNT(*) FROM research_tokens WHERE pumped=1", default=0)}
    active = next((v for v in reversed(versions) if v["active"]), versions[-1] if versions else None)
    # gradient boosting has no coefficients in token_model_json: use the report's per-feature weights instead
    rmodels = ((report or {}).get("data") or {}).get("models") or {}
    if active and report and report.get("version") == active["id"]:
        for h, mm in rmodels.items():
            if not active["coefs"].get(h) and mm.get("weights"):
                active["coefs"][h] = [{"feature": w.get("feature"), "label": w.get("label"), "weight": _f(w.get("weight"), 4)}
                                      for w in mm["weights"]]
            k = active["coefs"].setdefault("_kind", {}).setdefault(h, {})
            k.setdefault("algo", mm.get("algo"))
            if not k.get("algo"):
                k["algo"] = mm.get("algo")
    return {"versions": versions, "active": active, "report": report, "calibration": calib, "research": research,
            "tier_source": tier_source(conn),
            "signal_formula": [{"feature": f, "label": lab, "weight": w} for f, lab, w in
                               (("inflow", "Netto-Inflow Smart Money", 0.4), ("breadth", "Anzahl Smart Wallets", 0.3),
                                ("quality", "Ø Wallet-Qualität", 0.3))],
            "performance": signal_performance(conn)}


# ====================================================================== overview + system
def overview(conn):
    now = now_ts()
    tm = tier_map(conn)
    tiers = {k: sum(1 for v in tm.values() if v.get("tier") == k) for k in TIERS}
    day = now - 86400
    sigs = signals(conn, since=day, limit=500)
    flows = flow_series(conn, day, 900)
    net_24 = sum(f["ultra"] + f["smart"] + f["good"] for f in flows)
    perf = signal_performance(conn)
    ct = {}
    for mode in ("paper", "live"):
        if has(conn, "ct_positions"):
            ps = [_pos(p) for p in rows(conn, "SELECT * FROM ct_positions WHERE mode=?", (mode,))]
            if ps or mode == "paper":
                ct[mode] = _summary(ps)
                eq = rows(conn, "SELECT ts, equity_sol FROM ct_equity WHERE mode=? ORDER BY ts", (mode,)) if has(conn, "ct_equity") else []
                if len(eq) > 300:
                    eq = eq[:: len(eq) // 300 + 1] + [eq[-1]]
                ct[mode]["equity"] = eq
                ct[mode]["equity_now"] = eq[-1]["equity_sol"] if eq else None
    return {
        "now": now, "tier_source": tier_source(conn), "tiers": tiers,
        "wallets_total": scalar(conn, "SELECT COUNT(*) FROM wallets", default=0),
        "tokens_tracked": scalar(conn, "SELECT COUNT(*) FROM tokens WHERE tracked=1", default=0),
        "trades_24h": scalar(conn, "SELECT COUNT(*) FROM trades WHERE ts>=?", (day,), 0),
        "signals_24h": len(sigs),
        "net_tiered_24h": _f(net_24, 0), "flows": flows, "signal_perf": {k: perf[k] for k in ("n", "hits", "hit_rate", "pump_threshold", "median_max_1h")},
        "feed": sigs[:25], "top_coins": coins(conn, sort="strength", limit=12),
        "copytrade": ct, "control": control_state(conn), "model": model_active(conn),
        "last_trade_ts": scalar(conn, "SELECT MAX(ts) FROM trades"),
    }


def system(conn):
    from .db import db_path
    path = db_path()
    counts = {}
    for t in sorted(tables(conn)):
        if t.startswith("sqlite_"):
            continue
        counts[t] = scalar(conn, f"SELECT COUNT(*) FROM '{t}'", default=0)
    last = {}
    for t, col in (("trades", "ts"), ("token_prices", "ts"), ("signal_metrics", "ts"), ("signals", "ts"),
                   ("model_predictions", "ts"), ("model_versions", "ts"), ("ct_events", "ts"), ("ct_equity", "ts"),
                   ("ct_swaps", "seen_at")):
        if has(conn, t, col):
            last[t] = scalar(conn, f"SELECT MAX({col}) FROM {t}")
    services = []
    pids = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "pids")
    if os.path.isdir(pids):
        for f in sorted(os.listdir(pids)):
            if f.endswith(".pid"):
                name = f[:-4]
                try:
                    pid = int(open(os.path.join(pids, f)).read().strip())
                    os.kill(pid, 0)
                    alive = True
                except Exception:
                    pid, alive = None, False
                services.append({"name": name, "pid": pid, "running": alive})
    try:
        size = os.path.getsize(path)
    except OSError:
        size = None
    return {"db_path": path, "db_size": size, "tables": counts, "last": last, "services": services,
            "tier_source": tier_source(conn), "now": now_ts(), "sol_usd": _f(sol_usd(conn), 2), "api_usage": api_usage()}


# ====================================================================== API budgets (core.http usage files)
_USAGE_HIST = {}            # (service, group) -> deque[(ts, calls)] sampled whenever the dashboard reads usage
USAGE_WINDOW_S = 600
USAGE_STALE_S = 180         # usage files are rewritten every minute; older means the service is not running


def api_usage(now=None):
    """API calls per service and host group from core.http.usage(all_services=True).

    Each row: service, group, calls (since the service started), calls_window and window_s (calls in the last
    10 min, from samples this process took; shorter window right after the dashboard starts), last_min,
    budget_per_min, load (last_min / budget), 429, waited_s, ts, stale. Extra numeric fields in a group
    (e.g. credits_month, credits_cap) are passed through unchanged."""
    from collections import deque
    try:
        from core import http as chttp  # type: ignore
        raw = chttp.usage(all_services=True) or {}
    except Exception as e:  # core.http without usage(): older main
        return {"available": False, "note": f"core.http.usage fehlt: {e}"[:200], "rows": []}
    now = now or time.time()
    out = []
    for svc, d in sorted(raw.items()):
        ts = d.get("ts")
        stale = not ts or now - ts > USAGE_STALE_S
        for g, v in sorted((d.get("groups") or {}).items()):
            if not isinstance(v, dict):
                continue
            calls = v.get("calls") or 0
            h = _USAGE_HIST.setdefault((svc, g), deque(maxlen=200))
            if not h or h[-1][0] != ts:
                if h and calls < h[-1][1]:  # service restarted: counters began again
                    h.clear()
                h.append((ts or now, calls))
            while len(h) > 1 and h[1][0] <= (ts or now) - USAGE_WINDOW_S:
                h.popleft()
            base_ts, base_calls = h[0]
            win = (ts or now) - base_ts
            b = v.get("budget_per_min")
            last = v.get("last_min")
            row = {k: x for k, x in v.items() if isinstance(x, (int, float)) and not isinstance(x, bool)}
            row.update(service=svc, group=g, pid=d.get("pid"), ts=ts, stale=stale, calls=calls,
                       calls_window=calls - base_calls if win > 0 else None, window_s=int(win) if win > 0 else 0,
                       last_min=last, budget_per_min=b, waited_s=_f(v.get("waited_s"), 1),
                       load=_f(last / b, 3) if b and last is not None and not stale else None)
            row["429"] = v.get("429") or 0
            out.append(row)
    return {"available": True, "rows": out, "window_s": USAGE_WINDOW_S}


def search(conn, q, limit=12):
    q = (q or "").strip()
    if len(q) < 2:
        return []
    out = []
    if has(conn, "tokens"):
        for t in rows(conn, """SELECT mint, symbol, name, mcap_usd FROM tokens WHERE symbol LIKE ? OR name LIKE ? OR mint LIKE ?
                               ORDER BY mcap_usd DESC LIMIT ?""", (f"%{q}%", f"%{q}%", f"{q}%", limit)):
            out.append({"type": "coin", "id": t["mint"], "label": t["symbol"] or t["mint"][:6], "sub": t["name"], "mcap_usd": t["mcap_usd"]})
    if len(q) >= 4 and has(conn, "wallets"):
        for w in rows(conn, "SELECT address, label, score FROM wallets WHERE address LIKE ? OR label LIKE ? ORDER BY score DESC LIMIT ?",
                      (f"{q}%", f"%{q}%", limit)):
            out.append({"type": "wallet", "id": w["address"], "label": w["label"] or w["address"][:8], "sub": f"Score {w['score'] or 0:.0f}"})
    return out[:limit]
