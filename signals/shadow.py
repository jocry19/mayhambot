"""Shadow book of the signal funnel: coins that had smart buyers but were not called, with the reason, and what
their price did afterwards. This is how a threshold gets loosened on data instead of a guess: if coins rejected for
'few_entities' or 'low_net' reach +50 % as often as the calls do, that gate costs signals; if they die more often,
it earns its keep.

- record(): every cycle, the first rejection of a coin per reason and hour (no_smart is not kept: nothing to learn).
- update(): fills max_ret_1h / max_ret_4h / ret_1h with the same honest rules as the calls (entry ENTRY_DELAY_S after
  the decision at the worse of the two prices, single bad prints filtered, a coin that died counts DEAD_RET).
- summary(): per reason n, hit rates, median high, dead share; in the hourly report as shadow_24h / shadow_7d.
- retro (CLI): the same table rebuilt read-only from signal_metrics (last ~3 days) for a DB that has no shadow rows
  yet: `python -m signals.shadow --retro --db data/mayhambot.db` (safety is not stored there, so coins that passed
  entities and net but not the score are one group "score_or_safety").
"""
import argparse
import json
import logging
import sqlite3
import time

from signals.backtest import DEAD_RET, confirmed, price_near
from signals.funnel import COST, trade_cost

log = logging.getLogger("mayhambot.signals.shadow")
EVERY_S = 3600
ENTRY_DELAY_S = 60
DEAD_LIQ_USD = 1000.0
SKIP = ()  # no_smart kept too: the big runners had at most one smart buyer (winner study 17.10.)
KEEP_DAYS = 14



def _market(conn, mint, price, now):
    """tokens row (DexScreener) + the own 15 min move before now."""
    t = conn.execute("SELECT dex, mcap_usd, pair_created_at, price_change_m5, price_change_h1, txns_m5, txns_h1, "
                     "volume_h1, symbol FROM tokens WHERE mint=?", (mint,)).fetchone()
    before = price_near(_points(conn, mint, now - 1500, now - 300), now - 900, 300)
    created = t[2] if t else None
    if created and created > now * 10:  # DexScreener gives milliseconds
        created /= 1000
    return t, before, created


NOT_NEAR = ("no_smart", "few_entities", "cooldown", "stale_price")


def near_call(m, reason, action="passed"):
    """2+ smart owners bought, only a later gate said no (18.10. shadow, 24 h: under $500 few_holders 7 of 24 and
    safety_drop 5 of 21 ran +50 % in 1 h). The brain's learning account may buy these small, with its own rug gate."""
    if action == "alerted":
        return True
    return (m.get("entities", m.get("smart_buyers")) or 0) >= 2 and reason not in NOT_NEAR


def _band(m):
    try:
        from signals import engine
        return bool(engine.in_band(dict(m, smart_buyers=m.get("smart_buyers") or 0, net_usd=m.get("net_usd") or 0)))
    except Exception:  # noqa: BLE001
        return None


def report_opportunity(conn, source, ref, mint, now, price, m, reason=None, action="passed", kind="v1", mkt=None):
    """One row in the brain's shared opportunities table (brain.opportunities.report): raw values under the shared
    names, everything else as signals.<name>; the funnel's verdict is a feature (source_reason), not a gate."""
    try:
        from brain.opportunities import report
    except Exception:  # noqa: BLE001 - brain/ absent
        return False
    t, before, created = mkt or _market(conn, mint, price, now)
    sp = m.get("safety") or {}
    try:
        from signals import plan
        staged = plan._staged(conn, mint)
    except Exception:  # noqa: BLE001
        staged = None
    f = {"wallets_n": m.get("entities", m.get("smart_buyers")), "inflow_usd": m.get("net_usd"),
         "inflow_sol": m.get("net_sol"), "mc_usd": t[1] if t else None, "liq_usd": m.get("liquidity_usd"),
         "dex": t[0] if t else None, "pc_5m": t[3] if t else None, "pc_1h": t[4] if t else None,
         "risk": sp.get("risk"), "staged": staged, "age_s": now - created if created else None,
         "signals.kind": kind, "signals.reason": reason or action, "signals.entities": m.get("entities"),
         "signals.move_15m": price / before - 1 if before and price else None, "signals.score": m.get("score"),
         "signals.score_raw": m.get("score_raw"), "signals.min_score": m.get("min_score"),
         "signals.min_net": m.get("min_net"), "signals.n_ultra": m.get("n_ultra"), "signals.n_smart": m.get("n_smart"),
         "signals.sellers": m.get("sellers", m.get("n_sellers")), "signals.avg_quality": m.get("avg_quality"),
         "signals.model_exp_net": m.get("model_exp_net"), "signals.prediction": m.get("prediction"),
         "signals.safety_factor": sp.get("factor"), "signals.wash": bool(sp.get("red_flags")),
         "signals.txns_m5": t[5] if t else None, "signals.txns_h1": t[6] if t else None,
         "signals.volume_h1": t[7] if t else None, "signals.holders": m.get("holders"),
         "signals.hold_share": m.get("share"), "signals.span_min": m.get("span_min"),
         "signals.receivers": m.get("n_receivers"), "signals.band": _band(m),
         "signals.near_call": near_call(m, reason, action),
         "signals.lag_last_s": now - m["smart_last_ts"] if m.get("smart_last_ts") else None}
    try:
        return report(conn, source, ref, mint, now, f, symbol=(t[8] if t else "") or "", price_usd=price,
                      mcap_usd=t[1] if t else None, liq_usd=m.get("liquidity_usd"), source_score=m.get("score"),
                      source_action=action, source_reason=reason)
    except Exception as e:  # noqa: BLE001 - reporting must never stop signals
        log.debug("opportunity report %s failed: %s", mint[:6], e)
        return False


