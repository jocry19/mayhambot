"""Zentrale: what the bot sees and knows, as point-in-time features with coverage, a live ticker and the
measurement whether each source helps. Read-only interface for copytrade/ (the brain), model/, signals/, web/, cli/
(see intel/README.md). Nothing here trades or calls the network."""
import json

from intel import coin as _coin
from intel import marketctx as _market
from intel import wallet as _wallet
from intel.evaluate import evaluate as evaluation  # noqa: F401
from intel.panel import coin_view, view  # noqa: F401

COIN_FEATURES = tuple(_coin.FEATURES)
WALLET_FEATURES = tuple(_wallet.FEATURES)
MARKET_FEATURES = tuple(_market.FEATURES)
FEATURES = {"coin": _coin.FEATURES, "wallet": _wallet.FEATURES, "market": _market.FEATURES}


def features_at(kind, key, ts, conn=None):
    """{"kind", "key", "ts", "features": {name: float|None}, "coverage": {group: 0..1, "all": 0..1}, "meta": {...}}
    computed only from data with timestamps <= ts. kind 'coin' (key = mint), 'wallet' (key = address) or
    'market' (key ignored, use 'solana'). None means unknown, never 0."""
    from intel import store
    conn = conn or store.connect()
    ts = int(ts)
    if kind == "coin":
        r = _coin.coin_features(conn, key, ts)
    elif kind == "wallet":
        r = _wallet.wallet_features(conn, key, ts)
    elif kind == "market":
        r = _market.market_features(conn, ts)
        key = "solana"
    else:
        raise ValueError(f"unknown kind {kind!r} (coin, wallet, market)")
    return {"kind": kind, "key": key, "ts": ts, **r}


def feature_table(conn, kind, since, until=None, keys=None):
    """Stored history as long rows [(key, ts, feature, value)] (value None = unknown), oldest first.
    Coin and market rows every 15 min, wallet rows every hour, written by the intel service."""
    sql = "SELECT key, ts, features_json FROM intel_snapshots WHERE kind=? AND ts>=?"
    args = [kind, int(since)]
    if until is not None:
        sql += " AND ts<=?"
        args.append(int(until))
    if keys:
        sql += f" AND key IN ({','.join('?' * len(keys))})"
        args += list(keys)
    out = []
    for k, t, fj in conn.execute(sql + " ORDER BY ts", args):
        for name, v in json.loads(fj).items():
            out.append((k, t, name, v))
    return out


def latest(conn, kind, key):
    """Newest features the service computed for one coin/wallet/market (at most a few minutes old), or None."""
    r = conn.execute("SELECT ts, coverage, features_json, meta_json FROM intel_latest WHERE kind=? AND key=?",
                     (kind, key)).fetchone()
    if not r:
        return None
    return {"kind": kind, "key": key, "ts": r[0], "coverage_all": r[1], "features": json.loads(r[2]),
            "meta": json.loads(r[3] or "{}")}


def deep_analysis(conn):
    """Latest deep analysis (event study, robustness per feature, redundancy, walk-forward playbook), refreshed every
    30 min by the service; None before the first run. See intel/deep.py."""
    from intel import store
    return store.get_state(conn, "deep") if store.has(conn, "intel_state") else None


def setups(conn, mint, ts=None):
    """Confirmed playbook setups the coin matches right now (or at ts): [{"kind": "good"|"bad", "text", "lift"}].
    Uses the playbook found on older data and confirmed on newer data; the brain decides what to do with it."""
    from intel import deep
    book = (deep_analysis(conn) or {}).get("playbook")
    f = features_at("coin", mint, ts, conn)["features"] if ts else (latest(conn, "coin", mint) or {}).get("features")
    if not f:
        f = features_at("coin", mint, __import__("time").time(), conn)["features"]
    return [{"kind": k, "text": t, "lift": lift} for k, t, lift in deep.matching(f, book)]
