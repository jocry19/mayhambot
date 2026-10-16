"""Wallet score and tier rules (pure functions) + DB accessors for the current tiers."""
import json
import math

from model.config import PRIOR_WALLET_WEIGHTS, TIERS, msettings
from research.wallet_stats import COMPONENTS


def wallet_score(stats, weights=None):
    w = weights or PRIOR_WALLET_WEIGHTS
    tot = sum(w.get(c, 0) for c in COMPONENTS) or 1.0
    raw = sum(w.get(c, 0) * stats["components"][c] for c in COMPONENTS) / tot
    return round(100 * raw * math.sqrt(1 - math.exp(-stats["n_tokens"] / 4)), 1)


def assign_tier(stats, score, rules=None, skip=()):
    rules = rules or msettings.rules
    for tier in TIERS:
        if tier in skip:
            continue
        r = rules[tier]
        if isinstance(r, dict):
            r = type("R", (), r)
        if (score >= r.min_score and stats["n_tokens"] >= r.min_tokens and stats["win_rate"] >= r.min_win_rate
                and stats["rug_rate"] <= r.max_rug_rate and stats["total_pnl_usd"] > r.min_pnl_usd):
            return tier
    return None


def tier_all(stats_by_wallet, weights=None, rules=None, caps=None):
    """wallet -> (tier, score, stats). Includes untiered wallets with tier None.
    Caps keep only the best N wallets in a tier; the others fall through to the next tier's rule."""
    caps = msettings.caps if caps is None else caps
    scored = sorted(((w, s, wallet_score(s, weights)) for w, s in stats_by_wallet.items()), key=lambda x: -x[2])
    used = {t: 0 for t in TIERS}
    out = {}
    for w, s, sc in scored:
        full = tuple(t for t in TIERS if caps.get(t) and used[t] >= caps[t])
        tier = assign_tier(s, sc, rules, skip=full)
        if tier:
            used[tier] += 1
        out[w] = (tier, sc, s)
    return out


def _row(r):
    d = dict(r)
    comps = json.loads(d.pop("components_json") or "{}")
    return {"address": d["address"], "tier": d["tier"], "score": d["score"], "version": d["version"],
            "components": comps,
            "stats": {k: d[k] for k in ("n_tokens", "win_rate", "early_rate", "roi", "realized_pnl_usd",
                                        "total_pnl_usd", "median_entry_mcap", "median_min_to_2x",
                                        "influence_followers", "influence_usd", "inflow_lift_usd",
                                        "consistency", "rug_rate")},
            "cluster_id": d.get("cluster_id"), "exit_capture": d.get("exit_capture"),
            "median_hold_min": d.get("median_hold_min")}


def _conn(conn):
    if conn is not None:
        from research.store import ensure_schema
        return ensure_schema(conn)
    from research.store import connect
    return connect()


def tier_of(address, conn=None):
    conn = _conn(conn)
    r = conn.execute("SELECT * FROM model_wallet_stats WHERE address=?", (address,)).fetchone()
    return _row(r) if r else None


def wallet_tiers(conn=None, tier=None, limit=None):
    conn = _conn(conn)
    q, args = "SELECT * FROM model_wallet_stats", []
    if tier:
        q += " WHERE tier=?"
        args.append(tier)
    else:
        q += " WHERE tier IS NOT NULL"
    q += " ORDER BY score DESC"
    if limit:
        q += f" LIMIT {int(limit)}"
    return [_row(r) for r in conn.execute(q, args)]


def tier_sync(conn=None):
    """Source of truth for every other package once a model version exists.
    -> None before the first successful retrain (callers fall back to their own heuristic), else
    {address: {"tier", "score", "cluster_id", "exit_capture", "version"}} for every wallet the active model scored
    (tier None = scored but not good enough). Wallets missing from the dict were not scored by the model."""
    conn = _conn(conn)
    if not conn.execute("SELECT 1 FROM model_versions WHERE active=1 LIMIT 1").fetchone():
        return None
    return {r[0]: {"tier": r[1], "score": r[2], "cluster_id": r[3], "exit_capture": r[4], "version": r[5]}
            for r in conn.execute("SELECT address, tier, score, cluster_id, exit_capture, version FROM model_wallet_stats")}


def tier_map(conn):
    """wallet -> (tier, score, influence_usd, cluster_id, exit_capture) for tiered wallets (live predictor)."""
    return {r[0]: (r[1], r[2] or 0.0, r[3] or 0.0, r[4], r[5]) for r in conn.execute(
        """SELECT address, tier, score, influence_usd, cluster_id, exit_capture FROM model_wallet_stats
           WHERE tier IS NOT NULL""")}
