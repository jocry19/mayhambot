"""Statistics of the sniper for dashboard and terminal. Pure SQL on the sniper_ tables, no network.

Returns honest numbers: outcomes are measured from the market cap at the decision (what we could have
bought), and skipped launches get the same 1/5/15/60-minute outcomes as entered ones, so the value of
each filter is visible: winners it cost us and rugs it saved us from."""
import json
import time

from sniper.engine import SKIP_LABELS
from sniper.research import MIN_BASE_MCAP

HIT = 1.0     # max_ret_60m >= +100 % (2x) counts as a winner
RUG = -0.8    # ret_60m <= -80 % counts as a rug / dead coin
EXIT_LABELS = {"tp1": "erster Gewinnschub", "trail": "Trailing-Stop", "stop": "Stop-Loss", "time": "Zeit-Stop",
               "max_hold": "maximale Haltedauer", "insider_sell": "Bundle-/Dev-Wallet verkauft", "manual": "manuell"}


def _median(xs):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def _group(rows):
    done = [r for r in rows if r["max_ret_60m"] is not None]
    out = {"n": len(rows), "n_measured": len(done)}
    for h in ("1m", "5m", "15m", "60m"):
        out[f"median_ret_{h}"] = _median([r[f"ret_{h}"] for r in rows])
    out["median_max_ret_60m"] = _median([r["max_ret_60m"] for r in done])
    out["hit_rate_2x"] = sum(1 for r in done if r["max_ret_60m"] >= HIT) / len(done) if done else None
    rug = [r for r in rows if r["ret_60m"] is not None]
    out["rug_rate"] = sum(1 for r in rug if r["ret_60m"] <= RUG) / len(rug) if rug else None
    return out


def _clean(r):
    """A pump launch decided below the curve's start price had a broken quote: its returns (+4,800,000 % on
    average) are no outcome. It still counts as a launch, just not as measured."""
    if r.get("pool") == "pump" and r.get("mcap_decide_sol") is not None and r["mcap_decide_sol"] < MIN_BASE_MCAP:
        for k in ("ret_1m", "ret_5m", "ret_15m", "ret_60m", "max_ret_60m", "rugged"):
            r[k] = None
    return r


def _has(conn):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE name='sniper_launches'").fetchone() is not None


def stats(conn, hours=24, now=None):
    now = int(now or time.time())
    since = now - int(hours * 3600) if hours else 0
    if not _has(conn):
        return {"available": False}
    cols = "mint, stage, decision, skip_reason, score, risk, momentum, bundle_flag, insider_flag, " \
           "ret_1m, ret_5m, ret_15m, ret_60m, max_ret_60m, rugged, calls_rpc, calls_api, source, pool, mcap_decide_sol"
    rows = [_clean(dict(r)) for r in conn.execute(f"SELECT {cols} FROM sniper_launches WHERE decided_ts >= ?",
                                                  (since,))]
    entered = [r for r in rows if r["decision"] == "enter"]
    stage2 = [r for r in rows if r["stage"] == 2]
    skipped2 = [r for r in stage2 if r["decision"] != "enter"]
    skipped1 = [r for r in rows if r["stage"] == 1 and r["decision"] == "skip"]
    reasons = {}
    for r in rows:
        if r["decision"] == "skip":
            reasons.setdefault(r["skip_reason"] or "?", []).append(r)
    filters = []
    for code, rs in sorted(reasons.items(), key=lambda kv: -len(kv[1])):
        g = _group(rs)
        done = [r for r in rs if r["max_ret_60m"] is not None]
        g.update(code=code, label=SKIP_LABELS.get(code, code),
                 missed_winners=sum(1 for r in done if r["max_ret_60m"] >= HIT),
                 avoided_rugs=sum(1 for r in rs if r["ret_60m"] is not None and r["ret_60m"] <= RUG))
        filters.append(g)
    deep = [r for r in stage2 if r["calls_rpc"] is not None]
    out = {
        "available": True, "hours": hours, "ts": now,
        "launches": len(rows), "stage2": len(stage2), "entered": len(entered),
        "bundle_flagged": sum(1 for r in stage2 if r["bundle_flag"]),
        "insider_flagged": sum(1 for r in stage2 if r["insider_flag"]),
        "groups": {"entered": _group(entered), "skipped_deep": _group(skipped2), "skipped_stage1": _group(skipped1),
                   "all": _group(rows)},
        "filters": filters,
        "calls_per_deep_launch": {"rpc": round(sum(r["calls_rpc"] or 0 for r in deep) / len(deep), 2) if deep else None,
                                  "pump_api": round(sum(r["calls_api"] or 0 for r in deep) / len(deep), 2) if deep else None},
        "score_buckets": score_buckets(stage2),
        "threshold_sweep": threshold_sweep(stage2),
        "paper": paper_stats(conn, since),
    }
    return out


def score_buckets(rows, width=10):
    out = []
    for lo in range(0, 100, width):
        rs = [r for r in rows if r["score"] is not None and lo <= r["score"] < lo + width]
        if rs:
            g = _group(rs)
            g["bucket"] = f"{lo}-{lo + width}"
            out.append(g)
    return out


