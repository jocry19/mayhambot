"""Smart-wallet scoring from each wallet's own track record (trades stored in the DB)."""
import bisect
import math
import sqlite3
import time
from collections import defaultdict
from statistics import median

from core.config import settings


def wallet_quality(positions, exclude_mint=None, cfg=settings) -> dict:
    """Pure function. positions: dicts with mint, buy_usd, roi, pnl, peak_multiple.
    exclude_mint lets the signal engine judge a wallet without the token it is buying right now."""
    P = [p for p in positions if p["mint"] != exclude_mint and (p["buy_usd"] or 0) >= cfg.min_position_usd]
    n = len(P)
    if n == 0:
        return dict(score=0.0, n=0, win_rate=None, early_rate=None, roi=None, consistency=None,
                    total_pnl=0.0, median_peak=None)
    wins = sum(1 for p in P if (p["roi"] or 0) > 0.05)
    early = sum(1 for p in P if (p["peak_multiple"] or 0) >= cfg.early_hit_multiple)
    win_rate = (wins + 1) / (n + 2)
    early_rate = (early + 0.5) / (n + 2)
    total_buy = sum(p["buy_usd"] for p in P)
    total_pnl = sum(p["pnl"] for p in P)
    roi = total_pnl / total_buy if total_buy else 0.0
    roi_score = 0.5 + 0.5 * math.tanh(roi)
    pos = [p["pnl"] for p in P if p["pnl"] > 0]
    consistency = min(1.0, 2 * (1 - max(pos) / sum(pos))) if total_pnl > 0 and pos else 0.0
    raw = 0.30 * win_rate + 0.25 * early_rate + 0.25 * roi_score + 0.20 * consistency
    confidence = 1 - math.exp(-n / 3)
    score = 100 * raw * math.sqrt(confidence)
    peaks = [p["peak_multiple"] for p in P if p["peak_multiple"]]
    return dict(score=round(score, 1), n=n, win_rate=wins / n, early_rate=early / n, roi=roi,
                consistency=consistency, total_pnl=total_pnl, median_peak=median(peaks) if peaks else None)


def tier_for(score, n_tokens, is_bot=False, manual=False, cfg=settings):
    """'ultra' | 'smart' | 'good' | None. Smart and above count as smart money (wallets.is_smart=1)."""
    if is_bot:
        return None
    if manual:
        return "ultra" if score >= cfg.tier_ultra_score else "smart"
    if n_tokens < cfg.smart_min_tokens:
        return None
    if score >= cfg.tier_ultra_score and n_tokens >= cfg.smart_min_tokens + 1:
        return "ultra"
    if score >= cfg.smart_min_score:
        return "smart"
    if score >= cfg.tier_good_score:
        return "good"
    return None


def _price_index(conn):
    """mint -> (sorted ts list, suffix max of price) built from trades + DexScreener snapshots."""
    series = defaultdict(list)
    for r in conn.execute("SELECT mint, ts, price_usd FROM trades WHERE price_usd > 0"):
        series[r[0]].append((r[1], r[2]))
    for r in conn.execute("SELECT mint, ts, price_usd FROM token_prices WHERE price_usd > 0"):
        series[r[0]].append((r[1], r[2]))
    idx = {}
    for mint, pts in series.items():
        pts.sort()
        ts = [p[0] for p in pts]
        prices = [p[1] for p in pts]
        # robust peak: a price only counts if a neighbour confirms it within 3x (filters single bad prints)
        conf = []
        for i, pr in enumerate(prices):
            nb = [prices[j] for j in (i - 1, i + 1) if 0 <= j < len(prices)]
            conf.append(pr if not nb or any(pr <= 3 * x for x in nb) else min(nb))
        suf = conf[:]
        for i in range(len(suf) - 2, -1, -1):
            suf[i] = max(suf[i], suf[i + 1])
        idx[mint] = (ts, suf)
    return idx


