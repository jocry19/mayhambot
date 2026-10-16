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
import re
import statistics
import time

# repo root: data/ (pids, state files) and logs/ live under it; tests point it at a temp dir (web/tests/conftest.py)
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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
def _has_index(conn, table, col):
    """True if some index of `table` starts with `col` (then range and MAX queries on it are cheap)."""
    def calc():
        try:
            for ix in conn.execute(f"PRAGMA index_list('{table}')").fetchall():
                first = conn.execute(f"PRAGMA index_info('{ix[1]}')").fetchone()
                if first and first[2] == col:
                    return True
        except Exception:
            pass
        return False
    return cache.get(("has_index", table, col), 300, calc)


TAIL_ROWS = 20000  # newest inserts looked at when trades(ts) has no index


def last_ts(conn, table, col="ts"):
    """Newest value of a time column. Without an index starting with it, MAX() reads the whole table (seconds on
    millions of rows), so look at the newest inserts by rowid instead: rows arrive in time order, backfills only
    add older ones."""
    if not has(conn, table, col):
        return None
    if _has_index(conn, table, col):
        return scalar(conn, f"SELECT MAX({col}) FROM {table}")
    try:
        return scalar(conn, f"SELECT MAX({col}) FROM {table} WHERE rowid > (SELECT MAX(rowid) FROM {table}) - ?", (TAIL_ROWS,))
    except Exception:  # WITHOUT ROWID table
        return scalar(conn, f"SELECT MAX({col}) FROM {table}")


def last_trade_ts(conn):
    return last_ts(conn, "trades")


def sol_usd(conn) -> float:
    def calc():
        r = rows(conn, """SELECT usd_amount, sol_amount FROM trades WHERE rowid > (SELECT MAX(rowid) FROM trades) - ?
                          AND sol_amount>0 AND usd_amount>0 ORDER BY rowid DESC LIMIT 300""", (TAIL_ROWS,))
        xs = [a["usd_amount"] / a["sol_amount"] for a in r if a["sol_amount"]]
        xs = [x for x in xs if 20 < x < 2000]
        return statistics.median(xs) if xs else 150.0
    return cache.get(("sol_usd",), 60, calc)


def _wallets_synced(conn) -> bool:
    """True once signals/ synced the active model's tiers into wallets (core.scoring.sync_model_tiers)."""
    return has(conn, "wallets", "tier_source") and bool(
        scalar(conn, "SELECT 1 FROM wallets WHERE tier_source='model' AND tier IS NOT NULL LIMIT 1", default=0))


def tier_source(conn) -> str:
    """model = tiers of the active model (wallets.tier synced from it, else model_wallet_stats directly),
    core = wallets.tier from the core heuristic, fallback = README rules on core score."""
    def calc():
        if _wallets_synced(conn):
            return "model"
        if has(conn, "model_wallet_stats") and scalar(conn, "SELECT COUNT(*) FROM model_wallet_stats WHERE tier IS NOT NULL", default=0):
            return "model"
        if has(conn, "wallets", "tier") and scalar(conn, "SELECT COUNT(*) FROM wallets WHERE tier IS NOT NULL", default=0):
            return "core"
        return "fallback"
    return cache.get(("tier_source",), 30, calc)


def tier_map(conn) -> dict[str, dict]:
    """{address: {tier, score, influence_followers, influence_usd, ...}} for every tiered wallet.
    wallets.tier is the one truth once synced, so dashboard, signals and copy trader count the same wallets."""
    def calc():
        out = {}
        if _wallets_synced(conn):
            infl = {}
            if has(conn, "model_wallet_stats"):
                infl = {r["address"]: r for r in rows(conn, """SELECT address, influence_followers, influence_usd, inflow_lift_usd
                                                               FROM model_wallet_stats WHERE tier IS NOT NULL""")}
            for r in rows(conn, """SELECT address, tier, COALESCE(model_score, score) score, tier_source, tier_version
                                   FROM wallets WHERE tier IS NOT NULL"""):
                out[r["address"]] = {**(infl.get(r["address"]) or {}), **r}
            return out
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


