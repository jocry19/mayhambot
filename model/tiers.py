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


def assign_tier(stats, score, rules=None):
    rules = rules or msettings.rules
    for tier in TIERS:
        r = rules[tier]
        if isinstance(r, dict):
            r = type("R", (), r)
        if (score >= r.min_score and stats["n_tokens"] >= r.min_tokens and stats["win_rate"] >= r.min_win_rate
                and stats["rug_rate"] <= r.max_rug_rate and stats["total_pnl_usd"] > r.min_pnl_usd):
            return tier
    return None


def tier_all(stats_by_wallet, weights=None, rules=None):
    """wallet -> (tier, score, stats). Includes untiered wallets with tier None."""
    out = {}
    for w, s in stats_by_wallet.items():
        sc = wallet_score(s, weights)
        out[w] = (assign_tier(s, sc, rules), sc, s)
    return out


def _row(r):
    d = dict(r)
    comps = json.loads(d.pop("components_json") or "{}")
    return {"address": d["address"], "tier": d["tier"], "score": d["score"], "version": d["version"],
            "components": comps,
            "stats": {k: d[k] for k in ("n_tokens", "win_rate", "early_rate", "roi", "realized_pnl_usd",
                                        "total_pnl_usd", "median_entry_mcap", "median_min_to_2x",
                                        "influence_followers", "influence_usd", "inflow_lift_usd",
                                        "consistency", "rug_rate")}}


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


def tier_map(conn):
    """wallet -> (tier, score, influence_usd) for tiered wallets (used by the live predictor)."""
    return {r[0]: (r[1], r[2] or 0.0, r[3] or 0.0) for r in conn.execute(
        "SELECT address, tier, score, influence_usd FROM model_wallet_stats WHERE tier IS NOT NULL")}