def _peak_after(idx, mint, ts):
    if mint not in idx:
        return None
    tss, suf = idx[mint]
    i = bisect.bisect_left(tss, ts)
    return suf[i] if i < len(suf) else None


def compute_positions(conn):
    cur = {r["mint"]: (r["price_usd"] or 0, r["liquidity_usd"] or 0)
           for r in conn.execute("SELECT mint, price_usd, liquidity_usd FROM tokens")}
    pidx = _price_index(conn)
    agg = {}
    last_price = {}
    for r in conn.execute("SELECT wallet, mint, side, ts, token_amount, usd_amount, price_usd FROM trades ORDER BY ts"):
        k = (r["wallet"], r["mint"])
        a = agg.setdefault(k, dict(buy_usd=0.0, sell_usd=0.0, buy_tokens=0.0, sell_tokens=0.0,
                                   first_buy_ts=None, first_buy_price=None, trades=0, last_ts=0))
        a["trades"] += 1
        a["last_ts"] = r["ts"]
        if r["side"] == "buy":
            a["buy_usd"] += r["usd_amount"] or 0
            a["buy_tokens"] += r["token_amount"] or 0
            if a["first_buy_ts"] is None:
                a["first_buy_ts"], a["first_buy_price"] = r["ts"], r["price_usd"]
        else:
            a["sell_usd"] += r["usd_amount"] or 0
            a["sell_tokens"] += r["token_amount"] or 0
        if r["price_usd"]:
            last_price[r["mint"]] = r["price_usd"]
    out = []
    for (wallet, mint), a in agg.items():
        if a["buy_tokens"] <= 0:
            continue  # only sells seen (bought before our data): no cost basis
        avg_cost = a["buy_usd"] / a["buy_tokens"]
        sold = min(a["sell_tokens"], a["buy_tokens"])
        sell_usd = a["sell_usd"] * (sold / a["sell_tokens"]) if a["sell_tokens"] else 0.0
        realized = sell_usd - avg_cost * sold
        remaining = a["buy_tokens"] - sold
        price, liq = cur.get(mint, (0, 0))
        price = price or last_price.get(mint, 0)
        if liq and liq < 1000:
            price = 0.0  # rugged / dead pool: holdings are not sellable
        unrealized = remaining * price - avg_cost * remaining
        pnl = realized + unrealized
        peak = _peak_after(pidx, mint, a["first_buy_ts"])
        peak = max(peak or 0, price or 0)
        fbp = a["first_buy_price"]
        out.append(dict(wallet=wallet, mint=mint, buy_usd=a["buy_usd"], sell_usd=a["sell_usd"],
                        buy_tokens=a["buy_tokens"], sell_tokens=a["sell_tokens"], realized_pnl_usd=realized,
                        unrealized_pnl_usd=unrealized, pnl=pnl, roi=pnl / a["buy_usd"] if a["buy_usd"] else 0,
                        first_buy_ts=a["first_buy_ts"], first_buy_price=fbp,
                        peak_multiple=(peak / fbp) if fbp and peak else None, trades=a["trades"],
                        last_ts=a["last_ts"]))
    return out


def load_positions(conn):
    """wallet -> list of position dicts (as stored by rescore_all)."""
    by = defaultdict(list)
    for r in conn.execute("SELECT *, realized_pnl_usd + unrealized_pnl_usd AS pnl FROM wallet_positions"):
        by[r["wallet"]].append(dict(r))
    return by


def model_tiers(conn):
    """(active model version, {address: (tier, score, cluster_id, exit_capture)}) from model/, or (None, {})
    while no model version is active. model_wallet_stats only ever holds the active version's wallets."""
    try:
        v = conn.execute("SELECT id FROM model_versions WHERE active=1 ORDER BY id DESC LIMIT 1").fetchone()
        if not v:
            return None, {}
        cols = {r[1] for r in conn.execute("PRAGMA table_info(model_wallet_stats)")}
        extra = [c if c in cols else "NULL" for c in ("cluster_id", "exit_capture")]
        rows = conn.execute(f"SELECT address, tier, score, {extra[0]}, {extra[1]} FROM model_wallet_stats "
                            "WHERE tier IS NOT NULL").fetchall()
    except sqlite3.OperationalError:  # model/ tables not created yet
        return None, {}
    return v[0], {r[0]: (r[1], r[2], r[3], r[4]) for r in rows}


