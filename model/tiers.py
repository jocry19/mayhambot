"""Wallet score and tier rules (pure functions) + DB accessors for the current tiers."""
import json
import math

from model.config import PRIOR_WALLET_WEIGHTS, TIERS, msettings
from research.wallet_stats import COMPONENTS


def wallet_score(stats, weights=None):
    w = weights or PRIOR_WALLET_WEIGHTS
    tot = sum(w.get(c, 0) for c in COMPONENTS) or 1.0
    raw = sum(w.get(c, 0) * stats["components"].get(c, 0.5) for c in COMPONENTS) / tot
    return round(100 * raw * math.sqrt(1 - math.exp(-stats["n_tokens"] / 4)), 1)


def uncopyable(stats):
    """Wallets nobody can follow: launch/dev bots (buy within 3 s of the pool's creation, or first buyer every
    time), second-flippers (median hold under 15 s), wash wallets (most exits within 5 min at |ROI| < 3 %) and
    one-hit wallets (over 70 % of the profit from one coin, at a loss after costs without it).
    They can show great PnL or volume but are not a signal."""
    n = stats.get("n_tokens", 0)
    if n >= 3 and (stats.get("launch_share") or 0) >= 0.5:
        return "launch_bot"
    if n >= 3 and stats.get("median_entry_rank") == 0 and (stats.get("influence_samples") or 0) >= 3:
        return "launch_bot"
    if (stats.get("n_exits") or 0) >= 3 and stats.get("median_hold_min") is not None and stats["median_hold_min"] < 0.25:
        return "flipper"
    if (stats.get("n_exits") or 0) >= 4 and (stats.get("wash_share") or 0) >= 0.5:
        return "wash"  # mostly in-and-out within minutes at ~0 % (volume farming, wash trading)
    if n >= 3 and (stats.get("top_coin_share") or 0) > 0.7 and (stats.get("net_pnl_wo_best_usd") or 0) <= 0:
        return "one_hit"  # one lucky coin, losing without it: 61 % of Good wallets looked like this (10/2026)
    return None


def assign_tier(stats, score, rules=None, skip=()):
    if uncopyable(stats):
        return None
    rules = rules or msettings.rules
    for tier in TIERS:
        if tier in skip:
            continue
        r = rules[tier]
        if isinstance(r, dict):
            r = type("R", (), r)
        if (score >= r.min_score and stats["n_tokens"] >= r.min_tokens and stats["win_rate"] >= r.min_win_rate
                and stats["rug_rate"] <= r.max_rug_rate
                and stats.get("net_pnl_usd", stats["total_pnl_usd"]) > r.min_pnl_usd):  # profit after costs
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
            "median_hold_min": d.get("median_hold_min"),
            "net_pnl_usd": d.get("net_pnl_usd"), "net_roi": d.get("net_roi")}


def _conn(conn):
    if conn is not None:
        from research.store import ensure_schema
        return ensure_schema(conn)
    from research.store import connect
    return connect()


def copy_edge(conn, address):
    """What copying this wallet earned US (copytrade.learn.wallet_edge: our delay, exits and costs), next to the
    model's view of what the wallet earned itself. None when copytrade has no replays for it."""
    try:
        from copytrade import learn as ct_learn
        e = ct_learn.wallet_edge(conn, address)
    except Exception:  # noqa: BLE001  (copytrade tables missing or package not installed)
        return None
    return e if e and e.get("n") else None


def tier_of(address, conn=None, with_edge=False):
    conn = _conn(conn)
    r = conn.execute("SELECT * FROM model_wallet_stats WHERE address=?", (address,)).fetchone()
    if not r:
        return None
    out = _row(r)
    if with_edge:
        out["copy_edge"] = copy_edge(conn, address)
    return out


def wallet_tiers(conn=None, tier=None, limit=None, with_edge=False):
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
    out = [_row(r) for r in conn.execute(q, args)]
    if with_edge:
        for d in out:
            d["copy_edge"] = copy_edge(conn, d["address"])
    return out


def tier_sync(conn=None):
    """Source of truth for every other package once a model version exists.
    -> None before the first successful retrain (callers fall back to their own heuristic), else
    {address: {"tier", "score", "cluster_id", "exit_capture", "version", "net_pnl_usd", "net_roi"}} for every wallet the active model scored
    (tier None = scored but not good enough). Wallets missing from the dict were not scored by the model."""
    conn = _conn(conn)
    if not conn.execute("SELECT 1 FROM model_versions WHERE active=1 LIMIT 1").fetchone():
        return None
    return {r[0]: {"tier": r[1], "score": r[2], "cluster_id": r[3], "exit_capture": r[4], "version": r[5],
                   "net_pnl_usd": r[6], "net_roi": r[7]}
            for r in conn.execute("""SELECT address, tier, score, cluster_id, exit_capture, version, net_pnl_usd, net_roi
                                     FROM model_wallet_stats""")}


