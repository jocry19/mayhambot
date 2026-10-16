"""Stages 3 and 4: the only network calls of the Zentrale, all keyless and budgeted per minute.

Stage 3, market depth: Jupiter quotes SOL -> token -> SOL for 0.1 / 0.25 / 0.5 SOL on the hottest coins (open
positions first). The round-trip loss is the real execution cost (price impact both ways + pool fees), the
on-chain counterpart of the spread a market maker charges (Glosten-Milgrom). Table intel_quotes.
Stage 4, macro and attention: BTC / ETH / SOL with 24 h change (CoinGecko simple/price), Crypto Fear & Greed
(alternative.me), and the DexScreener boost level of each coin over time (from market_tokens, no call). Tables
intel_macro and intel_attention."""
import logging

from core import http
from intel import store

log = logging.getLogger("intel")
SOL_MINT = "So11111111111111111111111111111111111111112"
SIZES = (0.1, 0.25, 0.5)
COINGECKO = "https://api.coingecko.com/api/v3/simple/price"
FNG = "https://api.alternative.me/fng/"

SCHEMA = """
CREATE TABLE IF NOT EXISTS intel_quotes (
    mint TEXT, ts INTEGER, size_sol REAL, tokens_raw REAL, sol_back REAL, rt_cost REAL, impact_buy REAL,
    impact_sell REAL, hops INTEGER, error TEXT, PRIMARY KEY (mint, ts, size_sol)
);
CREATE TABLE IF NOT EXISTS intel_macro (
    ts INTEGER PRIMARY KEY, btc_usd REAL, btc_chg_24h REAL, eth_usd REAL, eth_chg_24h REAL, sol_usd REAL,
    sol_chg_24h REAL, fng INTEGER, fng_label TEXT
);
CREATE TABLE IF NOT EXISTS intel_attention (
    mint TEXT, ts INTEGER, boosted REAL, n_socials INTEGER, source TEXT, PRIMARY KEY (mint, ts)
);
"""


def ensure(conn):
    if not store.has(conn, "intel_attention"):
        from core import db
        db.retry_locked(conn, lambda: conn.executescript(SCHEMA))


def _get(url, params=None):
    return http.get_json(url, params=params, retries=1)


def quote(mint, size_sol, jupiter_url, get=_get):
    """One round trip. Returns a dict for intel_quotes (rt_cost None + error when there is no route)."""
    lam = int(size_sol * 1e9)
    row = {"mint": mint, "size_sol": size_sol, "tokens_raw": None, "sol_back": None, "rt_cost": None,
           "impact_buy": None, "impact_sell": None, "hops": None, "error": None}
    try:
        a = get(f"{jupiter_url}/swap/v1/quote", {"inputMint": SOL_MINT, "outputMint": mint, "amount": lam, "slippageBps": 500})
        out = int((a or {}).get("outAmount") or 0)
        if not out:
            row["error"] = "no_buy_route"
            return row
        b = get(f"{jupiter_url}/swap/v1/quote", {"inputMint": mint, "outputMint": SOL_MINT, "amount": out, "slippageBps": 500})
        back = int((b or {}).get("outAmount") or 0)
        if not back:
            row["error"] = "no_sell_route"
            return row
        row.update(tokens_raw=out, sol_back=back / 1e9, rt_cost=1 - back / lam,
                   impact_buy=_f(a.get("priceImpactPct")), impact_sell=_f(b.get("priceImpactPct")),
                   hops=len(a.get("routePlan") or []) + len(b.get("routePlan") or []))
    except Exception as e:  # noqa: BLE001 - a 400 is "no route", anything else is noted and retried next round
        row["error"] = str(e)[:80]
    return row


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def macro(get=_get):
    out = {}
    try:
        p = get(COINGECKO, {"ids": "bitcoin,ethereum,solana", "vs_currencies": "usd", "include_24hr_change": "true"}) or {}
        for k, c in (("bitcoin", "btc"), ("ethereum", "eth"), ("solana", "sol")):
            x = p.get(k) or {}
            out[f"{c}_usd"] = _f(x.get("usd"))
            ch = _f(x.get("usd_24h_change"))
            out[f"{c}_chg_24h"] = None if ch is None else ch / 100
    except Exception as e:  # noqa: BLE001
        log.info("intel macro: CoinGecko %s", str(e)[:80])
    try:
        d = (get(FNG, {"limit": 1}) or {}).get("data") or []
        if d:
            out["fng"] = int(d[0].get("value"))
            out["fng_label"] = d[0].get("value_classification")
    except Exception as e:  # noqa: BLE001
        log.info("intel macro: Fear&Greed %s", str(e)[:80])
    return out


def attention_rows(conn, now):
    """Boost level of every promoted / traded coin right now, written only when it changed (history of attention)."""
    if not store.has(conn, "market_tokens"):
        return []
    last = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT a.mint, a.boosted, a.n_socials FROM intel_attention a JOIN (SELECT mint, MAX(ts) t FROM intel_attention GROUP BY mint) m "
        "ON a.mint=m.mint AND a.ts=m.t")}
    out = []
    for m, b, s, src in conn.execute("SELECT mint, COALESCE(boosted,0), COALESCE(n_socials,0), source FROM market_tokens WHERE updated_at>?",
                                     (now - 1800,)):
        if last.get(m) != (b, s) and (b or m in last):
            out.append({"mint": m, "ts": now, "boosted": b, "n_socials": s, "source": src})
    return out