def record(conn, mint, reason, m, price, now, kind="v1"):
    if reason in SKIP or not price:
        return False
    if conn.execute("SELECT 1 FROM signal_shadow WHERE mint=? AND reason=? AND kind=? AND ts>?",
                    (mint, reason, kind, now - EVERY_S)).fetchone():
        return False
    sp = m.get("safety") or {}
    mkt = _market(conn, mint, price, now)
    t, before, created = mkt
    cur = conn.execute("INSERT INTO signal_shadow(ts, mint, kind, reason, entities, net_usd, score, min_net, min_score, "
                 "safety_risk, liquidity_usd, price_usd, model_exp_net, dex, mcap_usd, age_min, pc_m5, pc_h1, txns_m5, "
                 "txns_h1, volume_h1, ret_15m_before, cost) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (now, mint, kind, reason, m.get("entities", m.get("smart_buyers")), m.get("net_usd"), m.get("score"),
                  m.get("min_net"), m.get("min_score"), sp.get("risk"), m.get("liquidity_usd"), price,
                  m.get("model_exp_net"), t[0] if t else None, t[1] if t else None,
                  (now - created) / 60 if created else None, t[3] if t else None, t[4] if t else None,
                  t[5] if t else None, t[6] if t else None, t[7] if t else None,
                  price / before - 1 if before else None, trade_cost(t[0] if t else None, m.get("liquidity_usd"))))
    report_opportunity(conn, "signals_v2" if kind == "v2" else "signals", f"shadow:{cur.lastrowid}", mint, now, price, m,
                       reason, "passed", kind, mkt)
    return True


def _points(conn, mint, t0, t1):
    snap = conn.execute("SELECT ts, price_usd FROM token_prices WHERE mint=? AND ts>? AND ts<=? AND price_usd>0",
                        (mint, t0, t1)).fetchall()
    prints = conn.execute("SELECT ts, price_usd FROM trades WHERE mint=? AND ts>? AND ts<=? AND price_usd>0",
                          (mint, t0, t1)).fetchall()
    pts = sorted([(r[0], r[1], True) for r in snap] + [(r[0], r[1], False) for r in prints])
    clean = confirmed([p for _, p, _ in pts], [tr for _, _, tr in pts])
    return [(t, p) for (t, _, _), p in zip(pts, clean)]


def outcome(conn, mint, t, p0, now, liq=None, last_updated=None):
    """dict(ret_1h, max_ret_1h, ret_4h, max_ret_4h, ret_24h, max_ret_24h, outcome_flag) as far as due; {} when
    nothing is due yet."""
    if now < t + 3900:
        return {}
    pts = _points(conn, mint, t, t + 86400)
    pe = max(p0, price_near(pts, t + ENTRY_DELAY_S, 120) or p0)
    after = [(x, p) for x, p in pts if x >= t + ENTRY_DELAY_S]
    h1 = [p for x, p in after if x <= t + 3600]
    h4 = [p for x, p in after if x <= t + 4 * 3600]
    out = {}
    p1 = price_near(pts, t + 3600, 300)
    # died: pool drained, or DexScreener stopped returning a coin it still returned at the decision
    gone = (liq is not None and liq < DEAD_LIQ_USD) or (last_updated is not None and t - 900 <= last_updated < t + 1800)
    if p1 is not None:
        out["ret_1h"] = p1 / p0 - 1
    elif gone and not any(x > t + 3600 for x, _ in pts):
        out["ret_1h"] = DEAD_RET
    if h1:
        out["max_ret_1h"] = max(h1) / pe - 1
    elif out.get("ret_1h") == DEAD_RET:
        out["max_ret_1h"] = DEAD_RET
    for key, dt, tol in (("4h", 4 * 3600, 1800), ("24h", 86400, 3600)):
        if now < t + dt + tol:
            continue
        p = price_near(pts, t + dt, tol)
        if p is not None:
            out["ret_" + key] = p / p0 - 1
        elif gone and not any(x > t + dt for x, _ in pts):
            out["ret_" + key] = DEAD_RET
    if now >= t + 4 * 3600 and h4:
        out["max_ret_4h"] = max(h4) / pe - 1
    if now >= t + 86400 and after:
        out["max_ret_24h"] = max(p for _, p in after) / pe - 1
    out["outcome_flag"] = "dead" if out.get("ret_1h") == DEAD_RET else "ok" if "max_ret_1h" in out else "gap"
    return out