last_tier_sync = {}


def sync_model_tiers(conn):
    """Once a model version is active, model_wallet_stats is the one truth for tiers: copy tier, model score,
    cluster and exit capture into wallets (missing wallets are added). Manual wallets stay smart at least.
    Without an active version the heuristic tiers from rescore_all stay. Caller commits."""
    version, tm = model_tiers(conn)
    if version is None:
        last_tier_sync.clear()
        return None
    conn.executemany("INSERT OR IGNORE INTO wallets(address, source) VALUES (?, 'model')", [(a,) for a in tm])
    conn.execute("""UPDATE wallets SET tier=NULL, is_smart=0, tier_source='model', tier_version=?, model_score=NULL,
                        cluster_id=NULL, exit_capture=NULL""", (version,))
    conn.executemany(
        """UPDATE wallets SET tier=?, is_smart=?, model_score=?, cluster_id=?, exit_capture=? WHERE address=?""",
        [(t, int(t in ("ultra", "smart")), s, c, e, a) for a, (t, s, c, e) in tm.items()])
    conn.execute("UPDATE wallets SET is_smart=1, tier=COALESCE(tier,'smart') WHERE manual=1 AND is_bot=0")
    counts = {k: 0 for k in ("ultra", "smart", "good")}
    for t, *_ in tm.values():
        if t in counts:
            counts[t] += 1
    last_tier_sync.clear()
    last_tier_sync.update(version=version, **counts)
    return last_tier_sync


def rescore_all(conn, cfg=settings) -> int:
    positions = compute_positions(conn)
    conn.execute("DELETE FROM wallet_positions")
    conn.executemany(
        "INSERT INTO wallet_positions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(p["wallet"], p["mint"], p["buy_usd"], p["sell_usd"], p["buy_tokens"], p["sell_tokens"],
          p["realized_pnl_usd"], p["unrealized_pnl_usd"], p["roi"], p["first_buy_ts"], p["first_buy_price"],
          p["peak_multiple"], p["trades"]) for p in positions])
    by = defaultdict(list)
    for p in positions:
        by[p["wallet"]].append(p)
    manual = {r[0] for r in conn.execute("SELECT address FROM wallets WHERE manual=1")}
    now = int(time.time())
    rows = []
    for w, ps in by.items():
        q = wallet_quality(ps, cfg=cfg)
        is_bot = int(any(p["trades"] > cfg.bot_max_trades_per_token for p in ps))
        tier = tier_for(q["score"], q["n"], bool(is_bot), w in manual, cfg)
        is_smart = int(tier in ("ultra", "smart"))
        rows.append((q["score"], tier, is_smart, is_bot, q["n"], q["win_rate"], q["early_rate"],
                     sum(p["realized_pnl_usd"] for p in ps), q["total_pnl"], q["roi"], q["median_peak"],
                     q["consistency"], max(p["last_ts"] for p in ps), now, w))
    conn.executemany(
        """UPDATE wallets SET score=?, tier=?, is_smart=?, is_bot=?, tokens_traded=?, win_rate=?, early_rate=?,
               realized_pnl_usd=?, total_pnl_usd=?, roi=?, median_peak_multiple=?, consistency=?,
               last_trade_ts=?, updated_at=? WHERE address=?""", rows)
    conn.execute("UPDATE wallets SET is_smart=1, tier=COALESCE(tier,'smart') WHERE manual=1 AND is_bot=0")
    if sync_model_tiers(conn) is None:
        conn.execute("UPDATE wallets SET tier_source='heuristic', tier_version=NULL")
    conn.commit()
    return len(rows)
