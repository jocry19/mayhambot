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
import sqlite3
import time

from signals.backtest import DEAD_RET, confirmed, price_near

EVERY_S = 3600
ENTRY_DELAY_S = 60
DEAD_LIQ_USD = 1000.0
SKIP = ("no_smart",)
KEEP_DAYS = 14



def record(conn, mint, reason, m, price, now, kind="v1"):
    if reason in SKIP or not price:
        return False
    if conn.execute("SELECT 1 FROM signal_shadow WHERE mint=? AND reason=? AND kind=? AND ts>?",
                    (mint, reason, kind, now - EVERY_S)).fetchone():
        return False
    sp = m.get("safety") or {}
    conn.execute("INSERT INTO signal_shadow(ts, mint, kind, reason, entities, net_usd, score, min_net, min_score, "
                 "safety_risk, liquidity_usd, price_usd, model_exp_net) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (now, mint, kind, reason, m.get("entities", m.get("smart_buyers")), m.get("net_usd"), m.get("score"),
                  m.get("min_net"), m.get("min_score"), sp.get("risk"), m.get("liquidity_usd"), price,
                  m.get("model_exp_net")))
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
    """dict(ret_1h, max_ret_1h, max_ret_4h, outcome_flag) as far as due; {} when nothing is due yet."""
    if now < t + 3900:
        return {}
    pts = _points(conn, mint, t, t + 4 * 3600)
    pe = max(p0, price_near(pts, t + ENTRY_DELAY_S, 120) or p0)
    after = [(x, p) for x, p in pts if x >= t + ENTRY_DELAY_S]
    h1 = [p for x, p in after if x <= t + 3600]
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
    if now >= t + 4 * 3600 and after:
        out["max_ret_4h"] = max(p for _, p in after) / pe - 1
    out["outcome_flag"] = "dead" if out.get("ret_1h") == DEAD_RET else "ok" if "max_ret_1h" in out else "gap"
    return out


def update(conn, now=None):
    now = now or int(time.time())
    n = 0
    for s in conn.execute("""SELECT g.id, g.ts, g.mint, g.price_usd, t.liquidity_usd, t.last_updated FROM signal_shadow g
                             LEFT JOIN tokens t ON t.mint=g.mint
                             WHERE g.ts > ? AND g.ts < ? AND (g.max_ret_4h IS NULL)""",
                          (now - 2 * 86400, now - 3900)).fetchall():
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
                median_ret_1h=_med(r["ret_1h"] for r in m))


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