def update(conn, now=None):
    now = now or int(time.time())
    n = 0
    for s in conn.execute("""SELECT g.id, g.ts, g.mint, g.price_usd, t.liquidity_usd, t.last_updated FROM signal_shadow g
                             LEFT JOIN tokens t ON t.mint=g.mint
                             WHERE g.ts > ? AND g.ts < ? AND (g.max_ret_4h IS NULL
                                   OR (g.ret_24h IS NULL AND g.max_ret_24h IS NULL AND g.ts < ?))""",
                          (now - 2 * 86400, now - 3900, now - 86400 - 3600)).fetchall():
        o = outcome(conn, s[2], s[1], s[3], now, s[4], s[5])
        if o:
            conn.execute(f"UPDATE signal_shadow SET {', '.join(k + '=?' for k in o)} WHERE id=?", (*o.values(), s[0]))
            n += 1
    conn.execute("DELETE FROM signal_shadow WHERE ts < ?", (now - KEEP_DAYS * 86400,))
    conn.commit()
    return n


def _med(xs):
    xs = sorted(x for x in xs if x is not None)
    return round(xs[len(xs) // 2], 3) if xs else None


def _stats(rows):
    m = [r for r in rows if r["max_ret_1h"] is not None]
    if not m:
        return dict(n=len(rows), measured=0)
    mx = [r["max_ret_1h"] for r in m]
    m4 = [r["max_ret_4h"] for r in m if r["max_ret_4h"] is not None]
    return dict(n=len(rows), measured=len(m), hit_50=round(sum(x >= 0.5 for x in mx) / len(m), 3),
                hit_100=round(sum(x >= 1.0 for x in mx) / len(m), 3), median_max_1h=_med(mx),
                hit_50_4h=round(sum(x >= 0.5 for x in m4) / len(m4), 3) if m4 else None,
                dead=round(sum(1 for r in m if r["ret_1h"] == DEAD_RET) / len(m), 3),
                median_ret_1h=_med(r["ret_1h"] for r in m),
                **{f"median_net_{h}": _med(_net(r, h) for r in m if _net(r, h) is not None) for h in ("1h", "4h", "24h")})


def _net(r, h):
    """End-of-horizon return after this row's own round trip (lab.costs); None when not measured."""
    keys = r.keys()
    x = r[f"ret_{h}"] if f"ret_{h}" in keys else None
    if x is None:
        return None
    c = r["cost"] if "cost" in keys and r["cost"] is not None else COST
    return min(x, 3.0) - c


def groups(rows):
    """Per reason, plus the near misses a loosening would let through (one gate missed by a little)."""
    out = {}
    by = {}
    for r in rows:
        by.setdefault(r["reason"], []).append(r)
    for k, v in sorted(by.items()):
        out[k] = _stats(v)
    fe = by.get("few_entities", [])
    out["few_entities: 1 owner, net >= min"] = _stats([r for r in fe if (r["net_usd"] or 0) >= (r["min_net"] or 0)])
    ln = by.get("low_net", [])
    out["low_net: net >= half the min"] = _stats([r for r in ln if (r["net_usd"] or 0) >= 0.5 * (r["min_net"] or 0)])
    sd = [r for r in by.get("safety_drop", []) if r["safety_risk"] is not None]
    out["safety_drop: risk < 45"] = _stats([r for r in sd if r["safety_risk"] < 45])
    out["safety_drop: risk >= 45"] = _stats([r for r in sd if r["safety_risk"] >= 45])
    # momentum / venue hypotheses (winner study 17.10.: 3x coins dipped ~32 % in the 15 min before; only pumpswap ran 3x)
    col = lambda r, k: r[k] if k in r.keys() else None  # noqa: E731 - retro rows lack these
    for d in sorted({col(r, "dex") for r in rows if col(r, "dex")}):
        out[f"dex {d}"] = _stats([r for r in rows if col(r, "dex") == d])
    out["dip >= 20 % in 15 min before"] = _stats([r for r in rows if (col(r, "ret_15m_before") or 0) <= -0.2])
    out["run-up >= 20 % in 15 min before"] = _stats([r for r in rows if (col(r, "ret_15m_before") or 0) >= 0.2])
    out["txns 5 min >= 2x hour pace"] = _stats([r for r in rows if col(r, "txns_m5") and col(r, "txns_h1")
                                                 and col(r, "txns_m5") * 12 >= 2 * col(r, "txns_h1")])
    # confidence hypotheses (see funnel.CONFIDENCE_GROUPS): the same features among the rejected coins
    ent = lambda r: (r["entities"] or 0) if "entities" in r.keys() else 0  # noqa: E731 - retro rows have none
    out["any reason: entities>=3"] = _stats([r for r in rows if ent(r) >= 3])
    band = [r for r in rows if ent(r) >= 2 and 500 <= (r["net_usd"] or 0) <= 2000]
    out["any reason: net 500-2000, entities>=2"] = _stats(band)
    for k in sorted({r["reason"] for r in band}):  # why band candidates did not become calls
        out[f"band candidate, {k}"] = _stats([r for r in band if r["reason"] == k])
    # is the $500-2000 band special, or does the owner count carry it? the same split below and above the band
    for name, lo, hi in (("net <500", -1e18, 500), ("net >2000", 2000, 1e18)):
        sub = [r for r in rows if ent(r) >= 2 and lo <= (r["net_usd"] or 0) < hi]
        out[f"entities>=2, {name}"] = _stats(sub)
        for k in sorted({r["reason"] for r in sub}):
            out[f"entities>=2, {name}, {k}"] = _stats([r for r in sub if r["reason"] == k])
    return out


def summary(conn, hours=24, now=None, kind="v1"):
    now = now or int(time.time())
    try:
        rows = conn.execute("SELECT * FROM signal_shadow WHERE ts>=? AND ts<=? AND kind=?",
                            (now - int(hours * 3600), now - 3900, kind)).fetchall()
    except sqlite3.OperationalError:
        return {}
    return groups(rows)


def retro(conn, min_net, min_score, hours=72, now=None):
    """Shadow rows rebuilt from signal_metrics (no writes): first evaluation per coin and hour with a smart buyer."""
    now = now or int(time.time())
    seen = {}
    rows = []
    tok = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT mint, liquidity_usd, last_updated FROM tokens")}
    for r in conn.execute("SELECT ts, mint, smart_buyers, net_usd, score, price_usd FROM signal_metrics "
                          "WHERE ts>=? AND ts<=? AND smart_buyers>0 AND price_usd>0 ORDER BY ts",
                          (now - int(hours * 3600), now - 3900)):
        ts, mint, ent, net, score, p = r
        why = ("few_entities" if ent < 2 else "low_net" if (net or 0) < min_net else
               "score_or_safety" if (score or 0) < min_score else "passed_numbers")
        if ts - seen.get((mint, why), -10 ** 9) < EVERY_S:
            continue
        seen[(mint, why)] = ts
        liq, upd = tok.get(mint, (None, None))
        o = outcome(conn, mint, ts, p, now, liq, upd)
        rows.append(dict(reason=why, net_usd=net, min_net=min_net, safety_risk=None, ret_1h=o.get("ret_1h"),
                         max_ret_1h=o.get("max_ret_1h"), max_ret_4h=o.get("max_ret_4h")))
    out = groups(rows)
    for k in [k for k in out if k.startswith("safety_drop")]:
        del out[k]
    return out


def main():
    ap = argparse.ArgumentParser(description="what coins the signal funnel rejected did afterwards")
    ap.add_argument("--retro", action="store_true", help="rebuild from signal_metrics, read-only")
    ap.add_argument("--db", default=None)
    ap.add_argument("--hours", type=float, default=72)
    ap.add_argument("--min-net", type=float, default=100)
    ap.add_argument("--min-score", type=float, default=40)
    a = ap.parse_args()
    if a.db is None:
        from core.config import settings
        a.db = settings.db_path
    conn = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    res = retro(conn, a.min_net, a.min_score, a.hours) if a.retro else summary(conn, a.hours)
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