def tier_info(conn) -> dict:
    """Where the tiers come from right now, for badges: source, version, synced into wallets, counts."""
    src = tier_source(conn)
    synced = _wallets_synced(conn)
    ver = None
    if synced:
        ver = scalar(conn, "SELECT MAX(tier_version) FROM wallets WHERE tier_source='model'")
    elif src == "model" and has(conn, "model_wallet_stats", "version"):
        ver = scalar(conn, "SELECT MAX(version) FROM model_wallet_stats")
    tm = tier_map(conn)
    return {"source": src, "version": ver, "synced": synced,
            "counts": {k: sum(1 for v in tm.values() if v.get("tier") == k) for k in TIERS}}


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
    """Net USD inflow per bucket and tier (+ 'other' = everybody else) and distinct tiered buyers.
    Totals per bucket are summed in SQL; only the tiered wallets' trades are read row by row (a 24 h overview
    otherwise pulled every trade of the day, ~270k rows, into Python on each cache miss)."""
    tm = tier_map(conn)
    until = now_ts() + 60
    b = defaultdict(lambda: {"ultra": 0.0, "smart": 0.0, "good": 0.0, "other": 0.0, "buyers": set(), "n": 0})
    mf, margs = ("AND mint=?", (mint,)) if mint else ("", ())
    for r in rows(conn, f"""SELECT (ts/?)*? k, COUNT(*) n,
                                   SUM(CASE WHEN side='buy' THEN COALESCE(usd_amount,0) ELSE -COALESCE(usd_amount,0) END) net
                            FROM trades WHERE ts>=? AND ts<=? {mf} GROUP BY k""", (bucket, bucket, since, until, *margs)):
        b[r["k"]]["n"] = r["n"]
        b[r["k"]]["other"] = r["net"] or 0.0
    wallets = list(tm)
    for i in range(0, len(wallets), 400):
        chunk = wallets[i:i + 400]
        q = ",".join("?" * len(chunk))
        for t in rows(conn, f"""SELECT wallet, side, ts, usd_amount FROM trades
                               WHERE wallet IN ({q}) AND ts>=? AND ts<=? {mf}""", (*chunk, since, until, *margs)):
            k = (t["ts"] // bucket) * bucket
            tier = _tier(tm, t["wallet"])
            if tier == "other":
                continue
            v = (1 if t["side"] == "buy" else -1) * (t["usd_amount"] or 0.0)
            b[k][tier] += v
            b[k]["other"] -= v
            if t["side"] == "buy":
                b[k]["buyers"].add(t["wallet"])
    start = (since // bucket) * bucket
    end = (now_ts() // bucket) * bucket if not mint else max(b) if b else start
    out = []
    k = start
    while k <= end:
        d = b.get(k)
        out.append({"ts": k, "ultra": round(d["ultra"], 2) if d else 0, "smart": round(d["smart"], 2) if d else 0,
                    "good": round(d["good"], 2) if d else 0, "other": round(d["other"], 2) if d else 0,
                    "tiered_buyers": len(d["buyers"]) if d else 0, "n": d["n"] if d else 0})
        k += bucket
        if len(out) > 2000:
            break
    return out


def _mint_flows(conn, since, mints):
    """Per mint: tiered net usd, per-tier net usd, distinct tiered net buyers, all buy/sell usd.
    All-wallet totals are summed in SQL; only tiered wallets' trades are read row by row."""
    tm = tier_map(conn)
    mints = list(mints or [])
    until = now_ts() + 60
    per = defaultdict(lambda: {"net_tiered": 0.0, "ultra": 0.0, "smart": 0.0, "good": 0.0, "buy_all": 0.0,
                               "sell_all": 0.0, "w": defaultdict(float), "wt": {}, "traders": 0})
    for i in range(0, len(mints), 400):
        chunk = mints[i:i + 400]
        q = ",".join("?" * len(chunk))
        for r in rows(conn, f"""SELECT mint, COUNT(DISTINCT wallet) traders,
                                       SUM(CASE WHEN side='buy' THEN COALESCE(usd_amount,0) ELSE 0 END) buy_all,
                                       SUM(CASE WHEN side='buy' THEN 0 ELSE COALESCE(usd_amount,0) END) sell_all
                                FROM trades WHERE mint IN ({q}) AND ts>=? AND ts<=? GROUP BY mint""",
                            (*chunk, since, until)):
            d = per[r["mint"]]
            d["traders"], d["buy_all"], d["sell_all"] = r["traders"], r["buy_all"] or 0.0, r["sell_all"] or 0.0
    want = set(mints)
    wallets = list(tm)
    for i in range(0, len(wallets) if want else 0, 400):
        chunk = wallets[i:i + 400]
        q = ",".join("?" * len(chunk))
        for t in rows(conn, f"""SELECT wallet, mint, side, usd_amount FROM trades
                               WHERE wallet IN ({q}) AND ts>=? AND ts<=?""", (*chunk, since, until)):
            if t["mint"] not in want:
                continue
            tier = _tier(tm, t["wallet"])
            if tier == "other":
                continue
            d = per[t["mint"]]
            v = (1 if t["side"] == "buy" else -1) * (t["usd_amount"] or 0.0)
            d["net_tiered"] += v
            d[tier] += v
            d["w"][t["wallet"]] += v
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
                  "traders": d["traders"]}
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
            "outcome": {k: _f(s.get(k), 4) for k in ("ret_5m", "ret_15m", "ret_1h", "ret_6h", "max_ret_15m", "max_ret_1h",
                                                      "max_ret_4h", "max_ret_24h", "max_mcap_1h", "dd_before_peak_1h")}
                       | {"peak_min_1h": _f(s.get("peak_min_1h"), 1)},
            # manual trade plan sent with the alert (signals/plan.py): call number on this coin, target/stop and their hits
            "plan": {"call_no": s.get("call_no") or 1, "target_ret": _f(s.get("target_ret"), 4), "stop_ret": _f(s.get("stop_ret"), 4),
                     "target_hit_ts": s.get("target_hit_ts"), "stop_hit_ts": s.get("stop_hit_ts"), "move_since_last": None}}


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
    _recall_moves(conn, out)
    sm = safety_map(conn, [s["mint"] for s in out])
    for s in out:  # current state too: the buy button and "gesperrt" follow it, not the state at the signal
        s["safety_now"] = sm.get(s["mint"])
    return out


def _recall_moves(conn, out):
    """Re-calls (call_no > 1): price move since the previous call on the same coin, from the stored call prices."""
    re = [s for s in out if s["plan"]["call_no"] > 1 and s.get("price_usd")]
    if not re:
        return
    mints = sorted({s["mint"] for s in re})
    calls = defaultdict(list)
    for i in range(0, len(mints), 500):
        ch = mints[i:i + 500]
        for r in rows(conn, f"SELECT mint, ts, price_usd FROM signals WHERE mint IN ({','.join('?' * len(ch))}) ORDER BY ts", ch):
            calls[r["mint"]].append((r["ts"], r["price_usd"]))
    for s in re:
        ts = calls[s["mint"]]
        j = bisect.bisect_left(ts, (s["ts"],)) - 1
        if j >= 0 and ts[j][1]:
            s["plan"]["move_since_last"] = _f(s["price_usd"] / ts[j][1] - 1, 4)


def predictions(conn, mint, since=0, limit=2000):
    """Stored model prognoses for one coin (model_predictions), oldest first."""
    if not has(conn, "model_predictions"):
        return []
    sol = sol_usd(conn)
    r = rows(conn, "SELECT * FROM model_predictions WHERE mint=? AND ts>=? ORDER BY ts DESC LIMIT ?", (mint, since, limit))
    return [_norm_prediction(p, sol) for p in reversed(r)]


def signal_performance(conn, since=None):
    """How good were past signals: hit rate and returns per score bucket, per tier mix, per market cap."""
    all_items = signals(conn, since=since, limit=20000)
    items = [x for x in all_items if x["outcome"].get("max_ret_1h") is not None]

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
    # how many calls reached +50/+100/+150 % at their high within 15 min / 1 h / 4 h / 24 h (only calls with that window known)
    reach = []
    for lvl in (0.5, 1.0, 1.5):
        row = {"level": lvl}
        for key in ("max_ret_15m", "max_ret_1h", "max_ret_4h", "max_ret_24h"):
            known = [x["outcome"][key] for x in all_items if x["outcome"].get(key) is not None]
            hit = sum(1 for v in known if v >= lvl)
            row[key] = {"n": len(known), "hits": hit, "rate": _f(hit / len(known), 3) if known else None}
        reach.append(row)
    planned = [x for x in all_items if x["plan"].get("target_ret") is not None]
    pk = [x["outcome"]["peak_min_1h"] for x in all_items if x["outcome"].get("peak_min_1h") is not None]
    dd = [x["outcome"]["dd_before_peak_1h"] for x in all_items if x["outcome"].get("dd_before_peak_1h") is not None]
    manual = {"reach": reach, "planned": len(planned),
              "target_hits": sum(1 for x in planned if x["plan"]["target_hit_ts"]),
              "stop_hits": sum(1 for x in planned if x["plan"]["stop_hit_ts"]),
              "recalls": sum(1 for x in all_items if x["plan"]["call_no"] > 1),
              "median_peak_min_1h": _f(statistics.median(pk), 1) if pk else None,
              "median_dd_before_peak": _f(statistics.median(dd), 4) if dd else None}
    return {"pump_threshold": PUMP_1H, **grp(items), "manual": manual, "by_bucket": by_bucket, "by_ultra": by_ultra,
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


SAFETY_SCORE_DE = {"learned": "gelernt", "rules": "Regeln", "sniper": "Sniper-Risiko"}


def safety_quality(conn):
    """How well the rug protection really works, from its last learning run (safety/learn.py, safety_cache
    'learn_report'): walk-forward launch backtest per score (AUC, rugs caught at <=10 % doubler false alarms),
    rug rate per level, and the threshold sweep on tracked coins."""
    if not has(conn, "safety_cache"):
        return None
    r = one(conn, "SELECT ts, json FROM safety_cache WHERE key='learn_report'")
    rep = _j(r["json"], None) if r else None
    if not isinstance(rep, dict):
        return None
    bt = rep.get("launch_backtest") or {}
    scores = []
    for k, v in (bt.get("scores") or {}).items():
        op = (v or {}).get("at_10pct_fa") or {}
        scores.append({"key": k, "label": SAFETY_SCORE_DE.get(k, k), "auc": _f(v.get("auc"), 3),
                       "recall": _f(op.get("recall"), 3), "precision": _f(op.get("precision"), 3),
                       "rugs_caught": op.get("rugs_caught"), "rugs_total": op.get("rugs_total"),
                       "doublers_flagged": op.get("doublers_flagged"), "doublers_total": op.get("doublers_total"),
                       "threshold": op.get("threshold")})
    order = {"learned": 0, "rules": 1, "sniper": 2}
    scores.sort(key=lambda x: order.get(x["key"], 9))
    levels = [{"level": k, "level_de": SAFETY_LEVEL_DE.get(k, k), **(v or {})} for k, v in (rep.get("levels") or {}).items()]
    best = max((x for x in scores if x["auc"] is not None), key=lambda x: x["auc"], default=None)
    return {"ts": r["ts"], "learned_ts": rep.get("ts") or r["ts"], "n_labelled": rep.get("n_labelled"),
            "base_rug_rate": rep.get("base_rug_rate"), "horizon_h": rep.get("horizon_h"), "rug_drop_pct": rep.get("rug_drop_pct"),
            "backtest": {"n": bt.get("n"), "rugs": bt.get("rugs"), "doublers": bt.get("doublers"), "test_n": bt.get("test_n"),
                         "scores": scores, "best": best["key"] if best else None} if bt else None,
            "levels": levels, "thresholds": rep.get("thresholds") or {}, "launches": rep.get("launches")}


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
METRICS_RECENT_S = 6 * 3600


def _last_metrics(conn, now):
    """Newest signal_metrics row per mint. The table's key is (ts, mint), so 'latest per mint' over all of it is a
    full scan: the last 6 h come from a cheap ts range on every call, older coins from a full pass cached 10 min."""
    old = cache.get(("last_metrics_all",), 600, lambda: {r["mint"]: r for r in rows(conn, """
        SELECT m.* FROM signal_metrics m JOIN (SELECT mint, MAX(ts) ts FROM signal_metrics GROUP BY mint) x
        ON x.mint=m.mint AND x.ts=m.ts""")})
    out = dict(old)
    for r in rows(conn, "SELECT * FROM signal_metrics WHERE ts>=? ORDER BY ts", (now - METRICS_RECENT_S,)):
        out[r["mint"]] = r   # ascending, so the newest row per mint wins
    return out


def coins(conn, sort="strength", limit=100, q=None, window_min=30):
    if not has(conn, "tokens"):
        return []
    now = now_ts()

    def calc():
        toks = {t["mint"]: t for t in rows(conn, "SELECT * FROM tokens")}
        # coins that ever had a signal or a prognosis, plus tracked ones
        last_metric = _last_metrics(conn, now) if has(conn, "signal_metrics") else {}
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
    data = cache.get_swr(("coins", window_min), 15, calc)
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
        # score_token reads the coin's whole trade history: seconds on busy coins, so it runs in the background and
        # the page shows "wird berechnet" until the next poll
        r = cache.get_bg(("score_token", mint), 60, lambda: score_token(mint))
        if r is None:
            return {"pending": True}
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
WALLET_SORTS = {"net_pnl_usd", "net_roi", "score", "win_rate", "roi", "total_pnl_usd", "influence_followers", "influence_usd",
                "inflow_lift_usd", "n_tokens", "early_rate", "last_trade_ts", "copy_score", "exit_capture",
                "median_hold_min", "model_score", "score_heuristic", "cluster_n"}


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


def wallets(conn, tier=None, sort="score", q=None, limit=200, offset=0, include_untiered=False, cluster=None,
            group_clusters=False):
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
            b["model_score"] = b.get("score")
            out.append({**{k: b.get(k) for k in ("address", "tier", "score", "n_tokens", "win_rate", "early_rate", "roi",
                                                  "realized_pnl_usd", "total_pnl_usd", "median_entry_mcap",
                                                  "median_min_to_2x", "influence_followers", "influence_usd",
                                                  "inflow_lift_usd", "consistency", "rug_rate", "cluster_id",
                                                  "exit_capture", "median_hold_min", "net_pnl_usd", "net_roi")},
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
    # wallets.tier is the one truth once synced: tier, its source/version, model and heuristic score per wallet
    wcols = tables(conn).get("wallets", set()) if has(conn, "wallets") else set()
    if "tier_source" in wcols:
        addrs = [w["address"] for w in out]
        wrow = {}
        for i in range(0, len(addrs), 500):
            ch = addrs[i:i + 500]
            for r in rows(conn, f"""SELECT address, tier, tier_source, tier_version, model_score, score, cluster_id, exit_capture
                                    FROM wallets WHERE address IN ({','.join('?' * len(ch))})""", ch):
                wrow[r["address"]] = r
        synced = _wallets_synced(conn)
        known = set(addrs)
        if synced:  # tiered in wallets but not in the model table (manual wallets stay smart)
            for r in rows(conn, """SELECT address, tier, tier_source, tier_version, model_score, score, cluster_id, exit_capture,
                                          tokens_traded n_tokens, win_rate, early_rate, roi, realized_pnl_usd, total_pnl_usd,
                                          last_trade_ts FROM wallets WHERE tier IS NOT NULL"""):
                if r["address"] not in known:
                    wrow[r["address"]] = r
                    out.append({k: r[k] for k in ("address", "n_tokens", "win_rate", "early_rate", "roi", "realized_pnl_usd",
                                                  "total_pnl_usd", "last_trade_ts")})
        for w in out:
            r = wrow.get(w["address"])
            if not r:
                continue
            if synced:
                w["tier"] = r["tier"]
            w["tier_source"] = r.get("tier_source")
            w["tier_version"] = r.get("tier_version")
            w["score_heuristic"] = _f(r.get("score"), 1)
            if r.get("model_score") is not None:
                w["model_score"] = r["model_score"]
            for k in ("cluster_id", "exit_capture"):
                if w.get(k) is None and r.get(k) is not None:
                    w[k] = r[k]
            if w.get("score") is None:
                w["score"] = w.get("model_score") if w.get("model_score") is not None else r.get("score")
        if not include_untiered:
            out = [w for w in out if w.get("tier")]
    elif has(conn, "wallets"):
        hs = {r["address"]: r["score"] for r in rows(conn, "SELECT address, score FROM wallets WHERE score>0")} if src == "model" else {}
        for w in out:
            if src == "model":
                w["score_heuristic"] = _f(hs.get(w["address"]), 1)
                w["model_score"] = w.get("score")
                w["tier_source"] = "model"
            else:
                w["score_heuristic"] = w.get("score")
                w["tier_source"] = "heuristic"
    # wallets with the same cluster_id are one entity (same funder): size per cluster
    csize = defaultdict(int)
    for w in out:
        if w.get("cluster_id"):
            csize[w["cluster_id"]] += 1
    for w in out:
        w["cluster_n"] = csize.get(w.get("cluster_id"), 0) or None
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
        out = [w for w in out if ql in w["address"].lower() or ql in (w["label"] or "").lower()
               or (w.get("cluster_id") and ql == str(w["cluster_id"]).lower())]
    if cluster:
        out = [w for w in out if str(w.get("cluster_id") or "") == cluster]
    sort = sort if sort in WALLET_SORTS else "score"
    out.sort(key=lambda w: (w.get(sort) is not None, w.get(sort) or 0), reverse=True)
    if group_clusters:  # one row per entity: the best wallet of each cluster carries the group size
        seen, grouped = set(), []
        for w in out:
            c = w.get("cluster_id")
            if c and (w.get("cluster_n") or 0) > 1:
                if c in seen:
                    continue
                seen.add(c)
            grouped.append(w)
        out = grouped
    total = len(out)
    page = out[offset:offset + limit]
    edges = copy_edges([w["address"] for w in page]) if page and limit <= 500 else {}
    for w in page:
        w["copy_edge"] = (edges or {}).get(w["address"])
    return {"tier_source": src, "tier_info": tier_info(conn), "total": total, "counts": counts,
            "edges_pending": edges is None, "items": page}


def _edge_view(e):
    """copytrade.learn.wallet_edge in dashboard form: mean gain per copied buy after costs and its lower bound
    (mean minus one standard error); proven = even the lower bound is positive (the bot's own gate)."""
    if not e or not e.get("n"):
        return None
    return {"n": e["n"], "n_eff": _f(e.get("n_eff"), 1), "mean": _f(e.get("mean"), 4), "lower": _f(e.get("lower"), 4),
            "size_factor": _f(e.get("size_factor"), 2), "extra_cost": _f(e.get("extra_cost"), 4),
            "proven": (e.get("lower") or 0) > 0}


def copy_edges(addresses):
    """{address: edge view} for a page of wallets. wallet_edge replays every past buy of a whale, so a whole page
    runs in one background job (cached 5 min); None while it is still being computed."""
    try:
        from copytrade import learn as ct_learn  # type: ignore
    except Exception:
        return {}
    addrs = tuple(sorted(set(addresses)))

    def calc():
        from .db import read_conn
        c = read_conn()
        if not has(c, "ct_evals"):
            return {}
        out = {}
        for a in addrs:
            try:
                out[a] = _edge_view(ct_learn.wallet_edge(c, a))
            except Exception:
                out[a] = None
        return out
    return cache.get_bg(("copy_edges", addrs), 300, calc)


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
    return {"tier_source": tier_source(conn), "tier_info": tier_info(conn),
            "total_wallets": scalar(conn, "SELECT COUNT(*) FROM wallets", default=0),
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
            "copy_rank": ct, "copy_outcomes": outc, "copies": copies, "copy_tracked": tracked, "pnl_curve": cum,
            "edge": _wallet_edge_compare(conn, address, mstats)}


def _wallet_edge_compare(conn, address, mstats):
    """The whale's own result after costs (model: net_pnl_usd, net_roi) next to what copying it earns us
    (copytrade.learn.wallet_edge), with the reason a profitable whale may still not be copied."""
    try:
        from copytrade import learn as ct_learn  # type: ignore
        e = _edge_view(ct_learn.wallet_edge(conn, address)) if has(conn, "ct_evals") else None
    except Exception:
        e = None
    own_pnl = (mstats or {}).get("net_pnl_usd")
    own_roi = (mstats or {}).get("net_roi")
    if e is None:
        verdict = "Noch keine nachgespielten Käufe: der Copy-Trader hat diesen Wal noch nicht ausgewertet."
    elif e["proven"]:
        verdict = "Kopieren lohnt sich nachweislich: auch die vorsichtige Schätzung ist im Plus."
    elif (own_pnl or 0) > 0:
        verdict = ("Der Wal verdient selbst, aber unsere Kopie nicht sicher: mit unserer Verzögerung, unseren Ausstiegen "
                   "und Kosten bleibt zu wenig übrig. Der Bot kopiert ihn deshalb nicht.")
    else:
        verdict = "Weder der Wal noch unsere Kopie sind nach Kosten im Plus."
    return {"own_pnl_usd": _f(own_pnl, 2), "own_roi": _f(own_roi, 4), "copy": e, "verdict": verdict}


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
    try:  # live/ rehearses while LIVE_DRY_RUN is not false: 'live' switched on then means dry run, nothing is sent
        from live.config import load as live_load  # type: ignore
        out["live_dry_run"] = bool(live_load().dry_run)
    except Exception:
        out["live_dry_run"] = False
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


# ct_positions.status besides open/closed: opening = intent row before the swap, failed = live buy that bought nothing;
# pending_buy/pending_sell/stuck come with live trading. In-flight rows are listed with the open ones.
# copytrade books: strict paper (the live rules), explore (deliberately wider, to learn; copytrade/strategy.py), live
CT_ACCOUNTS = ("paper", "explore", "live")
POS_IN_FLIGHT = ("open", "opening", "closing", "pending_buy", "pending_sell", "stuck")


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
        a_["account"] = (_j(a_.get("data"), {}) or {}).get("account") if isinstance(a_.get("data"), str) else None
        a_["data"] = None  # can be large; the alert row itself carries the essentials
    events = rows(conn, "SELECT ts, level, kind, message FROM ct_events ORDER BY ts DESC LIMIT 200") if has(conn, "ct_events") else []
    cmds = rows(conn, "SELECT * FROM ct_commands ORDER BY id DESC LIMIT 30") if has(conn, "ct_commands") else []
    balance = None
    if mode in ("paper", "explore") and has(conn, "ct_state"):
        v = scalar(conn, "SELECT value FROM ct_state WHERE key=?", (f"{mode}_balance_sol",))
        balance = _f(v, 4) if v is not None else None
    return {"available": True, "mode": mode, "strategy": strategy, "summary": summ, "by_strategy": by_strategy,
            "balance_sol": balance, "equity": eq, "pnl_curve": curve,
            "open": [p for p in ps if p.get("status") in POS_IN_FLIGHT][::-1],
            "failed": [p for p in ps if p.get("status") == "failed"][::-1][:100],
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
            "tier_source": tier_source(conn), "tier_info": tier_info(conn), "retrain": retrain_schedule(conn),
            "signal_formula": [{"feature": f, "label": lab, "weight": w} for f, lab, w in
                               (("inflow", "Netto-Inflow Smart Money", 0.4), ("breadth", "Anzahl Smart Wallets", 0.3),
                                ("quality", "Ø Wallet-Qualität", 0.3))],
            "performance": signal_performance(conn)}


_RETRAIN_RE = None


def retrain_schedule(conn, now=None):
    """When the research service retrains next: the last "next retrain at" line of logs/research.log, else
    the active version's time + MODEL_RETRAIN_HOURS. due = the time has passed (retrain runs in the next cycle)."""
    import calendar
    import re
    global _RETRAIN_RE
    _RETRAIN_RE = _RETRAIN_RE or re.compile(r"next retrain at (\d{4}-\d\d-\d\d \d\d:\d\d) UTC")
    now = now or time.time()
    try:
        from model.config import msettings  # type: ignore
        every_h = float(msettings.retrain_hours)
    except Exception:
        try:
            every_h = float(os.getenv("MODEL_RETRAIN_HOURS", "6"))
        except ValueError:
            every_h = 6.0
    last_ok = scalar(conn, "SELECT MAX(ts) FROM model_versions WHERE active=1") if has(conn, "model_versions") else None
    out = {"every_h": every_h, "last_ok": last_ok, "next_ts": None, "source": None, "failed_ts": None}
    path = os.path.join(os.getenv("MAYHAM_LOG_DIR") or os.path.join(ROOT_DIR, "logs"), "research.log")
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 256 * 1024))
            tail = f.read().decode("utf-8", "replace")
        hits = _RETRAIN_RE.findall(tail)
        if hits:
            out["next_ts"] = calendar.timegm(time.strptime(hits[-1], "%Y-%m-%d %H:%M"))
            out["source"] = "log"
        out["failed"] = tail.rfind("retrain failed") > tail.rfind("next retrain at")
    except OSError:
        pass
    if out["next_ts"] is None and last_ok:
        out["next_ts"], out["source"] = int(last_ok + every_h * 3600), "berechnet"
    out["due"] = bool(out["next_ts"] and out["next_ts"] <= now)
    return out


# ====================================================================== launch sniper (sniper/, paper only)
SNIPER_SOURCE = {"pumpportal": "pump.fun", "migration": "Migration", "geckoterminal": "neuer Pool"}


def _sniper_cfg():
    try:
        from sniper.config import settings as scfg  # type: ignore
        return scfg
    except Exception:
        return None


def _pid_running(name):
    try:
        pid = int(open(os.path.join(ROOT_DIR, "data", "pids", f"{name}.pid")).read().strip())
    except Exception:
        return None  # unknown (no pid file: launchd, other machine, tests)
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


SNIPER_RPC_WS_MAX_SUBS = 150  # logsSubscribe per websocket (sniper/README.md)
SNIPER_HB_STALE_S = 30      # the service writes every 10 s; older means it is stopped
_SNIPER_HIST = []           # (ts, counters) samples of the heartbeat, for per-hour rates (counters are totals since start)


def _sniper_rates(ts, counters):
    """Per-hour rates from the heartbeat counters over the samples seen in the last hour; None until 60 s are covered."""
    if not isinstance(counters, dict):
        return None
    if _SNIPER_HIST and (ts < _SNIPER_HIST[-1][0] or any(counters.get(k, 0) < v for k, v in _SNIPER_HIST[-1][1].items())):
        _SNIPER_HIST.clear()   # service restarted: counters began again at 0
    if not _SNIPER_HIST or ts > _SNIPER_HIST[-1][0]:
        _SNIPER_HIST.append((ts, dict(counters)))
    while len(_SNIPER_HIST) > 2 and _SNIPER_HIST[1][0] <= ts - 3600:
        _SNIPER_HIST.pop(0)
    t0, c0 = _SNIPER_HIST[0]
    span = ts - t0
    if span < 60:
        return None
    return {"span_s": span, **{k: _f((v - c0.get(k, 0)) * 3600 / span, 1) for k, v in counters.items()}}


def sniper_stream(conn, now=None):
    """Data-stream status from the service's own heartbeat (sniper_state 'service_status', every 10 s): connected,
    budget per minute, counters. Without a fresh heartbeat the service counts as stopped. Per-hour numbers come from
    the counters once a minute of samples exists, until then from the stored launches."""
    now = int(now or time.time())
    hb = _j(scalar(conn, "SELECT value FROM sniper_state WHERE key='service_status'"), None) \
        if has(conn, "sniper_state") else None
    hb = hb if isinstance(hb, dict) and hb.get("ts") else None
    last = scalar(conn, "SELECT MAX(COALESCE(created_ts, decided_ts)) FROM sniper_launches") or None
    lh = scalar(conn, "SELECT COUNT(*) FROM sniper_launches WHERE COALESCE(created_ts, decided_ts) >= ?", (now - 3600,), 0)
    dh = scalar(conn, "SELECT COUNT(*) FROM sniper_launches WHERE stage=2 AND decided_ts >= ?", (now - 3600,), 0)
    eh = scalar(conn, "SELECT COUNT(*) FROM sniper_launches WHERE decision='enter' AND decided_ts >= ?", (now - 3600,), 0)
    cfg = _sniper_cfg()
    out = {"last_launch_ts": last, "launches_1h": lh, "deep_1h": dh, "entered_1h": eh, "rate_source": "db",
           "deep_per_hour_cap": cfg.deep_per_min * 60 if cfg else None, "running": _pid_running("sniper"),
           "heartbeat_ts": hb["ts"] if hb else None, "heartbeat_age_s": now - hb["ts"] if hb else None}
    fresh = bool(hb) and now - hb["ts"] <= SNIPER_HB_STALE_S
    out["service_up"] = fresh
    if not hb:
        out.update(source="none", connected=False)
    elif not fresh:
        out.update(source="stale", connected=False)
    else:
        budget = {}
        for k, b in (hb.get("budget") or {}).items():
            if isinstance(b, dict):
                pm = b.get("per_min") or 0
                budget[k] = {"used_last_min": b.get("used_last_min"), "per_min": pm, "total": b.get("total"),
                             "load": _f((b.get("used_last_min") or 0) / pm, 3) if pm else None}
        out.update(source="heartbeat", connected=bool(hb.get("connected")), ws_msgs=hb.get("ws_msgs"),
                   subscribed=hb.get("subscribed"), in_memory=hb.get("in_memory"), queue=hb.get("queue"),
                   open_positions=hb.get("open_positions"), pump_api_ok=hb.get("pump_api_ok"),
                   counters=hb.get("counters") or {}, budget=budget, trade_source=hb.get("trade_source"),
                   rpc_ws_connected=hb.get("rpc_ws_connected"), rpc_ws_subs=hb.get("rpc_ws_subs"),
                   rpc_ws_msgs=hb.get("rpc_ws_msgs"), active_launch_source=hb.get("active_launch_source"),
                   launches_per_min=hb.get("launches_per_min"),
                   launch_sources=[{"source": k, **v}
                                   for k, v in (hb.get("launch_sources") or {}).items() if isinstance(v, dict)],
                   rpc_ws_max_subs=getattr(cfg, "ws_max_subs", None) or SNIPER_RPC_WS_MAX_SUBS)
        if out["launch_sources"]:  # two launch streams (PumpPortal + RPC logs): connected when either delivers
            out["connected"] = out["connected"] or any(x.get("connected") for x in out["launch_sources"])
        rates = _sniper_rates(hb["ts"], hb.get("counters"))
        if rates:
            out.update(rate_source="counters", rate_span_s=rates["span_s"], migrations_1h=rates.get("migrations"),
                       launches_1h=rates.get("creates", lh), deep_1h=rates.get("stage2", dh), entered_1h=rates.get("entries", eh))
    cap = out["deep_per_hour_cap"]
    deep = (out.get("budget") or {}).get("deep")
    out["load_deep"] = deep["load"] if deep and deep.get("load") is not None else (_f(out["deep_1h"] / cap, 3) if cap else None)
    out["api"] = [r for r in api_usage().get("rows", []) if r["service"] == "sniper"]
    return out


def _credit_borrow(chttp, cred, svc, c):
    """A service past its own Helius share may borrow what others leave unused today (core.http._borrowable,
    HELIUS_BORROW_PCT): how much it borrowed, how much it may still take, and who lends most."""
    own = (c.get("credits_day_budget") or 0) * getattr(chttp, "CREDIT_BURST", {}).get(svc, 1.0)
    out = {"borrowed": _f(max(0.0, (c.get("credits_today") or 0) - own), 0), "borrow_left": None, "lenders": []}
    try:
        out["borrow_left"] = _f(chttp._borrowable(chttp._ledger_read(), svc), 0)
    except Exception:
        pass
    spare = sorted(((s, (v.get("credits_day_budget") or 0) - (v.get("credits_today") or 0)) for s, v in (cred or {}).items()
                    if s != svc and isinstance(v, dict)), key=lambda x: -x[1])
    out["lenders"] = [s for s, left in spare if left > 0][:2]
    return out


# live_orders.status (live/store.py, live/executor.py) in German with a tone for the badge
LIVE_ORDER_STATUS = {
    "new": ("angelegt", "warn"), "sending": ("wird gesendet", "warn"), "unknown": ("unklar, blockiert Käufe", "bad"),
    "landed": ("gelandet", "good"), "landed_late": ("spät gelandet, wird gebucht", "good"),
    "landed_unbooked": ("gelandet ohne Buchung, Wallet prüfen", "bad"), "failed": ("auf der Chain fehlgeschlagen", "bad"),
    "expired": ("verfallen, nie gelandet", "bad"), "sim_failed": ("Vorab-Simulation abgelehnt, nichts gesendet", ""),
    "rejected": ("vor dem Senden abgelehnt", ""), "dry_ok": ("Dry-Run simuliert ok", "good"),
    "dry_sim_failed": ("Dry-Run Simulation fehlgeschlagen", "bad"), "dry_built": ("Dry-Run Verkauf gebaut", "good"),
}


def live_overview(conn):
    """The real-money path at a glance (live/): off / dry run / real money, the go-live checklist, hard limits,
    kill switch, exposure per book and the newest orders. Needs a writable connection because
    live.readiness.report creates live_state/live_orders if missing. No key, URL or .env value leaves here:
    the wallet shows as its first 6 characters (as the report does), errors are redacted."""
    try:
        from live import config as lcfg, guard, readiness, store  # type: ignore
    except Exception as e:
        return {"available": False, "error": _redact(str(e))[:200]}
    cfg = lcfg.load()
    rep = readiness.report(conn, cfg)
    state = "off" if not cfg.enabled else ("dry" if cfg.dry_run else "live")
    books = {}
    for m in ("dry", "live"):
        op = guard.open_positions(conn, m)
        books[m] = {"open": len(op), "exposure_sol": _f(guard.exposure(conn, m), 4), "pnl_24h_sol": _f(guard.loss_24h(conn, m), 4)}
    orders = []
    for o in store.recent(conn, 30):
        lab, tone = LIVE_ORDER_STATUS.get(o.get("status"), (o.get("status") or "–", ""))
        orders.append({k: o.get(k) for k in ("id", "ts", "dry", "side", "mint", "status", "attempt", "price_impact", "slippage_bps",
                                             "realized_slippage", "done_ts")}
                      | {"status_de": lab, "tone": tone, "sig": (o.get("sig") or "")[:12] or None,
                         "error": _redact(o.get("error") or o.get("sim_err") or "")[:200] or None,
                         "fee_sol": _f((o.get("fee_lamports") or 0) / 1e9, 6) if o.get("fee_lamports") else None})
    since = int(time.time()) - 86400
    counts = {r["status"]: r["n"] for r in rows(conn, "SELECT status, COUNT(*) n FROM live_orders WHERE ts>=? GROUP BY status", (since,))}
    checks = [{**c, "detail": _redact(c.get("detail") or "")} for c in rep["checks"]]
    # orders that need a human: fate unknown (blocks buys) or landed on chain without a booked position
    attention = {r["status"]: r["n"] for r in rows(conn, "SELECT status, COUNT(*) n FROM live_orders WHERE dry=0 AND "
                                                      "status IN ('unknown','landed_unbooked') GROUP BY status")}
    return {"available": True, "state": state, "enabled": cfg.enabled, "dry_run": cfg.dry_run, "require_gate": cfg.require_gate,
            "ready": rep["ready"], "checks": checks, "limits": rep["limits"], "killed": store.killed(conn) or None,
            "consecutive_fails": store.consecutive_fails(conn), "max_consecutive_fails": cfg.max_consecutive_fails,
            "pending": len(store.pending(conn)), "books": books, "dry": rep["dry"], "live": rep["live"], "advance": rep["advance"],
            "orders": orders, "counts_24h": counts, "attention": attention, "heartbeat": int(float(store.get(conn, "heartbeat") or 0)) or None}


def _redact(text):
    """Strip API keys and tokens from free text (error messages can carry request URLs)."""
    try:
        from core.http import redact  # type: ignore
        text = redact(text)
    except Exception:
        text = re.sub(r"((?:api[-_]?key|apikey|token)=)[^&\s'\"]+", r"\1***", str(text), flags=re.I)
    return re.sub(r"(https?://[^/\s?]+)[^\s'\"]*", r"\1", str(text))  # keep the host, drop paths and queries


def sniper_check_errors(conn, mints):
    """Errors of the deep check per launch (features_json.errors, sniper/analyze.py), redacted, at most 6 each."""
    out = {}
    mints = list(dict.fromkeys(m for m in mints if m))
    for i in range(0, len(mints), 500):
        part = mints[i:i + 500]
        for r in rows(conn, f"SELECT mint, features_json FROM sniper_launches WHERE mint IN ({','.join('?' * len(part))}) "
                            "AND features_json LIKE '%\"errors\"%'", part):
            e = (_j(r["features_json"], {}) or {}).get("errors") or []
            if isinstance(e, list) and e:
                out[r["mint"]] = [_redact(x)[:160] for x in e[:6]]
    return out


def sniper_view(conn, hours=24, limit=300):
    """Everything for the Launch-Sniper page. Read-only: the sniper trades on paper only."""
    if not has(conn, "sniper_launches"):
        return {"available": False, "note": "Der Launch-Sniper hat noch keine Daten (Dienst sniper läuft noch nicht)."}
    try:
        import sniper  # type: ignore
        from sniper.analytics import EXIT_LABELS  # type: ignore
    except Exception as e:
        return {"available": False, "note": f"sniper/ fehlt: {e}"[:200]}
    now = now_ts()
    st = sniper.stats(conn, hours=hours, now=now)
    cfg = _sniper_cfg()
    size = cfg.size_sol if cfg else 0.1
    start = cfg.paper_start_sol if cfg else None
    # filter value: what each skip reason would have earned with the same stake, held 60 min (no fees)
    per = defaultdict(lambda: {"sum": 0.0, "n": 0})
    for r in rows(conn, "SELECT skip_reason, ret_60m FROM sniper_launches WHERE decision='skip' AND decided_ts>=? "
                        "AND ret_60m IS NOT NULL", (now - int(hours * 3600) if hours else 0,)):
        d = per[r["skip_reason"] or "?"]
        d["sum"] += max(-1.0, r["ret_60m"])
        d["n"] += 1
    for f in st.get("filters", []):
        d = per.get(f["code"], {"sum": 0.0, "n": 0})
        f["avg_ret_60m"] = _f(d["sum"] / d["n"], 4) if d["n"] else None
        f["pnl_without_sol"] = _f(d["sum"] * size, 4) if d["n"] else None   # bought anyway: result
        f["filter_value_sol"] = _f(-d["sum"] * size, 4) if d["n"] else None  # positive = the filter saved money
    without = sum(d["sum"] for d in per.values()) * size
    paper = st.get("paper") or {}
    st["filter_total"] = {"with_sol": _f(paper.get("pnl_sol"), 4), "skipped_sol": _f(without, 4),
                          "without_sol": _f((paper.get("pnl_sol") or 0) + without, 4), "size_sol": size,
                          "measured": sum(d["n"] for d in per.values())}
    launches = sniper.recent(conn, limit=limit)
    for x in launches:
        x["source_label"] = SNIPER_SOURCE.get(x.get("source"), x.get("source") or "–")
        if x.get("migrated") and x.get("source") == "pumpportal":
            x["source_label"] = "pump.fun → migriert"
    errs = sniper_check_errors(conn, [x["mint"] for x in launches])
    for x in launches:
        x["check_errors"] = errs.get(x["mint"], [])
    reasons_by_mint = {x["mint"]: x for x in launches}
    pos = rows(conn, "SELECT * FROM sniper_positions ORDER BY COALESCE(closed_ts, opened_ts) DESC LIMIT 300")
    missing = [p["mint"] for p in pos if p["mint"] not in reasons_by_mint]
    if missing:
        for r in rows(conn, f"SELECT mint, score, reasons_json, positives_json FROM sniper_launches WHERE mint IN ({','.join('?' * len(missing))})", missing):
            reasons_by_mint[r["mint"]] = {"score": r["score"], "reasons": _j(r["reasons_json"], []) or [],
                                          "positives": _j(r["positives_json"], []) or []}
    positions = []
    for p in pos:
        L = reasons_by_mint.get(p["mint"]) or {}
        ev = _j(p.get("events_json"), []) or []
        pp = {k: p.get(k) for k in ("id", "mint", "symbol", "mode", "opened_ts", "closed_ts", "status", "entry_mcap_sol",
                                    "entry_mcap_usd", "size_sol", "fees_sol", "fill_src", "score", "tp1_done",
                                    "realized_sol", "exit_reason", "pnl_sol", "pnl_pct", "entry_price_sol", "last_price_sol",
                                    "peak_price_sol")}
        pp["exit_label"] = EXIT_LABELS.get(p.get("exit_reason"), p.get("exit_reason"))
        pp["entry_why"] = (L.get("positives") or [])[:3]
        pp["sells"] = [{"ts": e.get("ts"), "reason": EXIT_LABELS.get(e.get("reason"), e.get("reason")), "sol": e.get("sol")}
                       for e in ev if e.get("kind") == "sell"]
        if p["status"] == "open" and p.get("entry_price_sol") and p.get("last_price_sol"):
            pp["unrealized_pct"] = _f(p["last_price_sol"] / p["entry_price_sol"] - 1, 4)
        if p.get("peak_price_sol") and p.get("entry_price_sol"):
            pp["peak_pct"] = _f(p["peak_price_sol"] / p["entry_price_sol"] - 1, 4)
        positions.append(pp)
    # paper balance over time: start + realized results in closing order
    curve, acc = [], start
    if start is not None:
        closed = sorted((p for p in positions if p["status"] == "closed" and p.get("closed_ts")), key=lambda p: p["closed_ts"])
        first = min([p["opened_ts"] for p in positions if p.get("opened_ts")] or [now])
        curve.append({"ts": first, "balance_sol": start})
        for p in closed:
            acc += p.get("pnl_sol") or 0
            curve.append({"ts": p["closed_ts"], "balance_sol": _f(acc, 4), "symbol": p.get("symbol"), "pnl_sol": p.get("pnl_sol")})
    return {"available": True, "hours": hours, "stats": st, "launches": launches, "positions": positions,
            "balance_curve": curve, "paper_start_sol": start, "stream": sniper_stream(conn, now),
            "settings": {k: getattr(cfg, k, None) for k in ("min_score", "max_risk", "size_sol", "deep_per_min",
                                                             "max_open", "max_entries_per_hour")} if cfg else {},
            "sources": SNIPER_SOURCE}


def sniper_summary(conn, now=None):
    """Overview tile and terminal line: launches today, paper result, last buy."""
    if not has(conn, "sniper_launches"):
        return None
    now = int(now or time.time())
    day0 = now - now % 86400
    out = {"launches_today": scalar(conn, "SELECT COUNT(*) FROM sniper_launches WHERE COALESCE(created_ts, decided_ts)>=?", (day0,), 0),
           "entered_today": scalar(conn, "SELECT COUNT(*) FROM sniper_launches WHERE decision='enter' AND decided_ts>=?", (day0,), 0)}
    if has(conn, "sniper_positions"):
        out["pnl_sol"] = _f(scalar(conn, "SELECT SUM(pnl_sol) FROM sniper_positions WHERE status='closed'", default=0) or 0, 4)
        out["closed"] = scalar(conn, "SELECT COUNT(*) FROM sniper_positions WHERE status='closed'", default=0)
        out["open"] = scalar(conn, "SELECT COUNT(*) FROM sniper_positions WHERE status='open'", default=0)
        lb = one(conn, "SELECT mint, symbol, opened_ts FROM sniper_positions ORDER BY opened_ts DESC LIMIT 1")
        out["last_buy"] = lb
    if has(conn, "sniper_state"):
        v = scalar(conn, "SELECT value FROM sniper_state WHERE key='paper_balance_sol'")
        out["balance_sol"] = _f(float(v), 4) if v is not None else None
        hb = _j(scalar(conn, "SELECT value FROM sniper_state WHERE key='service_status'"), None)
        hb = hb if isinstance(hb, dict) and hb.get("ts") else None
        out["service_up"] = bool(hb) and now - hb["ts"] <= SNIPER_HB_STALE_S
        out["connected"] = out["service_up"] and bool(hb.get("connected"))
        out["heartbeat_ts"] = hb["ts"] if hb else None
    return out


# ====================================================================== public RPC pool
RPC_SLOW_MS = 1000


def rpc_pool(now=None, usage=None):
    """Keyless public RPC endpoints (core.http.pool_status) with this window's calls per endpoint host.

    Rows: host, state (aktiv | pausiert | ungeprüft), disabled_until, reason, latency_ms (mean over the last
    10 min when calls were made, else the probe), calls/429/live/bulk in the window summed over services.
    Only hosts are returned, never URLs (a custom PUBLIC_RPC_URLS entry could carry a key)."""
    try:
        from core import http as chttp  # type: ignore
        st = chttp.pool_status() or {}
    except Exception as e:  # older core.http without the pool
        return {"available": False, "note": f"core.http.pool_status fehlt: {e}"[:200], "rows": []}
    now = now or time.time()
    usage = usage if usage is not None else api_usage(now)
    rows_ = []
    for host, d in st.items():
        us = [r for r in usage.get("rows", []) if r.get("group") == host]
        wsum = lambda k: sum(r.get(k) or 0 for r in us) if any(r.get(k) is not None for r in us) else None  # noqa: E731
        lat_w = [(r["latency_ms_window"], r.get("calls_window") or 0) for r in us if r.get("latency_ms_window")]
        lat = (sum(a * b for a, b in lat_w) / sum(b for _, b in lat_w)) if lat_w and sum(b for _, b in lat_w) else None
        until = d.get("disabled_until") or 0
        state = "pausiert" if until > now else "aktiv" if d.get("ok") or d.get("checked_at") else "ungeprüft"
        live, bulk = wsum("live_calls_window"), wsum("bulk_calls_window")
        lb = (live or 0) + (bulk or 0)
        rows_.append({"host": host, "state": state, "disabled_until": int(until) if until > now else None,
                      "reason": (d.get("reason") or "")[:80] if state == "pausiert" else "",
                      "checked_at": int(d["checked_at"]) if d.get("checked_at") else None,
                      "latency_ms": _f(lat if lat is not None else d.get("latency_ms"), 0),
                      "latency_src": "Aufrufe" if lat is not None else "Probe" if d.get("latency_ms") else None,
                      "calls_window": wsum("calls_window"), "429_window": wsum("429_window"),
                      "live_window": live, "bulk_window": bulk, "live_share": _f(live / lb, 3) if lb else None,
                      "services": sorted({r["service"] for r in us if (r.get("calls") or 0) > 0})})
    act = [r for r in rows_ if r["state"] != "pausiert"]
    lats = [r["latency_ms"] for r in act if r["latency_ms"] is not None]
    fastest = min(act, key=lambda r: r["latency_ms"] if r["latency_ms"] is not None else 1e12) if lats else None
    return {"available": True, "rows": rows_, "active": len(act), "total": len(rows_), "slow_ms": RPC_SLOW_MS,
            "fastest_host": fastest["host"] if fastest else None, "fastest_ms": fastest["latency_ms"] if fastest else None,
            "window_s": max((r.get("window_s") or 0 for r in usage.get("rows", [])), default=0)}


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
    for mode in CT_ACCOUNTS:
        if has(conn, "ct_positions"):
            ps = [_pos(p) for p in rows(conn, "SELECT * FROM ct_positions WHERE mode=?", (mode,))]
            if ps or mode == "paper":
                ct[mode] = _summary(ps)
                eq = rows(conn, "SELECT ts, equity_sol FROM ct_equity WHERE mode=? ORDER BY ts", (mode,)) if has(conn, "ct_equity") else []
                if len(eq) > 300:
                    eq = eq[:: len(eq) // 300 + 1] + [eq[-1]]
                ct[mode]["equity"] = eq
                ct[mode]["equity_now"] = eq[-1]["equity_sol"] if eq else None
    sn, mdl = sniper_summary(conn, now), model_active(conn)
    return {
        "now": now, "tier_source": tier_source(conn), "tiers": tiers, "sniper": sn,
        "wallets_total": scalar(conn, "SELECT COUNT(*) FROM wallets", default=0),
        "tokens_tracked": scalar(conn, "SELECT COUNT(*) FROM tokens WHERE tracked=1", default=0),
        "trades_24h": sum(f["n"] for f in flows),  # same 24 h window flow_series already read
        "signals_24h": len(sigs),
        "net_tiered_24h": _f(net_24, 0), "flows": flows, "signal_perf": {k: perf[k] for k in ("n", "hits", "hit_rate", "pump_threshold", "median_max_1h")},
        "feed": sigs[:25], "top_coins": coins(conn, sort="strength", limit=12),
        "copytrade": ct, "control": control_state(conn), "model": mdl,
        "last_trade_ts": last_trade_ts(conn),
        "board": situation(conn, now, sniper=sn, ct=ct, model=mdl, signals_24h=len(sigs), perf=perf),
    }


def situation(conn, now=None, sniper=None, ct=None, model=None, signals_24h=None, perf=None):
    """Start-page situation board: one line per service with its state (services_health), a headline number and
    the page it links to, plus open problems (watchdog, backup, stopped services). Reuses what overview() already
    computed; the extra reads are single-row lookups."""
    now = int(now or time.time())
    health = {h["name"]: h for h in cache.get(("health", "board"), 10, lambda: services_health(conn, now))}
    out = []

    def add(name, page, headline, detail=""):
        h = health.get(name)
        if not h:
            return
        out.append({"name": name, "label": h["label"], "state": h["state"], "last_ts": h["last_ts"], "page": page,
                    "headline": headline, "detail": detail})

    add("signals", "/signals", f"{signals_24h or 0} Signale in 24 h",
        f"Trefferquote {round(100 * perf['hit_rate'])} %" if perf and perf.get("hit_rate") is not None else "")
    paper = (ct or {}).get("paper") or {}
    add("copytrade", "/copytrade", f"Papier streng {paper['pnl_sol']:+.2f} SOL".replace(".", ",") if paper.get("pnl_sol") is not None else "noch keine Trades",
        (f"{paper.get('open') or 0} offen · {paper.get('trades') or 0} Trades" if paper else "") +
        (f" · Erkundung {ex['pnl_sol']:+.2f} SOL".replace(".", ",") if (ex := (ct or {}).get("explore") or {}).get("pnl_sol") is not None else ""))
    sq = cache.get(("safety_quality",), 60, lambda: safety_quality(conn))
    blocked = scalar(conn, "SELECT COUNT(*) FROM safety_tokens WHERE blocked=1", default=0) if has(conn, "safety_tokens", "blocked") else None
    best = None
    if sq and sq.get("backtest"):
        best = next((x for x in sq["backtest"]["scores"] if x["key"] == sq["backtest"]["best"]), None)
    add("safety", "/coins", f"{blocked} Coins gesperrt" if blocked is not None else "–",
        f"erkennt {round(100 * best['recall'])} % der Rugs bei ≤10 % Fehlalarm" if best and best.get("recall") is not None else "")
    if sniper:
        add("sniper", "/sniper", f"{sniper.get('launches_today', 0)} Launches heute",
            (f"Papier {sniper['pnl_sol']:+.3f} SOL".replace(".", ",") if sniper.get("pnl_sol") is not None else "") +
            (f" · {sniper.get('entered_today', 0)} gekauft" if sniper.get("entered_today") is not None else ""))
    mk = one(conn, "SELECT ts, score, label, sol_ret_24h FROM market_snapshots ORDER BY ts DESC LIMIT 1") \
        if has(conn, "market_snapshots", "label") else None
    add("market", "/market", (mk.get("label") or "–") if mk else "noch keine Analyse",
        (f"Marktwert {round(mk['score'])} von 100" if mk and mk.get("score") is not None else "") +
        (f" · SOL {mk['sol_ret_24h'] * 100:+.1f} % in 24 h".replace(".", ",") if mk and mk.get("sol_ret_24h") is not None else ""))
    if has(conn, "intel_events", "ts"):
        ev = one(conn, "SELECT ts, title, severity FROM intel_events ORDER BY ts DESC LIMIT 1")
        n1h = scalar(conn, "SELECT COUNT(*) FROM intel_events WHERE ts>=?", (now - 3600,), 0)
        add("intel", "/zentrale", f"{n1h} Meldungen in 1 h", (ev.get("title") or "")[:90] if ev else "")
    else:
        add("intel", "/zentrale", "noch keine Meldungen")
    m = model or {}
    add("research", "/model", f"Modell v{m['id']}" if m.get("id") else "noch kein Modell",
        f"AUC 1 h {m['auc_1h']:.2f}".replace(".", ",") if m.get("auc_1h") is not None else "")
    problems = []
    for h in health.values():
        if h["state"] == "steht":
            problems.append(f"{h['label']} steht" + (f" (letzte Daten {_ago_de(now - h['last_ts'])})" if h.get("last_ts") else ""))
    bk = cache.get(("backup",), 60, backup_status)
    if bk:
        problems += [p for p in (bk.get("problems") or {}).values() if p and p not in problems]
    order = {"steht": 0, "langsam": 1, "neu": 2, "ok": 3}
    return {"rows": out, "problems": problems[:8],
            "worst": min((r["state"] for r in out), key=lambda x: order.get(x, 9), default="ok")}


def _ago_de(s):
    s = int(s)
    return f"vor {s} s" if s < 90 else f"vor {s // 60} min" if s < 5400 else f"vor {s // 3600} h" if s < 172800 else f"vor {s // 86400} Tagen"


def _table_counts(conn):
    """Rows per table. COUNT(*) scans whole tables, so callers cache this for minutes."""
    return {t: scalar(conn, f"SELECT COUNT(*) FROM '{t}'", default=0) for t in sorted(tables(conn)) if not t.startswith("sqlite_")}


def system(conn):
    from .db import db_path
    path = db_path()
    counts = cache.get(("table_counts", path), 300, lambda: _table_counts(conn))
    last = {}
    for t, col in (("trades", "ts"), ("token_prices", "ts"), ("signal_metrics", "ts"), ("signals", "ts"),
                   ("model_predictions", "ts"), ("model_versions", "ts"), ("ct_events", "ts"), ("ct_equity", "ts"),
                   ("ct_swaps", "seen_at"), ("market_snapshots", "ts")):
        last[t] = last_ts(conn, t, col)
    services = []
    pids = os.path.join(ROOT_DIR, "data", "pids")
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
            "tier_source": tier_source(conn), "now": now_ts(), "sol_usd": _f(sol_usd(conn), 2),
            "health": cache.get(("health", path), 10, lambda: services_health(conn)),
            "safety_quality": cache.get(("safety_quality",), 60, lambda: safety_quality(conn)),
            "backup": cache.get(("backup",), 60, backup_status),
            "api_usage": cache.get(("api_usage",), 10, api_usage),
            "rpc_pool": cache.get(("rpc_pool",), 10, lambda: rpc_pool(usage=cache.get(("api_usage",), 10, api_usage)))}


# ====================================================================== health of all services
# service -> (German label, what it does, SQL for its newest activity ts, expected max gap in s)
SERVICE_INFO = {
    "signals": ("Signale", "Trades sammeln, Smart-Money-Signale", "SELECT MAX(ts) FROM trades", 15 * 60),
    "copytrade": ("Copy-Trading", "Wallets pollen, Papier- und Echtkäufe", "SELECT MAX(last_polled) FROM ct_wallets WHERE active=1", 15 * 60),
    "safety": ("Rug-Schutz", "Coins auf Rug, Honeypot, Insider prüfen", "SELECT MAX(ts) FROM safety_tokens", 30 * 60),
    "sniper": ("Launch-Sniper", "neue Launches prüfen, Papierkäufe", None, SNIPER_HB_STALE_S),
    "market": ("Markt", "Marktphase, Sektoren, Narrative", "SELECT MAX(ts) FROM market_snapshots", 30 * 60),
    "intel": ("Zentrale", "Merkmale, Ticker, Quellen-Auswertung", "SELECT MAX(ts) FROM intel_events", 30 * 60),
    "research": ("Modell & Forschung", "Wallet-Bewertung, Retraining", "SELECT MAX(ts) FROM model_versions", 30 * 3600),
    "web": ("Dashboard", "diese Oberfläche", None, None),
}


def _service_names():
    try:
        import mayham  # type: ignore  (root launcher; only defs at import time)
        return list(mayham.SERVICES)
    except Exception:
        return list(SERVICE_INFO)


def _log_mtime(name):
    try:
        return int(os.path.getmtime(os.path.join(os.getenv("MAYHAM_LOG_DIR") or os.path.join(ROOT_DIR, "logs"), f"{name}.log")))
    except OSError:
        return None


def _json_file(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def backup_status(now=None):
    """Newest backup archive (deploy/ops.py: BACKUP_DIR, mayhambot-*.tar.gz) and the watchdog's view of it.
    Only file names, sizes and times; the archive is never opened."""
    import glob
    now = now or time.time()
    d = os.path.expanduser(os.getenv("BACKUP_DIR") or "~/mayhambot-backups")
    files = sorted(glob.glob(os.path.join(d, "mayhambot-*.tar.gz")))
    last = files[-1] if files else None
    ts = int(os.path.getmtime(last)) if last else None
    wd = _json_file(os.path.join(ROOT_DIR, "data", "watchdog_state.json")) or {}
    au = _json_file(os.path.join(ROOT_DIR, "data", "autoupdate_state.json")) or {}
    problems = {k: v.get("msg") for k, v in (wd.get("problems") or {}).items() if isinstance(v, dict)}
    if int(au.get("red_streak") or 0) >= 2:
        problems["autoupdate_red"] = (f"Auto-Update hängt: Tests seit {int(au['red_streak'])} Runden rot, "
                                      f"der Mac bleibt auf {(au.get('deployed') or '?')[:7]}")
    blocked = _redact(str(au["blocked"]))[:400] if au.get("blocked") else None
    if blocked:  # a commit that exists only on the Mac stops every update until someone resolves it
        problems["autoupdate_blocked"] = f"Auto-Update blockiert: {blocked}"
    return {"last_name": os.path.basename(last) if last else None, "last_ts": ts,
            "last_size": os.path.getsize(last) if last else None, "count": len(files),
            "age_s": int(now - ts) if ts else None, "ok": bool(ts and now - ts <= 3 * 3600),
            "watchdog_ts": int(wd["last_run"]) if wd.get("last_run") else None, "problems": problems,
            "autoupdate": {"deployed": (au.get("deployed") or "")[:7] or None, "last_deploy": au.get("last_deploy"),
                           "bad": (au.get("bad") or "")[:7] or None,
                           "disabled": os.path.exists(os.path.join(ROOT_DIR, "data", "NO_AUTO_UPDATE")),
                           # rounds in a row whose test gate failed; from the 3rd the failing tests are named
                           "red_streak": int(au.get("red_streak") or 0), "blocked": blocked,
                           "red_tests": [_redact(x)[:200] for x in (au.get("red_tests") or [])
                                         if isinstance(x, str)][:12] if isinstance(au.get("red_tests"), list)
                           else ([_redact(au["red_tests"])[:600]] if au.get("red_tests") else [])}}


def services_health(conn, now=None):
    """One row per app service: process running, newest data it wrote, and whether that is overdue.
    state: ok | langsam (activity older than expected) | steht (process gone or no activity for 3x the gap) | neu (no data yet)."""
    now = int(now or time.time())
    out = []
    names = _service_names()
    for name in names + [k for k in SERVICE_INFO if k not in names and k != "web"]:
        label, what, sql, gap = SERVICE_INFO.get(name, (name, "", None, None))
        running = _pid_running(name)
        last, extra = None, {}
        if name == "sniper":
            st = sniper_stream(conn, now) if has(conn, "sniper_state") else None
            if st:
                last = st.get("heartbeat_ts")
                extra = {"connected": st.get("connected"), "launch_source": st.get("active_launch_source"),
                         "launches_per_min": st.get("launches_per_min"), "trade_source": st.get("trade_source"),
                         "rpc_ws_connected": st.get("rpc_ws_connected")}
        elif name == "signals":
            last = last_trade_ts(conn)
        elif sql:
            tbl = re.search(r"FROM (\w+)", sql).group(1)
            if has(conn, tbl):
                try:
                    last = scalar(conn, sql)
                except Exception:
                    last = None
        elif name == "web":
            last = now
        logm = _log_mtime(name)
        age = now - int(last) if last else None
        if running is False:
            state = "steht"
        elif not last:
            state = "neu"
        elif gap and age > 3 * gap:
            state = "steht"
        elif gap and age > gap:
            state = "langsam"
        else:
            state = "ok"
        if name == "sniper" and state == "ok" and extra.get("connected") is False:
            state = "langsam"
        out.append({"name": name, "label": label, "what": what, "running": running, "last_ts": int(last) if last else None,
                    "age_s": age, "expected_s": gap, "log_ts": logm, "state": state, **extra})
    return out


# ====================================================================== API budgets (core.http usage files)
_USAGE_HIST = {}            # (service, group) -> deque[(ts, calls)] sampled whenever the dashboard reads usage
USAGE_WINDOW_S = 600
_WINDOW_KEYS = ("calls", "live_calls", "bulk_calls", "live_waited_s", "bulk_waited_s", "429", "latency_ms_sum")
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
            snap = {k: v.get(k) or 0 for k in _WINDOW_KEYS}
            h = _USAGE_HIST.setdefault((svc, g), deque(maxlen=200))
            if not h or h[-1][0] != ts:
                if h and calls < h[-1][1].get("calls", 0):  # service restarted: counters began again
                    h.clear()
                h.append((ts or now, snap))
            while len(h) > 1 and h[1][0] <= (ts or now) - USAGE_WINDOW_S:
                h.popleft()
            base_ts, base = h[0]
            win = (ts or now) - base_ts
            b = v.get("budget_per_min")
            last = v.get("last_min")
            row = {k: x for k, x in v.items() if isinstance(x, (int, float)) and not isinstance(x, bool)}
            row.update(service=svc, group=g, pid=d.get("pid"), ts=ts, stale=stale, calls=calls,
                       calls_window=calls - base.get("calls", 0) if win > 0 else None, window_s=int(win) if win > 0 else 0,
                       last_min=last, budget_per_min=b, waited_s=_f(v.get("waited_s"), 1),
                       load=_f(last / b, 3) if b and last is not None and not stale else None)
            # public-RPC lanes (core.http live/bulk): totals since start and the same 10-min window
            if any(k in v for k in ("live_calls", "bulk_calls")):
                for k in ("live_calls", "bulk_calls", "live_waited_s", "bulk_waited_s"):
                    row[f"{k}_window"] = _f(snap[k] - base.get(k, 0), 1) if win > 0 else None
            row["429"] = v.get("429") or 0
            row["429_window"] = snap["429"] - base.get("429", 0) if win > 0 else None
            if v.get("latency_ms_sum") and win > 0:   # pool endpoints: mean answer time over the window
                dc, dl = calls - base.get("calls", 0), snap["latency_ms_sum"] - base.get("latency_ms_sum", 0)
                row["latency_ms_window"] = _f(dl / dc, 1) if dc > 0 and dl > 0 else None
            out.append(row)
    # Helius credits: shared monthly ledger (core.http.credits), one row per service with a share
    total = None
    try:
        cred = chttp.credits() if hasattr(chttp, "credits") else {}
    except Exception:
        cred = {}
    group = getattr(chttp, "CREDIT_GROUP", "helius")
    for svc, c in sorted((cred or {}).items()):
        if not isinstance(c, dict):
            continue
        row = next((r for r in out if r["service"] == svc and r["group"] == group), None)
        if row is None:
            row = {"service": svc, "group": group, "stale": True, "ts": None, "calls": None, "calls_window": None,
                   "window_s": 0, "last_min": None, "budget_per_min": None, "waited_s": None, "load": None, "429": 0}
            out.append(row)
        for k in ("credits_today", "credits_month", "credits_cap", "credits_day_budget", "share_pct"):
            row[k] = _f(c.get(k), 1)
        cap, day = c.get("credits_cap"), c.get("credits_day_budget")
        # day budget 0 = month share used up: "voll" (the service runs on the public RPC until next month)
        row["load_day"] = _f((c.get("credits_today") or 0) / day, 3) if day else (None if day is None else 1.0)
        row["day_full"] = day is not None and (not day or (c.get("credits_today") or 0) >= day)
        if row["day_full"]:
            row.update(_credit_borrow(chttp, cred, svc, c))
        row["load_month"] = _f((c.get("credits_month") or 0) / cap, 3) if cap else None
        if c.get("credits_month_limit"):
            total = {"used": _f(c.get("credits_month_all"), 0), "limit": c["credits_month_limit"],
                     "load": _f((c.get("credits_month_all") or 0) / c["credits_month_limit"], 4)}
    for r in out:
        if r["group"] == group:
            r["refused"] = r.get("refused") or 0
    if total is not None and hasattr(chttp, "credits_status"):
        try:
            cs = chttp.credits_status() or {}
            total.update(key_fp=cs.get("key_fp") or None, key_changed_at=cs.get("key_changed_at"),
                         warn=cs.get("warn"), history=[{k: h.get(k) for k in ("month", "key_fp", "used", "until", "reason")}
                                                      for h in (cs.get("history") or [])][::-1])
            if cs.get("used") is not None and cs.get("limit"):
                total.update(used=_f(cs["used"], 0), limit=cs["limit"], load=_f(cs.get("warn_level"), 4))
        except Exception:
            pass
    return {"available": True, "rows": out, "window_s": USAGE_WINDOW_S, "credits": total}


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


# ====================================================================== market analysis (market/, read-only)
def market_summary(conn, now=None):
    """Overview tile and terminal line: phase, top sector and narrative (market.summary), or None."""
    if not has(conn, "market_snapshots"):
        return None
    try:
        import market  # type: ignore
        return market.summary(conn, now)
    except Exception:
        return None
