"""One market analysis round, point in time: everything is computed from data that existed at `ts`.

Inputs: the shared DB (tokens, trades + wallets.tier, sniper_launches, market_tokens, market_sol and earlier
market_* rows) plus what the service fetched this round. The same functions run for live rounds and for the
backfill of past hours (then without the live-only parts: volume, net flow, meme sector)."""
import json
import math
import statistics
import time
from collections import Counter, defaultdict

from market import narratives
from market.config import settings as default_cfg

TIERS = ("ultra", "smart", "good")
MCAP_BUCKETS = [("<100k", 0, 1e5), ("100k-1M", 1e5, 1e6), ("1M-10M", 1e6, 1e7), (">10M", 1e7, float("inf"))]
AGE_BUCKETS = [("<1h", 0, 1), ("1-6h", 1, 6), ("6-24h", 6, 24), ("1-7d", 24, 168), (">7d", 168, float("inf"))]
LABELS = {"risk_off": "Risk-off", "neutral": "Neutral", "risk_on": "Risk-on", "hot": "Heiß"}

# phase inputs: weight, and what 0 / 1 mean (all mapped linearly and clipped)
PHASE_WEIGHTS = {
    "sol_trend": 0.12, "sol_week": 0.06, "meme_sector": 0.12, "breadth": 0.14, "volume": 0.10,
    "launch_rate": 0.08, "launch_hit": 0.14, "launch_survival": 0.08, "smart_flow": 0.16,
}
PHASE_WHY = {
    "sol_trend": "SOL 24 h", "sol_week": "SOL 7 Tage", "meme_sector": "Solana-Meme-Sektor 24 h (CoinGecko)",
    "breadth": "Breite: Anteil Coins im Plus (1 h)", "volume": "DEX-Volumen ggü. 24-h-Median",
    "launch_rate": "Launches pro Stunde ggü. 24-h-Median", "launch_hit": "Launches mit 2x in 60 min",
    "launch_survival": "Launches ohne Rug", "smart_flow": "Netto-Zufluss der Ultra/Smart/Good-Wallets (1 h)",
}


def lin(x, lo, hi):
    if x is None:
        return None
    return max(0.0, min(1.0, (x - lo) / (hi - lo)))


def _has(conn, t):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone() is not None


def _cols(conn, t):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({t})")}


def label_for(score, cfg=default_cfg):
    if score is None:
        return None
    if score >= cfg.hot_above:
        return "hot"
    if score >= cfg.riskon_above:
        return "risk_on"
    if score < cfg.riskoff_below:
        return "risk_off"
    return "neutral"