def save(conn, table, rows):
    if not rows:
        return 0
    cols = list(rows[0])
    from core import db

    def write():
        conn.executemany(f"INSERT OR REPLACE INTO {table}({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                         [tuple(r.get(c) for c in cols) for r in rows])
        conn.commit()
    db.retry_locked(conn, write)
    return len(rows)


def hot_mints(conn, now, n):
    """Coins whose real execution cost matters most: open positions, recent signals and sniper entries, then the
    coins with the most smart money in the live view."""
    out = []
    if store.has(conn, "ct_positions"):
        out += [r[0] for r in conn.execute("SELECT DISTINCT mint FROM ct_positions WHERE status='open'")]
    out += [r[0] for r in conn.execute("SELECT mint FROM signals WHERE ts>? ORDER BY ts DESC LIMIT 10", (now - 3 * 3600,))]
    if store.has(conn, "sniper_launches"):
        out += [r[0] for r in conn.execute("SELECT mint FROM sniper_launches WHERE decision='enter' AND decided_ts>? LIMIT 10",
                                           (now - 3 * 3600,))]
    import json
    rows = conn.execute("SELECT key, features_json FROM intel_latest WHERE kind='coin' AND ts>?", (now - 900,)).fetchall()
    ranked = sorted(rows, key=lambda r: -((json.loads(r[1]).get("flow_smart_entities_60m") or 0) * 1e6
                                          + (json.loads(r[1]).get("vol_usd_60m") or 0)))
    out += [r[0] for r in ranked]
    return list(dict.fromkeys(out))[:n]


def quote_round(conn, now, cfg, get=_get):
    mints = hot_mints(conn, now, cfg.quote_coins)
    rows = []
    for m in mints:
        for s in SIZES:
            r = quote(m, s, cfg.jupiter_url, get)
            r["ts"] = now
            rows.append(r)
            if r["error"] and "route" not in r["error"]:
                break  # the API is down or throttled: no point in asking the other sizes
    save(conn, "intel_quotes", rows)
    return len(rows)


def measured_cost(conn, mint, ts, max_age=1800):
    """{exec_q_0_1, exec_q_0_25, exec_q_0_5, exec_q_age_s} from the newest quotes <= ts (None when not measured)."""
    out = {"exec_q_0_1": None, "exec_q_0_25": None, "exec_q_0_5": None, "exec_q_noroute": None}
    if not store.has(conn, "intel_quotes"):
        return out
    r = conn.execute("SELECT MAX(ts) FROM intel_quotes WHERE mint=? AND ts<=? AND ts>?", (mint, ts, ts - max_age)).fetchone()
    if not r or not r[0]:
        return out
    for size, cost, err in conn.execute("SELECT size_sol, rt_cost, error FROM intel_quotes WHERE mint=? AND ts=?", (mint, r[0])):
        out[{0.1: "exec_q_0_1", 0.25: "exec_q_0_25", 0.5: "exec_q_0_5"}[size]] = cost
        if err and "route" in err:
            out["exec_q_noroute"] = 1
    if out["exec_q_noroute"] is None and any(out[k] is not None for k in ("exec_q_0_1", "exec_q_0_25", "exec_q_0_5")):
        out["exec_q_noroute"] = 0
    return out


def attention(conn, mint, ts):
    out = {"att_boost": None, "att_boost_chg_1h": None, "att_promoted_h": None}
    if not store.has(conn, "intel_attention"):
        return out
    rows = conn.execute("SELECT ts, boosted FROM intel_attention WHERE mint=? AND ts<=? ORDER BY ts", (mint, ts)).fetchall()
    if not rows:
        return out
    out["att_boost"] = rows[-1][1]
    old = [b for t, b in rows if t <= ts - 3600]
    out["att_boost_chg_1h"] = rows[-1][1] - (old[-1] if old else 0)
    first = next((t for t, b in rows if b), None)
    if first:
        out["att_promoted_h"] = (ts - first) / 3600
    return out


def macro_at(conn, ts, max_age=3 * 3600):
    if not store.has(conn, "intel_macro"):
        return None
    r = conn.execute("SELECT * FROM intel_macro WHERE ts<=? AND ts>? ORDER BY ts DESC LIMIT 1", (ts, ts - max_age)).fetchone()
    return dict(r) if r else None


def macro_round(conn, now, get=_get):
    m = macro(get)
    if not m:
        return 0
    prev = macro_at(conn, now, 6 * 3600) or {}
    if "fng" not in m and prev.get("fng") is not None:  # Fear & Greed changes daily: keep the last value
        m["fng"], m["fng_label"] = prev["fng"], prev.get("fng_label")
    return save(conn, "intel_macro", [dict(m, ts=now)])

