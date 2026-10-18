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
            "known_bad", "fast_buyers", "n_smart", "n_good", "momentum", "risk", "score", "fee_pct",
            "quote_mint"]
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


def _quote_label(mint):
    """What the curve is quoted in: SOL, PUMP or another coin (pump.fun "depth" coins)."""
    from .rpcws import PUMP_TOKEN_MINT
    return "SOL" if not mint else ("PUMP" if mint == PUMP_TOKEN_MINT else "coin")


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


def shared_features(o, row):
    """The opportunity's features under the shared names of brain.opportunities (gelegenheiten_schema.md), the
    rest as sniper.<name>. Raw values, no thresholds."""
    f, sol_usd = o["features"], row.get("sol_usd") or 0.0
    vol = (f.get("buy_sol") or 0) + (f.get("sell_sol") or 0)
    out = {"mc_usd": (f.get("mcap_decide_sol") or 0) * sol_usd or None, "buyers": f.get("n_buyers"),
           "trades_5m": f.get("n_trades"), "vol_5m_usd": vol * sol_usd if sol_usd and f.get("n_trades") else None,
           "top10_pct": f.get("top10_pct"),
           "curve_pct": 100 * f["curve_progress"] if f.get("curve_progress") is not None else None,
           "bundle": bool(f["bundle_flag"]) if f.get("bundle_flag") is not None else None,
           "dev_pct": f.get("dev_buy_pct"), "age_s": o["decided_after_sec"], "risk": f.get("risk"),
           "dex": "launchlab" if o["pool"] == "bonk" else ("pumpfun" if o["pool"] == "pump" else o["pool"]),
           "sniper.seen_trades": o["seen_trades"], "sniper.reason": o["reason"] or "",
           "sniper.start_mc_usd": (f.get("start_mcap_sol") or 0) * sol_usd or None,
           "sniper.mc_sol": f.get("mcap_decide_sol"), "sniper.start_mc_sol": f.get("start_mcap_sol"),
           "sniper.sellers": f.get("n_sellers"), "sniper.buy_sol": f.get("buy_sol"), "sniper.sell_sol": f.get("sell_sol"),
           "sniper.net_sol": f.get("net_sol"), "sniper.block0": f.get("block0_wallets"),
           "sniper.bundle_wallets": f.get("bundle_wallets"), "sniper.bundle_pct": f.get("bundle_pct"),
           "sniper.unseen_pct": f.get("unseen_pct"), "sniper.insiders": f.get("insider_flag"),
           "sniper.dev_sold_pct": f.get("dev_sold_pct"), "sniper.dev_launches": f.get("dev_launches"),
           "sniper.dev_launches_24h": f.get("dev_launches_24h"), "sniper.known_bad": f.get("known_bad"),
           "sniper.fast_buyers": f.get("fast_buyers"), "sniper.smart_buyers": f.get("n_smart"),
           "sniper.good_buyers": f.get("n_good"), "sniper.momentum": f.get("momentum"), "sniper.fee": f.get("fee_pct"),
           "sniper.quote": _quote_label(f.get("quote_mint"))}
    return out


def _load(conn, mint):
    cur = conn.execute("SELECT * FROM sniper_launches WHERE mint = ?", (mint,))
    t = cur.fetchone()
    return dict(zip([c[0] for c in cur.description], t)) if t else None


def report_decision(conn, mint, cfg):
    """One decided launch into the shared opportunity table (brain.opportunities); a repeat is ignored there.
    Never raises: the table belongs to the brain and must not stop the sniper."""
    try:
        from brain.opportunities import report
        row = _load(conn, mint)
        if not row or row.get("source") != "pumpportal" or row.get("decision") in (None, "pending", "queued"):
            return False
        o = from_row(row, cfg)
        f = o["features"]
        sol_usd = row.get("sol_usd") or 0.0
        price = (f.get("mcap_decide_sol") or 0) / 1e9 or None
        new = report(conn, "sniper", mint, mint, o["ts"] or time.time(), shared_features(o, row), symbol=row.get("symbol") or "",
                     src_ts=row.get("created_ts"), price_sol=price, price_usd=price * sol_usd if price and sol_usd else None,
                     mcap_usd=(f.get("mcap_decide_sol") or 0) * sol_usd or None, source_score=row.get("score"),
                     source_action="took" if o["action"] == "enter" else "passed", source_reason=o["reason"])
        conn.commit()
        return new
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger("sniper").debug("Gelegenheit %s nicht gemeldet: %s", mint, e)
        return False


def report_result(conn, mint, cfg):
    """The launch's measured 60-minute net result (rug -1.0) into the opportunity table."""
    try:
        from brain.opportunities import source_result
        row = _load(conn, mint)
        net = net_result(row, cfg) if row else None
        if net is None:
            return False
        source_result(conn, "sniper", mint, net, horizon_min=HORIZON_MIN)
        conn.commit()
        return True
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger("sniper").debug("Ergebnis %s nicht gemeldet: %s", mint, e)
        return False