def threshold_sweep(rows):
    """What other minimum scores would have bought (flagged launches excluded), measured on real outcomes."""
    clean = [r for r in rows if not r["bundle_flag"] and not r["insider_flag"] and r["max_ret_60m"] is not None]
    out = []
    for thr in range(20, 85, 5):
        rs = [r for r in clean if (r["score"] or 0) >= thr]
        g = _group(rs)
        g["min_score"] = thr
        out.append(g)
    return out


def paper_stats(conn, since=0):
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='sniper_positions'").fetchone() is None:
        return {}
    closed = [dict(r) for r in conn.execute(
        "SELECT * FROM sniper_positions WHERE status='closed' AND closed_ts >= ? ORDER BY closed_ts DESC", (since,))]
    opened = [dict(r) for r in conn.execute("SELECT * FROM sniper_positions WHERE status='open' ORDER BY opened_ts DESC")]
    bal = conn.execute("SELECT value FROM sniper_state WHERE key='paper_balance_sol'").fetchone()
    by_exit = {}
    for p in closed:
        for e in json.loads(p["events_json"] or "[]"):
            if e.get("kind") == "sell":
                by_exit.setdefault(e.get("reason"), []).append(e)
    wins = [p for p in closed if (p["pnl_sol"] or 0) > 0]
    return {
        "balance_sol": float(bal[0]) if bal else None,
        "n_closed": len(closed), "n_open": len(opened),
        "win_rate": len(wins) / len(closed) if closed else None,
        "pnl_sol": round(sum(p["pnl_sol"] or 0 for p in closed), 6),
        "avg_pnl_pct": sum(p["pnl_pct"] or 0 for p in closed) / len(closed) if closed else None,
        "best_pct": max((p["pnl_pct"] for p in closed), default=None),
        "worst_pct": min((p["pnl_pct"] for p in closed), default=None),
        "exits": [{"reason": k, "label": EXIT_LABELS.get(k, k), "n": len(v)} for k, v in by_exit.items()],
        "open": [{k: p[k] for k in ("id", "mint", "symbol", "opened_ts", "entry_mcap_sol", "size_sol", "remaining",
                                    "tokens", "last_price_sol", "entry_price_sol", "tp1_done")}
                 | {"unrealized_pct": (p["last_price_sol"] / p["entry_price_sol"] - 1) if p["entry_price_sol"] else None}
                 for p in opened],
        "recent": [{k: p[k] for k in ("id", "mint", "symbol", "opened_ts", "closed_ts", "exit_reason", "pnl_sol",
                                      "pnl_pct", "score")} for p in closed[:20]],
    }


def recent(conn, limit=50, decision=None, stage=None):
    """Latest launches for lists (dashboard table, terminal)."""
    if not _has(conn):
        return []
    q, args = "SELECT * FROM sniper_launches WHERE 1=1", []
    if decision:
        q += " AND decision=?"
        args.append(decision)
    if stage:
        q += " AND stage>=?"
        args.append(stage)
    q += " ORDER BY decided_ts DESC LIMIT ?"
    args.append(int(limit))
    out = []
    for r in conn.execute(q, args):
        d = dict(r)
        d.pop("features_json", None)
        d["reasons"] = json.loads(d.pop("reasons_json") or "[]")
        d["positives"] = json.loads(d.pop("positives_json") or "[]")
        d["skip_label"] = SKIP_LABELS.get(d["skip_reason"], d["skip_reason"])
        out.append(d)
    return out


def launch(conn, mint):
    """Everything about one launch: row with reasons and features, its wallets, curve and positions."""
    if not _has(conn):
        return None
    r = conn.execute("SELECT * FROM sniper_launches WHERE mint=?", (mint,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["reasons"] = json.loads(d.pop("reasons_json") or "[]")
    d["positives"] = json.loads(d.pop("positives_json") or "[]")
    d["features"] = json.loads(d.pop("features_json") or "{}")
    d["skip_label"] = SKIP_LABELS.get(d["skip_reason"], d["skip_reason"])
    d["wallets"] = [dict(w) for w in conn.execute(
        "SELECT * FROM sniper_launch_wallets WHERE mint=? ORDER BY role, tokens DESC", (mint,))]
    d["curve"] = [dict(c) for c in conn.execute("SELECT ts, mcap_sol, v_sol, n_trades FROM sniper_curve WHERE mint=? ORDER BY ts",
                                                (mint,))]
    d["positions"] = [dict(p) for p in conn.execute("SELECT * FROM sniper_positions WHERE mint=? ORDER BY id", (mint,))]
    return d


def wallet_history(conn, wallet):
    """In which launches a wallet appeared as bundle / insider / dev / early buyer, with the outcome."""
    if not _has(conn):
        return []
    return [dict(r) for r in conn.execute(
        """SELECT w.*, l.symbol, l.decided_ts, l.ret_60m, l.max_ret_60m, l.rugged FROM sniper_launch_wallets w
           LEFT JOIN sniper_launches l ON l.mint = w.mint WHERE w.wallet=? ORDER BY l.decided_ts DESC""", (wallet,))]