# ---------------------------------------------------------------- SOL
def sol_stats(points, ts):
    """Returns at 1 h, 24 h, 7 d and realised hourly volatility over 24 h from (ts, price) points up to ts."""
    pts = [(t, p) for t, p in points if t <= ts and p]
    if not pts:
        return {}
    now_p = pts[-1][1]

    def back(sec):
        cand = [p for t, p in pts if t <= ts - sec]
        if not cand or ts - sec - max(t for t, _ in pts if t <= ts - sec) > 3 * 3600:
            return None
        return now_p / cand[-1] - 1

    hourly = {}
    for t, p in pts:
        if t >= ts - 86400:
            hourly[t // 3600] = p
    hp = [hourly[k] for k in sorted(hourly)]
    rets = [math.log(b / a) for a, b in zip(hp, hp[1:]) if a and b]
    vol = statistics.pstdev(rets) if len(rets) >= 6 else None
    return {"sol_usd": now_p, "sol_ret_1h": back(3600), "sol_ret_24h": back(86400), "sol_ret_7d": back(7 * 86400),
            "sol_vol_24h": vol}


# ---------------------------------------------------------------- universe of traded coins
def universe(conn, ts, live=True, fresh_sec=1800):
    """Traded coins as of ts: market_tokens (with buys/sells) or core's tokens table (signals refresh it each
    cycle). Historical rounds only get price changes reconstructed from token_prices (no volume)."""
    out = {}
    if live:
        if _has(conn, "market_tokens"):
            for r in conn.execute("SELECT * FROM market_tokens WHERE updated_at>=? AND updated_at<=?",
                                  (ts - fresh_sec, ts + 60)):
                out[r["mint"]] = dict(r)
        if _has(conn, "tokens"):
            for r in conn.execute("SELECT mint, symbol, name, mcap_usd, liquidity_usd, pair_created_at, price_change_h1, "
                                  "price_change_h24, volume_h1, dex FROM tokens WHERE last_updated>=?", (ts - fresh_sec,)):
                if r["mint"] not in out:
                    out[r["mint"]] = dict(r, buys_h1=None, sells_h1=None, description="")
        return out
    if not _has(conn, "token_prices"):
        return out
    now_rows = {r[0]: r[1] for r in conn.execute(
        "SELECT mint, mcap_usd FROM token_prices WHERE ts BETWEEN ? AND ? AND mcap_usd>0", (ts - 900, ts))}
    then = {r[0]: r[1] for r in conn.execute(
        "SELECT mint, mcap_usd FROM token_prices WHERE ts BETWEEN ? AND ? AND mcap_usd>0", (ts - 4500, ts - 3000))}
    meta = {}
    if now_rows and _has(conn, "tokens"):
        mints = list(now_rows)
        for i in range(0, len(mints), 400):
            part = mints[i:i + 400]
            for r in conn.execute(f"SELECT mint, symbol, name, pair_created_at FROM tokens WHERE mint IN "
                                  f"({','.join('?' * len(part))})", part):
                meta[r["mint"]] = dict(r)
    for m, mc in now_rows.items():
        if m in then and then[m]:
            d = meta.get(m, {"mint": m, "symbol": "", "name": ""})
            out[m] = dict(d, mint=m, mcap_usd=mc, price_change_h1=(mc / then[m] - 1) * 100, volume_h1=None,
                          buys_h1=None, sells_h1=None, liquidity_usd=None)
    return out


def net_flow(r):
    """Estimated net buying in USD over 1 h: volume x (buys - sells) / (buys + sells). None without the split."""
    b, s, v = r.get("buys_h1"), r.get("sells_h1"), r.get("volume_h1")
    if b is None or s is None or not v or b + s <= 0:
        return None
    return v * (b - s) / (b + s)


def sector_of(r):
    if r.get("sectors_json"):
        try:
            d = json.loads(r["sectors_json"])
            if d.get("primary"):
                return d["primary"]
        except (TypeError, ValueError):
            pass
    return narratives.classify(r.get("name"), r.get("symbol"), r.get("description"))[0]


# ---------------------------------------------------------------- smart money and launches
def smart_flows(conn, ts, window=3600):
    """Net USD of tiered wallets per mint in [ts-window, ts): {mint: {"net", "buyers": set}}."""
    out = defaultdict(lambda: {"net": 0.0, "buyers": set()})
    if not (_has(conn, "wallets") and _has(conn, "trades")) or "tier" not in _cols(conn, "wallets"):
        return out
    q = ("SELECT t.mint, t.side, t.usd_amount, t.wallet FROM wallets w JOIN trades t ON t.wallet=w.address "
         "AND t.ts>=? AND t.ts<? WHERE w.tier IN ('ultra','smart','good') AND COALESCE(w.is_bot,0)=0")
    for r in conn.execute(q, (ts - window, ts)):
        usd = r["usd_amount"] or 0.0
        d = out[r["mint"]]
        if r["side"] == "buy":
            d["net"] += usd
            d["buyers"].add(r["wallet"])
        else:
            d["net"] -= usd
    return out


def launches(conn, since, until):
    if not _has(conn, "sniper_launches"):
        return []
    return [dict(r) for r in conn.execute(
        "SELECT mint, name, symbol, created_ts, max_ret_60m, rugged, migrated FROM sniper_launches "
        "WHERE COALESCE(created_ts, decided_ts)>=? AND COALESCE(created_ts, decided_ts)<?", (since, until))]


def launch_stats(conn, ts):
    """Launch rate (last hour vs median of the 24 hours before) and outcomes of launches whose 60 min window
    has ended by ts (no lookahead). A rate below 15 % of the median is a feed gap, not a market signal."""
    rows = launches(conn, ts - 25 * 3600, ts)
    if not rows:
        return {}
    per_h = Counter((ts - (r["created_ts"] or ts)) // 3600 for r in rows)
    last = per_h.get(0, 0)
    hist = [per_h.get(h, 0) for h in range(1, 25)]
    hist_nz = [h for h in hist if h > 0]
    med = statistics.median(hist_nz) if len(hist_nz) >= 3 else None
    out = {"launches_1h": last, "launch_median_h": med}
    if med and last >= 0.15 * med:
        out["launch_ratio"] = last / med
    done = [r for r in rows if (r["created_ts"] or ts) <= ts - 3600 and (r["created_ts"] or 0) >= ts - 6 * 3600
            and r["max_ret_60m"] is not None]
    if len(done) >= 30:
        out["launch_hit2x"] = sum(1 for r in done if r["max_ret_60m"] >= 1.0) / len(done)
        out["launch_rug"] = sum(1 for r in done if r["rugged"]) / len(done)
        out["launch_migrated"] = sum(1 for r in done if r["migrated"]) / len(done)
        out["launch_n_done"] = len(done)
    return out


def _baseline(conn, ts, col, hours=24):
    if not _has(conn, "market_snapshots"):
        return None
    vals = [r[0] for r in conn.execute(f"SELECT {col} FROM market_snapshots WHERE ts>=? AND ts<? AND {col} IS NOT NULL",
                                       (ts - hours * 3600, ts - 600))]
    return statistics.median(vals) if len(vals) >= 6 else None


# ---------------------------------------------------------------- phase
def phase(inputs):
    """inputs (any may be None) -> (score 0..100, coverage 0..1, contributions {name: {weight, value, points, why}}).
    Missing inputs drop out and the remaining weights are renormalised; coverage says how much was there."""
    v = {
        "sol_trend": lin(inputs.get("sol_ret_24h"), -0.08, 0.08),
        "sol_week": lin(inputs.get("sol_ret_7d"), -0.20, 0.20),
        "meme_sector": lin(inputs.get("meme_mcap_chg_24h"), -15, 15),
        "breadth": lin(inputs.get("breadth"), 0.25, 0.60),
        "volume": lin(math.log2(inputs["volume_ratio"]) if inputs.get("volume_ratio") else None, -1, 1),
        "launch_rate": lin(math.log2(inputs["launch_ratio"]) if inputs.get("launch_ratio") else None, -1, 1),
        "launch_hit": lin(inputs.get("launch_hit2x"), 0.03, 0.15),
        "launch_survival": lin(1 - inputs["launch_rug"] if inputs.get("launch_rug") is not None else None, 0.85, 1.0),
        "smart_flow": None if inputs.get("smart_net_usd") is None else 0.5 + 0.5 * math.tanh(inputs["smart_net_usd"] / 25000),
    }
    have = {k: x for k, x in v.items() if x is not None}
    wsum = sum(PHASE_WEIGHTS[k] for k in have)
    if wsum <= 0:
        return None, 0.0, {}
    contrib = {}
    for k, x in have.items():
        w = PHASE_WEIGHTS[k] / wsum
        contrib[k] = {"weight": round(w, 4), "value": round(x, 4), "points": round(100 * w * x, 2), "why": PHASE_WHY[k],
                      "raw": inputs.get({"sol_trend": "sol_ret_24h", "sol_week": "sol_ret_7d", "meme_sector": "meme_mcap_chg_24h",
                                         "volume": "volume_ratio", "launch_rate": "launch_ratio", "launch_hit": "launch_hit2x",
                                         "launch_survival": "launch_rug", "smart_flow": "smart_net_usd"}.get(k, k))}
    score = round(sum(c["points"] for c in contrib.values()), 1)
    return score, round(wsum / sum(PHASE_WEIGHTS.values()), 3), contrib


# ---------------------------------------------------------------- sectors, buckets
def _median(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def squash(x, scale):
    return None if x is None else 0.5 + 0.5 * math.tanh(x / scale)


def sector_heat(row, launch_edge=None):
    """0..100 from momentum (median 1 h change), breadth, share growth of volume, net flow and smart money."""
    parts = [(0.25, squash(row.get("median_ret_h1"), 15)), (0.15, row.get("breadth")),
             (0.20, squash(row.get("vol_share_chg"), 0.05)),
             (0.15, squash(row.get("net_flow_usd"), 50000)), (0.15, squash(row.get("smart_net_usd"), 10000)),
             (0.10, launch_edge)]
    have = [(w, x) for w, x in parts if x is not None]
    if not have:
        return None
    return round(100 * sum(w * x for w, x in have) / sum(w for w, _ in have), 1)


def sectors(conn, ts, uni, flows, recent_launches, done_launches, prev_shares=None):
    """Per primary sector: coins, volume and its share (and change vs ~1 h ago), net flow, median 1 h change,
    breadth, smart money, launches and their 2x rate, heat."""
    by = defaultdict(list)
    for m, r in uni.items():
        by[sector_of(r)].append((m, r))
    total_vol = sum((r.get("volume_h1") or 0) for r in uni.values()) or None
    l_by = Counter(narratives.classify(r["name"], r["symbol"])[0] for r in recent_launches)
    d_by, h_by = Counter(), Counter()
    for r in done_launches:
        s = narratives.classify(r["name"], r["symbol"])[0]
        d_by[s] += 1
        h_by[s] += (r["max_ret_60m"] or 0) >= 1.0
    base_hit = (sum(h_by.values()) / sum(d_by.values())) if d_by else None
    rows = []
    for s in set(by) | set(l_by):
        toks = by.get(s, [])
        vol = sum((r.get("volume_h1") or 0) for _, r in toks)
        nfs = [net_flow(r) for _, r in toks]
        chg = [r.get("price_change_h1") for _, r in toks if r.get("price_change_h1") is not None]
        smart_net = sum(flows[m]["net"] for m, _ in toks if m in flows)
        buyers = set().union(*[flows[m]["buyers"] for m, _ in toks if m in flows]) if toks else set()
        share = vol / total_vol if total_vol else None
        hit = None
        edge = None
        if d_by.get(s, 0) >= 10 and base_hit:
            hit = (h_by[s] + base_hit * 10) / (d_by[s] + 10)
            edge = lin(hit / base_hit, 0.5, 1.5)
        top = sorted(toks, key=lambda x: -(x[1].get("volume_h1") or abs(x[1].get("price_change_h1") or 0)))[:5]
        row = {"ts": ts, "sector": s, "n_tokens": len(toks), "mcap_usd": round(sum((r.get("mcap_usd") or 0) for _, r in toks), 2),
               "volume_h1_usd": round(vol, 2) if total_vol else None, "vol_share": round(share, 5) if share is not None else None,
               "vol_share_chg": None, "net_flow_usd": round(sum(x for x in nfs if x is not None), 2) if any(x is not None for x in nfs) else None,
               "median_ret_h1": _median(chg), "breadth": (sum(1 for c in chg if c > 0) / len(chg)) if chg else None,
               "smart_net_usd": round(smart_net, 2), "smart_buyers": len(buyers), "launches": l_by.get(s, 0),
               "launch_hit2x": round(hit, 4) if hit is not None else None,
               "top_json": json.dumps([{"mint": m, "symbol": r.get("symbol"), "ret_h1": r.get("price_change_h1"),
                                        "volume_h1": r.get("volume_h1")} for m, r in top], separators=(",", ":"))}
        if prev_shares and share is not None and s in prev_shares and prev_shares[s] is not None:
            row["vol_share_chg"] = round(share - prev_shares[s], 5)
        row["heat"] = sector_heat(row, edge)
        rows.append(row)
    rows.sort(key=lambda r: -(r["heat"] or 0))
    return rows


def buckets(ts, uni, flows):
    """Where the money sits by market cap and by age: volume share, net flow, median 1 h change, smart money."""
    out = []
    total = sum((r.get("volume_h1") or 0) for r in uni.values()) or None
    for kind, defs in (("mcap", MCAP_BUCKETS), ("age", AGE_BUCKETS)):
        groups = defaultdict(list)
        for m, r in uni.items():
            if kind == "mcap":
                x = r.get("mcap_usd")
            else:
                pc = r.get("pair_created_at")
                x = (ts - pc) / 3600 if pc else None
            if x is None:
                continue
            for name, lo, hi in defs:
                if lo <= x < hi:
                    groups[name].append((m, r))
                    break
        for name, _, _ in defs:
            g = groups.get(name, [])
            vol = sum((r.get("volume_h1") or 0) for _, r in g)
            nfs = [net_flow(r) for _, r in g]
            out.append({"ts": ts, "kind": kind, "bucket": name, "n": len(g), "volume_h1_usd": round(vol, 2) if total else None,
                        "vol_share": round(vol / total, 5) if total else None,
                        "net_flow_usd": round(sum(x for x in nfs if x is not None), 2) if any(x is not None for x in nfs) else None,
                        "median_ret_h1": _median([r.get("price_change_h1") for _, r in g]),
                        "smart_net_usd": round(sum(flows[m]["net"] for m, _ in g if m in flows), 2)})
    return out


def term_volume(uni):
    vol = Counter()
    for r in uni.values():
        v = r.get("volume_h1") or 0
        if v:
            for t in narratives.terms_of(r.get("name"), r.get("symbol")):
                vol[t] += v
    return vol


def prev_sector_shares(conn, ts, back=3600):
    if not _has(conn, "market_sectors"):
        return None
    r = conn.execute("SELECT MAX(ts) FROM market_sectors WHERE ts<=? AND ts>=? AND vol_share IS NOT NULL",
                     (ts - back + 600, ts - back - 1800)).fetchone()
    if not r or not r[0]:
        return None
    return {x[0]: x[1] for x in conn.execute("SELECT sector, vol_share FROM market_sectors WHERE ts=?", (r[0],))}


# ---------------------------------------------------------------- the whole round
def compute(conn, ts=None, external=None, live=True, cfg=default_cfg):
    """-> (snapshot row, sector rows, term rows, bucket rows). `external`: {"meme": {...}} fetched this round."""
    ts = int(ts or time.time())
    external = external or {}
    from market import store
    sol = sol_stats(store.sol_series(conn, ts - 8 * 86400, ts) if _has(conn, "market_sol") else [], ts)
    uni = universe(conn, ts, live=live)
    flows = smart_flows(conn, ts)
    ls = launch_stats(conn, ts)
    chg = [r.get("price_change_h1") for r in uni.values() if r.get("price_change_h1") is not None]
    breadth = (sum(1 for c in chg if c > 0) / len(chg)) if len(chg) >= 20 else None
    vols = sorted((r.get("volume_h1") or 0 for r in uni.values()), reverse=True)[:100]
    vol100 = sum(vols) if live and any(vols) else None
    nfs = [net_flow(r) for r in uni.values()]
    vb = _baseline(conn, ts, "volume_h1_usd")
    smart_net = sum(d["net"] for d in flows.values()) if flows or live else None
    meme = external.get("meme") or {}
    inputs = dict(sol, breadth=breadth, volume_ratio=(vol100 / vb) if vol100 and vb else None,
                  meme_mcap_chg_24h=meme.get("change_24h"), smart_net_usd=smart_net, **ls)
    score, coverage, contrib = phase(inputs)
    recent = launches(conn, ts - cfg.term_recent_hours * 3600, ts)
    base = launches(conn, ts - cfg.term_base_hours * 3600, ts)
    done = [r for r in launches(conn, ts - cfg.term_base_hours * 3600, ts - 3600) if r["max_ret_60m"] is not None]
    sec_rows = sectors(conn, ts, uni, flows, [r for r in recent if (r["created_ts"] or 0) >= ts - 3600], done,
                       prev_sector_shares(conn, ts))
    terms = narratives.emergent_terms(
        [(r["mint"], r["name"], r["symbol"]) for r in recent], [(r["mint"], r["name"], r["symbol"]) for r in base],
        outcomes=[(r["name"], r["symbol"], int((r["max_ret_60m"] or 0) >= 1.0), int(bool(r["rugged"]))) for r in done],
        volume=term_volume(uni) if live else None, min_count=cfg.term_min_count, keep=cfg.terms_kept)
    term_rows = [dict({k: v for k, v in t.items() if k != "mints"}, ts=ts, mints_json=json.dumps(t["mints"])) for t in terms]
    bucket_rows = buckets(ts, uni, flows) if live else []
    snap = {"ts": ts, "score": score, "label": label_for(score, cfg), "coverage": coverage,
            "components_json": json.dumps(contrib, separators=(",", ":")),
            "sol_usd": sol.get("sol_usd"), "sol_ret_1h": sol.get("sol_ret_1h"), "sol_ret_24h": sol.get("sol_ret_24h"),
            "sol_ret_7d": sol.get("sol_ret_7d"), "sol_vol_24h": sol.get("sol_vol_24h"),
            "meme_mcap_usd": meme.get("market_cap"), "meme_mcap_chg_24h": meme.get("change_24h"), "breadth": breadth,
            "universe_n": len(uni), "volume_h1_usd": vol100, "volume_ratio": inputs["volume_ratio"],
            "net_flow_usd": round(sum(x for x in nfs if x is not None), 2) if any(x is not None for x in nfs) else None,
            "launches_1h": ls.get("launches_1h"), "launch_ratio": ls.get("launch_ratio"),
            "launch_hit2x": ls.get("launch_hit2x"), "launch_rug": ls.get("launch_rug"),
            "launch_migrated": ls.get("launch_migrated"), "smart_net_usd": smart_net,
            "smart_buyers": len(set().union(*[d["buyers"] for d in flows.values()])) if flows else 0,
            "top_sector": next((r["sector"] for r in sec_rows if r["sector"] != narratives.OTHER and r["heat"] is not None), None),
            "top_term": terms[0]["term"] if terms else None, "backfilled": 0 if live else 1}
    return snap, sec_rows, term_rows, bucket_rows
