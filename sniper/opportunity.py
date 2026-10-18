"""Every decided launch as an opportunity for the shared decision maker (plan "Richtung neu", 18.10.): the features
the sniper had at its decision (the stage-1 snapshot, so nothing from later leaks in), whether the stream saw the
launch at all, what the sniper did, and once measured the net result of buying it at the decision and holding
60 minutes after costs (rug = -100 %). The table schema comes from the decision maker; this module only builds
the rows, from sniper_launches, so it needs no live state."""
import json
import time

from . import research

FEATURES = ["mcap_decide_sol", "start_mcap_sol", "n_trades", "n_buyers", "n_sellers", "buy_sol", "sell_sol",
            "net_sol", "top10_pct", "curve_progress", "bundle_wallets", "bundle_pct", "unseen_pct", "block0_wallets",
            "insider_flag", "bundle_flag", "dev_buy_pct", "dev_sold_pct", "dev_launches", "dev_launches_24h",
            "known_bad", "fast_buyers", "n_smart", "n_good", "momentum", "risk", "score", "fee_pct"]
HORIZON_MIN = 60


def net_result(row, cfg):
    """Net return of a paper buy at the decision price held HORIZON_MIN minutes, after the paper book's costs.
    None while it is not measured or the base price is broken."""
    r = row.get("ret_60m")
    if row.get("rugged"):
        return -1.0
    if r is None:
        return None
    side, tx = research.costs(cfg)
    if row.get("fee_pct"):
        side += (row["fee_pct"] - cfg.curve_fee_pct) / 100
    return max(-1.0, (1 + min(r, research.MAX_TRADE_NET)) * (1 - side) ** 2 - 1 - 2 * tx)


def from_row(row, cfg):
    feats = {k: row.get(k) for k in FEATURES}
    try:
        s1 = json.loads(row.get("stage1_json") or "null") or {}
    except ValueError:
        s1 = {}
    feats.update({k: v for k, v in s1.items() if k in feats})   # the values the decision saw, not later ones
    dt = (row.get("decided_ts") or 0) - (row.get("created_ts") or 0)
    return {"source": "sniper", "key": row["mint"], "ts": row.get("decided_ts"), "pool": row.get("pool") or "pump",
            "seen_trades": bool(feats.get("n_trades")), "decided_after_sec": dt if dt >= 0 else None,
            "action": row.get("decision"), "reason": row.get("skip_reason"), "features": feats,
            "outcome_net": net_result(row, cfg), "outcome_horizon_min": HORIZON_MIN,
            "migrated": bool(row.get("migrated")), "max_ret_60m": row.get("max_ret_60m")}


def recent(conn, cfg, since=None, limit=50000):
    """Opportunities decided since `since` (all GeckoTerminal pools left out: they never carry trades)."""
    since = since if since is not None else time.time() - 86400
    cur = conn.execute("SELECT * FROM sniper_launches WHERE source = 'pumpportal' AND decided_ts >= ? "
                       "ORDER BY decided_ts LIMIT ?", (int(since), limit))
    cols = [c[0] for c in cur.description]
    return [from_row(dict(zip(cols, t)), cfg) for t in cur.fetchall()]
