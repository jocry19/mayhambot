"""Wallet discovery at scale. Each run (budgeted, see research/config.py):

1. scan GeckoTerminal trending/new/top pools (price change, age, market cap)
2. coins that already pumped (>= RESEARCH_PUMPED_MULTIPLE) and are young: load their earliest swaps from
   the chain (core early_buyers), their 1-minute candles and recent pool trades
   -> wallets that bought early and cheap become candidates (priority = how much the coin rose after them)
3. a few coins that did not pump, the same way (negatives, so the statistics are not biased to winners)
4. top traders of hot coins already in the DB (largest realized profit) and the user's own wallets
5. backfill the history of the best candidates (core wallet_trades), so every wallet is judged over many coins
6. load candles for the coins those wallets traded, so their entries and outcomes can be measured
"""
import logging
import time
import zlib
from collections import Counter

from core import db
from core.models import Token
from core.sources import market
from research import credits, funding, rpc, seeds, sources
from research.config import rsettings
from research.store import add_research_wallets, connect, retry, store_candles, upsert_research_token

log = logging.getLogger("research.discovery")


SHARD = (0, 1)  # (index, count): several runners split coins and wallets by address hash


def set_shard(i, n):
    global SHARD
    SHARD = (int(i), max(1, int(n)))


def _mine(key):
    i, n = SHARD
    return n == 1 or zlib.crc32(key.encode()) % n == i


class Budget:
    def __init__(self, n):
        self.n = n

    def take(self, k=1):
        if self.n < k:
            return False
        self.n -= k
        return True


def _candles(conn, mint, pool, age_s, budget):
    """Minute candles for young coins, 15-minute candles for older ones (GeckoTerminal max 1000 per call)."""
    if not pool or not budget.take():
        return []
    agg = 1 if age_s and age_s < 16 * 3600 else 5 if age_s and age_s < 3 * 86400 else 15
    try:
        cs = market.pool_ohlcv(Token(mint=mint, pool_address=pool), "minute", aggregate=agg)
    except Exception as e:  # noqa: BLE001
        log.warning("ohlcv %s failed: %s", mint[:6], e)
        return []
    if cs:
        store_candles(conn, mint, 60 * agg, cs)
        upsert_research_token(conn, mint, candles_fetched_at=int(time.time()))
    return cs


def _early_candidates(trades, candles):
    """Early buyers whose entry was well below the later peak. -> {wallet: priority}"""
    if not candles:
        return {}
    peak = max(c[2] for c in candles)
    out = {}
    for t in trades:
        if t.side != "buy" or not t.price_usd:
            continue
        mult = peak / t.price_usd
        if mult >= 2.5:
            out[t.wallet] = max(out.get(t.wallet, 0), mult)
    return out


def _user_wallets(conn):
    ws = {r[0] for r in conn.execute("SELECT address FROM wallets WHERE manual=1")}
    try:
        from pathlib import Path
        f = Path(__file__).resolve().parent.parent / "copytrade" / "wallets.txt"
        for line in f.read_text().splitlines():
            line = line.split("#")[0].strip()
            if 32 <= len(line.split()[0] if line else "") <= 44:
                ws.add(line.split()[0])
    except OSError:
        pass
    return ws


