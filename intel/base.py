"""Shared point-in-time lookups: wallet tiers and entities (clusters) as they were known at ts, SOL price,
the price/liquidity series of a coin. Pure reads of the shared DB, no network."""
import math
import time

from intel.store import has

TIERS = ("ultra", "smart", "good")
ACTIVATION_LAG_S = 3600  # model_versions.ts is the retrain start; activation follows after the backtest
_CACHE = {}


def _cached(key, ttl, fn):
    hit = _CACHE.get(key)
    now = time.time()
    if hit and now - hit[0] < ttl:
        return hit[1]
    v = fn()
    _CACHE[key] = (now, v)
    return v


def clear_cache():
    _CACHE.clear()


def tiers_at(conn, ts):
    """{wallet: tier} as known at ts: the model version that was active then (model_tier_history), else the
    current wallets.tier (flag pit=False: the tier may contain knowledge from after ts)."""
    def load():
        if has(conn, "model_versions") and has(conn, "model_tier_history"):
            # only versions that went live (their tiers were written to model_tier_history), and only once the
            # retrain that started at model_versions.ts could have finished (ACTIVATION_LAG_S, conservative)
            v = conn.execute("SELECT id FROM model_versions WHERE ts<=? AND id IN (SELECT DISTINCT version FROM model_tier_history) "
                             "ORDER BY ts DESC LIMIT 1", (ts - ACTIVATION_LAG_S,)).fetchone()
            if v:
                rows = conn.execute("SELECT address, tier FROM model_tier_history WHERE version=? AND tier IS NOT NULL",
                                    (v[0],)).fetchall()
                if rows:
                    return {r[0]: r[1] for r in rows}, True
        rows = conn.execute("SELECT address, tier FROM wallets WHERE tier IS NOT NULL").fetchall() \
            if "tier" in {r[1] for r in conn.execute("PRAGMA table_info(wallets)")} else []
        return {r[0]: r[1] for r in rows}, False
    return _cached(("tiers", ts // 600), 300, load)


def clusters(conn, ts=None):
    """{wallet: cluster_id} from the active model (same funder or repeated co-buys within 60 s = one entity).
    Point in time: with ts, only when the active version could already have been live at ts, else {} (every
    wallet is its own entity, a weaker but honest count)."""
    if ts is not None and has(conn, "model_versions"):
        v = conn.execute("SELECT MAX(ts) FROM model_versions WHERE active=1").fetchone()
        if not v or not v[0] or v[0] > ts - ACTIVATION_LAG_S:
            return {}

    def load():
        out = {}
        if has(conn, "model_wallet_stats") and "cluster_id" in {r[1] for r in conn.execute("PRAGMA table_info(model_wallet_stats)")}:
            out.update({r[0]: r[1] for r in conn.execute("SELECT address, cluster_id FROM model_wallet_stats WHERE cluster_id IS NOT NULL")})
        if "cluster_id" in {r[1] for r in conn.execute("PRAGMA table_info(wallets)")}:
            for a, c in conn.execute("SELECT address, cluster_id FROM wallets WHERE cluster_id IS NOT NULL"):
                out.setdefault(a, c)
        return out
    return _cached("clusters", 600, load)


def entity(cl, wallet):
    c = cl.get(wallet)
    return f"c:{c}" if c else wallet


def sol_usd_at(conn, ts):
    if has(conn, "market_sol"):
        r = conn.execute("SELECT price_usd FROM market_sol WHERE ts<=? AND ts>=? ORDER BY ts DESC LIMIT 1",
                         (ts, ts - 6 * 3600)).fetchone()
        if r and r[0]:
            return float(r[0])
    r = conn.execute("SELECT usd_amount/sol_amount FROM trades WHERE ts<=? AND ts>=? AND sol_amount>0 AND usd_amount>0 "
                     "ORDER BY ts DESC LIMIT 1", (ts, ts - 6 * 3600)).fetchone()
    return float(r[0]) if r and r[0] else None


def series(conn, mint, since, until):
    """[(ts, price, mcap, liq)] from token_prices, oldest first."""
    return conn.execute("SELECT ts, price_usd, mcap_usd, liquidity_usd FROM token_prices WHERE mint=? AND ts>? AND ts<=? "
                        "ORDER BY ts", (mint, since, until)).fetchall()


def at_or_before(pts, t, idx=1, max_gap=None):
    """Last value of column idx with ts <= t (pts sorted), None if missing or older than max_gap."""
    best = None
    for p in pts:
        if p[0] > t:
            break
        if p[idx] is not None:
            best = p
    if best is None or (max_gap is not None and t - best[0] > max_gap):
        return None
    return best[idx]


def realized_vol(prices):
    """Std of consecutive log returns, scaled by sqrt(n): the move one can expect over the whole window."""
    rs = [math.log(b / a) for a, b in zip(prices, prices[1:]) if a and b and a > 0 and b > 0]
    if len(rs) < 2:
        return None
    m = sum(rs) / len(rs)
    var = sum((r - m) ** 2 for r in rs) / (len(rs) - 1)
    return math.sqrt(var) * math.sqrt(len(rs))


def ratio(a, b):
    return None if a is None or not b else a / b