def tier_map(conn):
    """wallet -> (tier, score, influence_usd, cluster_id, exit_capture) for tiered wallets (live predictor)."""
    return {r[0]: (r[1], r[2] or 0.0, r[3] or 0.0, r[4], r[5]) for r in conn.execute(
        """SELECT address, tier, score, influence_usd, cluster_id, exit_capture FROM model_wallet_stats
           WHERE tier IS NOT NULL""")}


TIER_RANK = {"ultra": 3, "smart": 2, "good": 1}
WALLET_FEATURE_NAMES = (["w_known", "w_n_ultra", "w_n_smart", "w_n_good", "w_n_flagged", "w_n_one_hit",
                         "w_score_max", "w_score_mean", "w_tier_rank_max", "w_n_tokens_top", "w_evidence_top",
                         "w_win_rate_top", "w_net_roi_top", "w_net_pnl_sum_usd", "w_excess_roi_top",
                         "w_top_coin_share_top", "w_rug_rate_top", "w_n_clusters"]
                        + [f"w_c_{c}_top" for c in COMPONENTS])


def wallet_rows(addresses, conn=None):
    """address -> flat dict of what the active model knows about the wallet (every scored wallet, tiered or not):
    tier, tier_rank (3 ultra .. 0 none), score, components, n_tokens, evidence (0..1, how much the score can be
    trusted: 1 - exp(-n/4)), net results, flag (launch_bot / flipper / wash / one_hit or None)."""
    conn = _conn(conn)
    addrs = list(dict.fromkeys(a for a in addresses if a))
    out = {}
    for i in range(0, len(addrs), 500):
        part = addrs[i:i + 500]
        q = f"SELECT * FROM model_wallet_stats WHERE address IN ({','.join('?' * len(part))})"
        for r in conn.execute(q, part):
            d = dict(r)
            n = d.get("n_tokens") or 0
            out[d["address"]] = dict(
                tier=d["tier"], tier_rank=TIER_RANK.get(d["tier"], 0), score=d["score"] or 0.0, n_tokens=n,
                evidence=1 - math.exp(-n / 4), win_rate=d["win_rate"], rug_rate=d["rug_rate"],
                net_roi=d.get("net_roi"), net_pnl_usd=d.get("net_pnl_usd"), cluster_id=d.get("cluster_id"),
                flag=d.get("flag"), top_coin_share=d.get("top_coin_share"),
                median_excess_roi=d.get("median_excess_roi"), version=d["version"],
                components=json.loads(d["components_json"] or "{}"))
    return out


def wallet_features(addresses, conn=None, rows=None):
    """Features of the wallets behind one opportunity (the buyers of a coin, or the copied wallet), for the shared
    opportunity table. Tiers are inputs here, not gates: untiered and flagged wallets count with their score.
    Values are plain floats; unknown wallets give w_known 0 and None for the 'top' fields."""
    rows = wallet_rows(addresses, conn) if rows is None else rows
    known = [rows[a] for a in dict.fromkeys(addresses) if a in rows]
    f = dict.fromkeys(WALLET_FEATURE_NAMES)
    f.update(w_known=float(len(known)), w_n_ultra=0.0, w_n_smart=0.0, w_n_good=0.0, w_n_flagged=0.0,
             w_n_one_hit=0.0, w_net_pnl_sum_usd=0.0, w_n_clusters=0.0)
    if not known:
        return f
    for r in known:
        if r["tier"] in TIER_RANK:
            f[f"w_n_{r['tier']}"] += 1
        f["w_n_flagged"] += bool(r["flag"])
        f["w_n_one_hit"] += r["flag"] == "one_hit"
        f["w_net_pnl_sum_usd"] += r["net_pnl_usd"] or 0.0
    f["w_n_clusters"] = float(len({r["cluster_id"] for r in known if r["cluster_id"]}))
    top = max(known, key=lambda r: r["score"])
    f.update(w_score_max=top["score"], w_score_mean=sum(r["score"] for r in known) / len(known),
             w_tier_rank_max=float(max(r["tier_rank"] for r in known)), w_n_tokens_top=float(top["n_tokens"]),
             w_evidence_top=top["evidence"], w_win_rate_top=top["win_rate"], w_net_roi_top=top["net_roi"],
             w_excess_roi_top=top["median_excess_roi"], w_top_coin_share_top=top["top_coin_share"],
             w_rug_rate_top=top["rug_rate"])
    for c in COMPONENTS:
        f[f"w_c_{c}_top"] = top["components"].get(c)
    return f