def run_once(conn=None):
    conn = conn or connect()
    now = int(time.time())
    budget = Budget(rsettings.gt_calls_per_run)
    summary = Counter()
    pools = sources.scan_pools()
    budget.take(len(sources.POOL_LISTS))
    summary["pools_scanned"] = len(pools)
    toks = []
    for p in pools:
        upsert_research_token(conn, p["mint"], pool=p["pool"], supply=p["supply"] or None,
                              created_at=p["created_at"] or None, source="gt_scan")
        toks.append(Token(mint=p["mint"], symbol=p["name"].split("/")[0].strip(), pool_address=p["pool"],
                          price_usd=p["price"], mcap_usd=p["mcap"], liquidity_usd=p["liquidity"],
                          pair_created_at=p["created_at"], price_change_h1=p["ch_h1"], price_change_h24=p["ch_h24"]))
    retry(lambda: db.upsert_tokens(conn, toks, tracked=False))
    done = {r[0] for r in conn.execute("SELECT mint FROM research_tokens WHERE early_fetched_at > 0")}
    max_age = rsettings.max_token_age_days * 86400
    young = [p for p in pools if p["created_at"] and now - p["created_at"] <= max_age and p["mint"] not in done]
    thr = (rsettings.pumped_multiple - 1) * 100
    pumped = sorted((p for p in young if max(p["ch_h24"], p["ch_h6"]) >= thr),
                    key=lambda p: -max(p["ch_h24"], p["ch_h6"]) / (1 + (now - p["created_at"]) / 86400))
    pumped = [p for p in pumped if _mine(p["mint"])]
    calm = [p for p in young if max(p["ch_h24"], p["ch_h6"]) < 30 and p["liquidity"] >= 5000][:3]
    calm = [p for p in calm if _mine(p["mint"])]
    summary["seeded_file"] = seeds.from_files(conn)
    summary["seeded_birdeye"] = seeds.birdeye_top_traders(conn, [p["mint"] for p in pumped[:rsettings.early_tokens_per_run]])
    for p, is_pump in [(p, True) for p in pumped[:rsettings.early_tokens_per_run]] + [(p, False) for p in calm]:
        age = now - p["created_at"]
        cs = _candles(conn, p["mint"], p["pool"], age, budget)
        try:
            early = rpc.early_buyers(p["mint"], max_pages=rsettings.early_max_sig_pages,
                                               n_tx=rsettings.early_tx_per_token)
        except Exception as e:  # noqa: BLE001
            log.warning("early buyers %s failed: %s", p["mint"][:6], e)
            early = []
        time.sleep(rsettings.pause_s)
        recent = []
        if budget.take():
            try:
                recent = market.pool_trades(Token(mint=p["mint"], pool_address=p["pool"]))
            except Exception as e:  # noqa: BLE001
                log.warning("pool trades %s failed: %s", p["mint"][:6], e)
        summary["trades_added"] += retry(lambda: db.insert_trades(conn, early + recent))
        peak_mult = None
        if cs:
            first = next((c[4] for c in cs if c[4] > 0), 0)
            peak_mult = max(c[2] for c in cs) / first if first else None
        upsert_research_token(conn, p["mint"], early_fetched_at=now, researched_at=now, peak_multiple=peak_mult,
                              pumped=int(is_pump))
        cand = _early_candidates(early, cs) if is_pump else {}
        for w, pr in cand.items():
            add_research_wallets(conn, [w], "early_pumped", pr)
        summary["early_tokens" if is_pump else "control_tokens"] += 1
        summary["early_trades"] += len(early)
        summary["candidates_early"] += len(cand)
        log.info("%s %s: %d early trades, %d recent, %d candidates, peak x%.1f", "pumped" if is_pump else "control",
                 p["name"][:20], len(early), len(recent), len(cand), peak_mult or 0)
    # top traders of coins seen in the last 24 h (realized profit)
    top = conn.execute(
        """SELECT wallet, SUM(CASE WHEN side='sell' THEN usd_amount ELSE -usd_amount END) AS pnl, COUNT(DISTINCT mint) AS n
           FROM trades WHERE ts > ? GROUP BY wallet HAVING pnl > 500 AND n <= 30 ORDER BY pnl DESC LIMIT 60""",
        (now - 86400,)).fetchall()
    for r in top:
        add_research_wallets(conn, [r[0]], "top_trader", min(10.0, r[1] / 1000))
    summary["candidates_top"] = len(top)
    user = _user_wallets(conn)
    add_research_wallets(conn, user, "user", 100.0)
    # wallets signals tracks that the model has not scored yet: backfill them before new candidates
    unscored = [r[0] for r in conn.execute(
        """SELECT w.address FROM wallets w LEFT JOIN model_wallet_stats m ON m.address = w.address
           WHERE m.address IS NULL AND COALESCE(w.is_bot, 0) = 0""")]
    retry(lambda: add_research_wallets(conn, unscored, "signals_unscored", 50.0), conn=conn)
    summary["signals_unscored"] = len(unscored)
    summary["signals_unscored_pending"] = conn.execute(
        """SELECT COUNT(*) FROM research_wallets r JOIN wallets w ON w.address = r.address
           LEFT JOIN model_wallet_stats m ON m.address = r.address
           WHERE m.address IS NULL AND r.backfilled_at = 0 AND COALESCE(w.is_bot, 0) = 0""").fetchone()[0]
    # backfill histories
    bots = {r[0] for r in conn.execute("SELECT address FROM wallets WHERE is_bot=1")}
    due = [r[0] for r in conn.execute(
        """SELECT address FROM research_wallets WHERE backfilled_at < ? ORDER BY (backfilled_at = 0) DESC, priority DESC
           LIMIT ?""", (now - int(rsettings.rebackfill_hours * 3600), rsettings.wallets_per_run * 2))
        if r[0] not in bots and _mine(r[0])][:rsettings.wallets_per_run]
    new_mints = Counter()
    # wallets that matter get the Helius Enhanced API (100 credits, 100 parsed swaps); the rest go via RPC
    vip = set(user) | {r[0] for r in conn.execute("SELECT address FROM model_wallet_stats WHERE tier IS NOT NULL")}
    for w in due:
        time.sleep(rsettings.pause_s)
        tr = rpc.wallet_trades(w, enhanced=bool(rsettings.enhanced_backfill) and w in vip)
        summary["wallet_trades_added"] += retry(lambda: db.insert_trades(conn, tr))
        for t in tr:
            new_mints[t.mint] += 1
        retry(lambda: (conn.execute("UPDATE research_wallets SET backfilled_at=?, n_backfills=n_backfills+1 WHERE address=?",
                                    (now, w)), conn.commit()))
    summary["wallets_backfilled"] = len(due)
    # funding source of the best wallets (same funder = same person, counted as one group)
    try:
        summary["funders_found"] = funding.run_once(conn, mine=_mine)
    except Exception as e:  # noqa: BLE001
        log.warning("funding failed: %s", e)
    # candles for coins those wallets traded (so entries/outcomes can be measured)
    have = {r[0] for r in conn.execute("SELECT DISTINCT mint FROM research_candles")}
    recent_tried = {r[0] for r in conn.execute("SELECT mint FROM research_tokens WHERE researched_at > ?", (now - 86400,))}
    need = [m for m, _ in new_mints.most_common() if m not in have and m not in recent_tried and _mine(m)]
    pending = [r[0] for r in conn.execute(
        """SELECT t.mint FROM trades t LEFT JOIN research_candles c ON c.mint = t.mint
           JOIN research_wallets w ON w.address = t.wallet
           WHERE c.mint IS NULL GROUP BY t.mint ORDER BY COUNT(*) DESC LIMIT 200""")]
    need += [m for m in pending if m not in need and m not in recent_tried and _mine(m)]
    infos = {t.mint: t for t in market.token_info(need[:150])} if need else {}
    retry(lambda: db.upsert_tokens(conn, list(infos.values()), tracked=False))
    for m in need:
        t = infos.get(m)
        upsert_research_token(conn, m, researched_at=now, pool=t.pool_address if t else None,
                              supply=(t.mcap_usd / t.price_usd) if t and t.price_usd and t.mcap_usd else None,
                              created_at=(t.pair_created_at or None) if t else None, source="wallet_history")
        if t and t.pool_address:
            if _candles(conn, m, t.pool_address, now - (t.pair_created_at or now - 10 * 86400), budget):
                summary["coins_with_candles"] += 1
        if budget.n <= 0:
            break
    # refresh candles of coins that had activity in the last 8 h, so their 15m/1h/6h outcomes can be labelled
    last_c = dict(conn.execute("SELECT mint, MAX(ts) FROM research_candles GROUP BY mint").fetchall())
    active = [r[0] for r in conn.execute(
        """SELECT mint FROM trades WHERE ts > ? GROUP BY mint ORDER BY COUNT(DISTINCT wallet) DESC LIMIT 60""",
        (now - 8 * 3600,))]
    pools = dict(conn.execute("SELECT mint, pool FROM research_tokens WHERE pool IS NOT NULL").fetchall())
    pools.update({r[0]: r[1] for r in conn.execute("SELECT mint, pool_address FROM tokens WHERE pool_address != ''")})
    # first: coins where a scored wallet bought or a live prediction was made in the last 7 h, so their
    # 15m/1h/6h outcomes become labels (the model's sample size is limited by exactly these)
    from model.config import msettings
    fetched = dict(conn.execute("SELECT mint, candles_fetched_at FROM research_tokens WHERE candles_fetched_at > 0"))
    labelled = [r[0] for r in conn.execute(
        """SELECT t.mint FROM trades t JOIN model_wallet_stats m ON m.address = t.wallet
           WHERE t.ts > ? AND t.side = 'buy' AND (m.tier IS NOT NULL OR m.score >= ?)
           GROUP BY t.mint ORDER BY MAX(t.ts) DESC LIMIT 80""", (now - 7 * 3600, msettings.event_min_score))]
    labelled += [r[0] for r in conn.execute(
        "SELECT mint FROM model_predictions WHERE ts > ? AND ret_6h IS NULL GROUP BY mint", (now - 7 * 3600,))]
    refresh, seen = [], set()
    for m in labelled + active:
        if m in seen or m not in pools or not _mine(m):
            continue
        seen.add(m)
        if max(last_c.get(m, 0), fetched.get(m, 0)) < now - 1800:
            refresh.append(m)
    summary["label_coins_due"] = sum(1 for m in refresh if m in set(labelled))
    for m in refresh[:max(0, min(25, budget.n))]:
        created = conn.execute("SELECT pair_created_at FROM tokens WHERE mint=?", (m,)).fetchone()
        age = now - (created[0] or now - 86400) if created else 86400
        if _candles(conn, m, pools[m], min(age, 15 * 3600), budget):
            summary["candles_refreshed"] += 1
    summary["gt_calls_left"] = budget.n
    summary["helius_credits_today"] = credits.used_today()
    log.info("discovery: %s", dict(summary))
    return dict(summary)


def loop(conn=None):
    while True:
        try:
            run_once(conn)
        except Exception:  # noqa: BLE001
            log.exception("discovery run failed")
        time.sleep(rsettings.discover_every_min * 60)
