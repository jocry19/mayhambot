"""Which wallets we follow, and reading their swaps.

Sources of whale wallets (all merged into ct_wallets):
  manual   CT_WATCH_WALLETS in .env and copytrade/wallets.txt (one address per line)
  core     smart wallets from the signal module's registry (core.wallets), when present
  auto     big buyers on trending Solana tokens (DexScreener + GeckoTerminal)
Wallets whose history proves bad (enough samples, negative score) are deactivated.
"""
from __future__ import annotations

import collections
import logging
import threading
import time
from collections import OrderedDict, deque
from pathlib import Path

from . import chain, market, opportunity, store
from .config import Settings

log = logging.getLogger("copytrade.tracker")
WALLETS_FILE = Path(__file__).with_name("wallets.txt")

# signatures already handled (websocket and polling both see the same transaction: act once)
_seen: OrderedDict[str, None] = OrderedDict()
_seen_lock = threading.Lock()  # websocket worker and polling run in different threads
_inflight: set = set()  # signatures the websocket path is reading right now (the poll must not move past them)
DETECTED = {"wslog": 0, "ws": 0, "poll": 0, "gap": 0}  # swaps per source since the last hourly line; gap = poll found a
#                                            swap of a wallet the websocket was watching (missed notification)
WS_LATENCY: list[float] = []  # seconds from websocket notification to recognised swap (hourly mean)
# (source, side, seconds from the swap's block time to our recognition) of recent swaps, for the hourly line
LAG: deque = deque(maxlen=5000)
GAPS: deque = deque(maxlen=2000)  # (found_at, wallet) of swaps polling found that the websockets missed


def _mark(sig: str) -> bool:
    """True the first time a signature is seen."""
    with _seen_lock:
        if sig in _seen:
            return False
        _seen[sig] = None
        if len(_seen) > 20000:
            _seen.popitem(last=False)
        return True


def _release(sig: str) -> None:
    """Could not read the transaction: let the other path (polling / websocket) try it again."""
    with _seen_lock:
        _seen.pop(sig, None)


def _known(conn, sig: str, wallet: str) -> bool:
    return sig in _seen or conn.execute("SELECT 1 FROM ct_swaps WHERE sig=? AND wallet=? LIMIT 1",
                                        (sig, wallet)).fetchone() is not None


def _claim(conn, sig: str, wallet: str) -> bool:
    """Atomically take a signature for handling (False: already handled or being handled elsewhere)."""
    if conn.execute("SELECT 1 FROM ct_swaps WHERE sig=? AND wallet=? LIMIT 1", (sig, wallet)).fetchone():
        return False
    return _mark(sig)


def record_detection(conn, sw: chain.Swap, source: str, notified_at: float | None = None,
                     sol_usd: float = 0.0) -> float:
    """Store how late we saw a swap (block time -> recognised), per source, with its value at that moment (USD
    amount, USD price, venue, market cap when known). Returns the lag in seconds."""
    now = time.time()
    lag = max(0.0, now - sw.block_time) if sw.block_time else float("nan")
    LAG.append((source, sw.side, lag, now))
    repeat = False
    if sw.wallet in BLOCK0:
        if sw.side == "buy":
            last_at, last_sig = _B0_LAST.get((sw.wallet, sw.mint), (0.0, ""))
            _B0_LAST[(sw.wallet, sw.mint)] = (now, sw.signature)
            # the same bot buying the same coin again (437x, 18.10.); the same signature twice is no repeat
            repeat = sw.signature != last_sig and now - last_at < B0_REPEAT_S
            if repeat:
                B0_REPEAT[sw.signature] = None
                while len(B0_REPEAT) > 5000:
                    B0_REPEAT.popitem(last=False)
        if not repeat:
            LAG.append(("block0", sw.side, lag, now))  # the group's own lag (its edge is the first seconds)
    if not repeat:
        opportunity.emit(conn, sw, source, lag, notified_at, sol_usd)  # the shared opportunity table (plan 18.10.)
    try:
        usd = sol_usd or 0.0
        conn.execute("INSERT OR IGNORE INTO ct_detect(sig, wallet, side, source, block_ts, notified_at, detected_at,"
                     " sol, usd, price_usd, dex, mcap_usd) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (sw.signature, sw.wallet, sw.side, source, sw.block_time, notified_at, now, sw.sol_amount,
                      sw.sol_amount * usd or None, sw.price_sol * usd or None, getattr(sw, "dex", "") or None,
                      getattr(sw, "mcap_sol", 0.0) * usd or None))
        conn.commit()
    except Exception as e:  # metrics never block trading
        log.debug("detect log failed: %s", e)
    return lag


def lag_summary(since: float = 0.0, side: str | None = "buy") -> dict:
    """{source: {n, p50, p90, max}} of block-time -> recognition lags (seconds) since `since`."""
    out: dict = {}
    for src in ("wslog", "ws", "poll", "gap", "block0"):
        xs = sorted(x for s_, sd, x, t in list(LAG) if s_ == src and t >= since and (side is None or sd == side)
                    and x == x)
        if xs:
            out[src] = {"n": len(xs), "p50": xs[len(xs) // 2], "p90": xs[min(len(xs) - 1, int(len(xs) * 0.9))],
                        "max": xs[-1]}
    return out


def load_manual(conn, s: Settings) -> int:
    addrs = list(s.watch_wallets)
    if WALLETS_FILE.exists():
        for line in WALLETS_FILE.read_text().splitlines():
            line = line.split("#")[0].strip()
            if line:
                addrs.append(line.split()[0])
    n = store.add_wallets(conn, addrs, "manual")
    conn.executemany("UPDATE ct_wallets SET source='manual', active=1 WHERE address=?", [(a,) for a in addrs])
    conn.commit()
    return n


B0_REPEAT_S = 1800  # a block-0 wallet's further buys of a coin within this window: no decision, no opportunity
B0_REPEAT: "collections.OrderedDict[str, None]" = collections.OrderedDict()
_B0_LAST: dict = {}
BLOCK0: set[str] = set()  # resolved block-0 wallets (detection lag is reported for them separately)
_BLOCK0_FOUND: dict[str, str] = {}  # pattern -> address (a search over all swaps runs at most hourly)
_BLOCK0_TRIED = {"at": 0.0}


def load_block0(conn, s: Settings) -> list[str]:
    """Resolve and watch the block-0 group: full addresses as given; "prefix" or "prefix…suffix" only when exactly
    one wallet the bot has seen (tracked or in any swap) matches. Unresolved ones are logged and tried again."""
    _B58 = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")
    out, missing = [], []
    for pat in s.block0_wallets:
        pre, _, suf = pat.replace("...", "…").partition("…")
        if not pre or not set(pre + suf) <= _B58:
            continue
        if not suf and len(pre) >= 32:
            out.append(pre)
            continue
        if pat in _BLOCK0_FOUND:
            out.append(_BLOCK0_FOUND[pat])
            continue
        if time.time() - _BLOCK0_TRIED["at"] < 3600:
            continue
        like = pre + "*" + suf  # GLOB: case-sensitive like base58, and it can use the wallet indexes
        hits = set()
        for q in ("SELECT address FROM ct_wallets WHERE address GLOB ?",
                  "SELECT DISTINCT wallet FROM ct_swaps WHERE wallet GLOB ?",
                  "SELECT DISTINCT wallet FROM trades WHERE wallet GLOB ?"):
            try:
                hits |= {r[0] for r in conn.execute(q, (like,)).fetchall()
                         if r[0].startswith(pre) and r[0].endswith(suf) and 32 <= len(r[0]) <= 44}
            except Exception:
                continue  # table missing in a fresh database
        if len(hits) == 1:
            _BLOCK0_FOUND[pat] = hits.pop()
            out.append(_BLOCK0_FOUND[pat])
        else:
            missing.append(f"{pat} ({len(hits)} matches)")
    if any(p not in _BLOCK0_FOUND and not (len(p) >= 32 and "…" not in p) for p in s.block0_wallets) \
            and time.time() - _BLOCK0_TRIED["at"] >= 3600:
        _BLOCK0_TRIED["at"] = time.time()
    if out:
        store.add_wallets(conn, out, "block0", label="block0")
        conn.executemany("UPDATE ct_wallets SET source='block0', active=1 WHERE address=? AND source!='manual'",
                         [(a,) for a in out])
        conn.commit()
    if missing:
        log.info("block-0 wallets not resolved yet: %s", ", ".join(missing))
    BLOCK0.clear()
    BLOCK0.update(out)
    return out


WATCH: set[str] = set()  # watched for learning only (sources watch_*): exploration copies, never strict ones


WATCH_RANDOM_SHARE = 0.2  # of the slots drawn at random from the pool: shows whether the ranking helps at all
WATCH_PER_CLUSTER = 2  # a cluster is often one operator: more of it only repeats the same signal
WATCH_ACTIVE_S = 48 * 3600


def watch_candidates(conn, n: int, now: float | None = None, seed: str | None = None) -> list[str]:
    """The learning universe's pick (wallet research, 18.10.: the score shows no forward edge, so breadth and
    independence count): from model_wallet_stats, no flag (launch_bot / flipper / wash / one_hit), >= 5 tokens, top
    coin under 70 % of its gains, active in the last 48 h (trades or ct_swaps); at most 2 per cluster; best
    median_excess_roi first (model_score breaks ties), 20 % of the slots drawn at random from the rest."""
    import random

    now = now or time.time()
    since = int(now - WATCH_ACTIVE_S)
    rows = conn.execute(
        """SELECT m.address, m.cluster_id FROM model_wallet_stats m
           WHERE m.flag IS NULL AND COALESCE(m.n_tokens, 0) >= 5 AND COALESCE(m.top_coin_share, 0) <= 0.7
             AND (EXISTS (SELECT 1 FROM trades t WHERE t.wallet=m.address AND t.ts>=?)
                  OR EXISTS (SELECT 1 FROM ct_swaps x WHERE x.wallet=m.address AND x.ts>=?))
           ORDER BY m.median_excess_roi IS NULL, m.median_excess_roi DESC, COALESCE(m.score, 0) DESC""",
        (since, since)).fetchall()
    per: dict = {}
    pool = []
    for addr, cluster in rows:
        if cluster:
            per[cluster] = per.get(cluster, 0) + 1
            if per[cluster] > WATCH_PER_CLUSTER:
                continue
        pool.append(addr)
    n_top = n - int(round(n * WATCH_RANDOM_SHARE))
    top, rest = pool[:n_top], pool[n_top:]
    rnd = random.Random(seed or time.strftime("%Y-%m-%d", time.gmtime(now)))
    return top + rnd.sample(rest, min(len(rest), n - len(top)))


def load_watch(conn, s: Settings) -> int:
    """The learning universe: research-picked wallets (watch_candidates) as source 'watch_model' within
    CT_MAX_ACTIVE_WALLETS (the research 'good' tier when the model's stats are missing); fills WATCH with every
    active watch_* wallet."""
    added = 0
    room = max(0, s.max_active_wallets - len(active_wallets(conn)))
    if room and s.watch_good_limit > 0:
        n = min(room, s.watch_good_limit)
        try:
            added = store.add_wallets(conn, watch_candidates(conn, n), "watch_model")
        except Exception as e:  # research tables or their newer columns missing
            log.debug("model wallet stats unavailable (%s): the 'good' tier instead", e)
            try:
                rows = conn.execute("SELECT address FROM wallets WHERE tier='good' ORDER BY COALESCE(model_score, 0) "
                                    "DESC LIMIT ?", (n,)).fetchall()
                added = store.add_wallets(conn, [r[0] for r in rows], "watch_good")
            except Exception as e2:  # core's wallets table missing
                log.debug("good wallets unavailable: %s", e2)
    WATCH.clear()
    WATCH.update(r[0] for r in conn.execute("SELECT address FROM ct_wallets WHERE active=1 AND source LIKE 'watch%'"))
    if added:
        log.info("learning universe: %d 'good' wallets added, %d watched only", added, len(WATCH))
    return added


def load_core_smart(conn, limit: int = 30) -> int:
    """Ultra/smart wallets: core's `wallets.tier` (the one truth, synced from the research model), else
    model.wallet_tiers, else core's smart wallets."""
    added = 0
    try:
        for tier in ("ultra", "smart"):
            rows = conn.execute("SELECT address FROM wallets WHERE tier=? ORDER BY COALESCE(model_score, 0) DESC "
                                "LIMIT ?", (tier, limit)).fetchall()
            added += store.add_wallets(conn, [r[0] for r in rows], f"model_{tier}")
        if added or conn.execute("SELECT 1 FROM wallets WHERE tier IS NOT NULL LIMIT 1").fetchone():
            return added
    except Exception as e:
        log.debug("wallets.tier unavailable: %s", e)
    try:
        from model import wallet_tiers  # type: ignore

        for tier in ("ultra", "smart"):
            rows = wallet_tiers(conn=conn, tier=tier, limit=limit)
            added += store.add_wallets(conn, [r["address"] for r in rows], f"model_{tier}")
        return added
    except Exception as e:
        log.debug("model tiers unavailable: %s", e)
    try:
        from core import wallets as core_wallets  # type: ignore

        rows = core_wallets.smart_wallets(conn)[:limit]
        return store.add_wallets(conn, [r["address"] for r in rows], "core_smart")
    except Exception as e:
        log.debug("core smart wallets unavailable: %s", e)
        return 0


def discover(conn, s: Settings, rpc: chain.Rpc, tokens: int = 6, txs_per_token: int = 60) -> int:
    """Find wallets that bought big on currently hot tokens."""
    mints = market.trending_solana_tokens(limit=30)
    snaps = market.snapshots(mints, s)
    hot = sorted(
        (sn for sn in snaps.values() if sn.liquidity_usd >= s.min_liquidity_usd and sn.age_hours <= 7 * 24),
        key=lambda sn: sn.volume_h1, reverse=True,
    )[:tokens]
    solusd = market.sol_usd(s)
    found: dict[str, float] = {}
    for sn in hot:
        try:
            buys = chain.pool_buyers(rpc, sn.pair_address or sn.mint, limit=txs_per_token, sol_usd=solusd)
        except Exception as e:
            log.warning("discover %s failed: %s", sn.symbol, e)
            continue
        store.save_swaps(conn, buys, solusd)
        for b in buys:
            if b.sol_amount >= s.min_whale_buy_sol:
                found[b.wallet] = max(found.get(b.wallet, 0), b.sol_amount)
    known = {r[0] for r in conn.execute("SELECT address FROM ct_wallets")}
    new = [w for w, _ in sorted(found.items(), key=lambda kv: -kv[1]) if w not in known]
    n_active = len(active_wallets(conn))
    room = max(0, s.max_tracked_wallets - n_active)
    take = max(room, 3)
    added = store.add_wallets(conn, new[:take], "auto")
    # the rest are watched for learning (more different buys and labels for the brain), not copied strictly
    rest = new[take:][: max(0, s.max_active_wallets - n_active - added)]
    watched = store.add_wallets(conn, rest, "watch") if rest else 0
    WATCH.update(rest[:watched])
    if added or watched:
        store.event(conn, "discover", f"{added} neue Wal-Wallets, {watched} nur beobachtet, aus {len(hot)} heißen Coins",
                    data=new[:added + watched])
    return added + watched


def active_wallets(conn) -> list:
    """Active wallets, best ranked first, unranked after."""
    return conn.execute(
        """SELECT w.*, r.score, r.samples FROM ct_wallets w LEFT JOIN ct_wallet_rank r ON r.address = w.address
           WHERE w.active = 1 ORDER BY (w.source='manual') DESC, COALESCE(r.score, -0.001) DESC"""
    ).fetchall()


def poll_interval(s: Settings, last_swap: int | None, now: float, manual: bool = False) -> float:
    """Seconds between polls of one wallet: hot wallets fast, quiet ones slow (saves RPC credits)."""
    if manual or (last_swap and now - last_swap < 6 * 3600):
        return s.wallet_poll_seconds
    if last_swap and now - last_swap < 48 * 3600:
        return s.poll_warm_seconds
    return s.poll_cold_seconds


def ws_wallets(conn, s: Settings) -> list[str]:
    """Wallets for real-time websocket watching: every active wallet, manual and Ultra/Smart first, then the
    whale ranking, then the most recently active; at most CT_WS_MAX_WALLETS."""
    rows = conn.execute(
        """SELECT w.address, w.source, r.score, (SELECT MAX(ts) FROM ct_swaps x WHERE x.wallet=w.address) AS last_swap
           FROM ct_wallets w LEFT JOIN ct_wallet_rank r ON r.address = w.address WHERE w.active = 1""").fetchall()

    def key(r):
        tier = store.wallet_info(conn, r["address"]).get("tier")
        return (r["source"] != "manual", 1 if r["source"] == "block0" else {"ultra": 0, "smart": 1}.get(tier, 2),
                -(r["score"] if r["score"] is not None else -1), -(r["last_swap"] or 0))

    return [r["address"] for r in sorted(rows, key=key)][: s.ws_max_wallets]


def hot_wallets(conn, s: Settings, now: float | None = None) -> list[str]:
    """Wallets polled at the fast rate (recent swaps, manual): these get a websocket subscription."""
    now = now or time.time()
    return [r["address"] for r in _tracked(conn, s)
            if poll_interval(s, r["last_swap"], now, r["source"] == "manual") <= s.wallet_poll_seconds]


def _tracked(conn, s: Settings) -> list:
    return conn.execute(
        """SELECT w.*, r.score, r.samples, (SELECT MAX(ts) FROM ct_swaps x WHERE x.wallet=w.address) AS last_swap
           FROM ct_wallets w LEFT JOIN ct_wallet_rank r ON r.address = w.address
           WHERE w.active = 1 ORDER BY (w.source='manual') DESC, COALESCE(r.score, -0.001) DESC LIMIT ?""",
        (s.max_tracked_wallets,)).fetchall()


def due_wallets(conn, s: Settings, now: float | None = None, covered: set | frozenset = frozenset(),
                solid: set | frozenset = frozenset()) -> list:
    """Active wallets (max CT_MAX_TRACKED_WALLETS, best ranked) whose poll interval has passed, longest waiting first.
    covered: wallets the websocket watches live; they are only polled as a safety net (warm interval).
    solid: wallets on the Helius stream, which does not drop notifications: safety net every CT_POLL_SAFE_SECONDS."""
    now = now or time.time()
    rows = _tracked(conn, s)

    def interval(r) -> float:
        iv = poll_interval(s, r["last_swap"], now, r["source"] == "manual")
        if r["address"] in solid:
            return max(iv, getattr(s, "poll_safe_seconds", s.poll_warm_seconds))
        return max(iv, s.poll_warm_seconds) if r["address"] in covered else iv

    due = [r for r in rows if now - (r["last_polled"] or 0) >= interval(r) - 0.5]
    return sorted(due, key=lambda r: r["last_polled"] or 0)


def backfill(conn, s: Settings, rpc: chain.Rpc, wallet: str, limit: int = 80) -> int:
    """Load a wallet's recent history so it can be ranked. Flags obvious bots."""
    try:
        sigs = rpc.signatures(wallet, limit=limit)
    except Exception:
        conn.execute("UPDATE ct_wallets SET backfill_tries=COALESCE(backfill_tries, 0)+1 WHERE address=?", (wallet,))
        conn.commit()
        raise
    if len(sigs) >= limit:
        span = (sigs[0].get("blockTime") or 0) - (sigs[-1].get("blockTime") or 0)
        if 0 <= span < 15 * 60:  # >80 tx in 15 min: MEV/bot, not a whale to copy
            conn.execute("UPDATE ct_wallets SET active=0, label='bot' WHERE address=? AND source NOT IN "
                         "('manual','block0')", (wallet,))
            conn.execute("UPDATE ct_wallets SET backfilled_at=? WHERE address=?", (int(time.time()), wallet))
            conn.commit()
            return 0
    solusd = market.sol_usd(s)
    swaps = []
    for sg in sigs:
        if sg.get("err"):
            continue
        try:
            sw = chain.extract_swap(rpc.transaction(sg["signature"]), wallet, solusd)
        except Exception:
            continue
        if sw:
            swaps.append(sw)
    n = store.save_swaps(conn, swaps, solusd)
    newest = sigs[0]["signature"] if sigs else None
    now = int(time.time())
    conn.execute("UPDATE ct_wallets SET last_sig=COALESCE(last_sig, ?), last_polled=?, backfilled_at=? WHERE address=?",
                 (newest, now, now, wallet))
    conn.commit()
    return n


def backfill_due(conn, limit: int = 4) -> list[str]:
    """Wallets whose history was never loaded: manual first, then Ultra/Smart tier, then the newest finds.
    Wallets that failed 3 times are skipped, so a dead address never blocks the slots."""
    try:
        rows = conn.execute(
            """SELECT w.address FROM ct_wallets w LEFT JOIN wallets m ON m.address=w.address
               WHERE w.active=1 AND COALESCE(w.backfilled_at, 0)=0 AND COALESCE(w.backfill_tries, 0)<3
               ORDER BY (w.source='manual') DESC,
                        CASE LOWER(COALESCE(m.tier, '')) WHEN 'ultra' THEN 0 WHEN 'smart' THEN 1 ELSE 2 END,
                        w.added_at DESC LIMIT ?""", (limit,)).fetchall()
    except Exception:  # core's wallets table missing
        rows = conn.execute(
            """SELECT address FROM ct_wallets WHERE active=1 AND COALESCE(backfilled_at, 0)=0
               AND COALESCE(backfill_tries, 0)<3 ORDER BY (source='manual') DESC, added_at DESC LIMIT ?""",
            (limit,)).fetchall()
    return [r[0] for r in rows]


def poll(conn, s: Settings, rpc: chain.Rpc, wallet_row, priority: str = "bulk",
         covered: bool = False) -> list[chain.Swap]:
    """New swaps since the last poll (oldest first). priority 'live' for wallets nobody watches in real time;
    the transactions of new finds are always read in the live lane (a fresh whale buy may follow).
    covered: the websocket watches this wallet, so a find here is a gap (missed notification)."""
    wallet = wallet_row["address"]
    last = wallet_row["last_sig"]
    sigs = rpc.poll_signatures(wallet, limit=15, until=last, priority=priority) if last \
        else rpc.poll_signatures(wallet, limit=1, priority=priority)
    now = int(time.time())
    if _burst_bot(conn, wallet_row, sigs):
        return []
    if not sigs:
        conn.execute("UPDATE ct_wallets SET last_polled=? WHERE address=?", (now, wallet))
        conn.commit()  # no-op in autocommit, but never leave a write lock open across what follows
        return []
    if not last:  # first contact: only set the cursor, do not trade old history
        conn.execute("UPDATE ct_wallets SET last_sig=?, last_polled=? WHERE address=?",
                     (sigs[0]["signature"], now, wallet))
        conn.commit()
        return []
    solusd = market.sol_usd(s)
    out = []
    claimed = []
    cursor = last
    for sg in reversed(sigs):  # oldest first
        if not sg.get("err") and sg["signature"] in _inflight:
            break  # the websocket is still reading it; if that fails it is released and this poll's cursor must
            #        still be before it, so the next poll reads it
        if sg.get("err") or not _claim(conn, sg["signature"], wallet):
            cursor = sg["signature"]  # failed on chain, or already handled from the websocket
            continue
        claimed.append(sg["signature"])
        try:
            # gap finds of websocket-covered wallets are old already: public pool only, no main RPC credits
            tx = rpc.transaction(sg["signature"], priority="bulk" if covered else "live")
            if tx is None:
                raise RuntimeError("transaction not available yet")
        except Exception as e:
            _release(sg["signature"])
            log.debug("tx failed, retry next poll: %s", e)
            break  # the cursor stays before it: the next poll reads it again, nothing is skipped
        cursor = sg["signature"]
        sw = chain.extract_swap(tx, wallet, solusd)
        if sw:
            out.append(sw)
    if out:
        try:
            store.save_swaps(conn, out, solusd)
        except Exception:
            # nothing stored, cursor not moved: hand every claimed signature back, or the retry (retry_locked)
            # would take them as handled and skip them, and a whale sell would never reach the strategy
            for sig in claimed:
                _release(sig)
            raise
        src = "gap" if covered else "poll"
        DETECTED[src] += len(out)
        if covered:
            GAPS.extend((time.time(), sw.wallet) for sw in out)
        for sw in out:
            try:
                record_detection(conn, sw, src, sol_usd=solusd)
            except Exception as e:
                log.debug("detection row %s failed: %s", sw.signature[:8], e)
    # advance the cursor only after the swaps are stored. From here on the swaps are saved, so they are returned
    # even if this write fails: the next poll re-reads those signatures and skips them as stored
    try:
        conn.execute("UPDATE ct_wallets SET last_sig=?, last_polled=? WHERE address=?",
                     (cursor, int(time.time()), wallet))
        conn.commit()
    except Exception as e:
        log.debug("cursor %s not moved: %s", wallet[:6], e)
    return out


BURST_TX, BURST_SPAN_S = 60, 11 * 60  # the backfill's bot rate (80 tx in 15 min) seen by one poll
BOTS_OFF: list[str] = []


def _burst_bot(conn, wallet_row, sigs) -> bool:
    """A poll that finds 60+ new transactions within 11 minutes is a bot's pace, not a whale's: such a wallet
    cost up to 60 transaction reads per poll (most of the public 429s, 18.10.). It is switched off as 'bot' like
    the backfill does; manual and block-0 wallets are kept."""
    src = wallet_row["source"] if "source" in wallet_row.keys() else None
    if len(sigs) < BURST_TX or src in ("manual", "block0") or wallet_row["address"] in BLOCK0:
        return False
    times = [sg.get("blockTime") for sg in sigs if sg.get("blockTime")]
    if len(times) < BURST_TX or max(times) - min(times) > BURST_SPAN_S:
        return False
    wallet = wallet_row["address"]
    conn.execute("UPDATE ct_wallets SET active=0, label='bot', last_sig=?, last_polled=? WHERE address=?",
                 (sigs[0]["signature"], int(time.time()), wallet))
    conn.commit()
    BOTS_OFF.append(wallet)
    log.info("wallet %s off as bot: %d transactions in %d s", wallet[:6], len(times), max(times) - min(times))
    try:
        store.event(conn, "bot", f"Wallet {wallet[:6]}… abgeschaltet: {len(times)} Transaktionen in "
                    f"{(max(times) - min(times)) / 60:.0f} min (Bot-Tempo)", data={"wallet": wallet})
    except Exception as e:
        log.debug("bot event failed: %s", e)
    return True


def _key_wallet(conn, wallet: str) -> bool:
    """Manual wallets and Ultra/Smart tiers: their fresh buys may spend main RPC credits."""
    try:
        r = conn.execute("SELECT source FROM ct_wallets WHERE address=?", (wallet,)).fetchone()
        if r and r[0] == "manual":
            return True
    except Exception:
        pass
    return str(store.wallet_info(conn, wallet).get("tier") or "").lower() in ("ultra", "smart")


def process_signature(conn, s: Settings, rpc: chain.Rpc, wallet: str, sig: str, tries: int = 3,
                      received_at: float | None = None,
                      escalate: bool = True) -> chain.Swap | None:
    """A transaction the websocket reported for a watched wallet -> stored swap (or None). Public RPC first; a
    brand-new transaction can be missing there for a moment, so retry and ask the main RPC on the last try."""
    solusd = market.sol_usd(s)  # before the claim: an error here must not leave the signature claimed
    if not _claim(conn, sig, wallet):
        return None  # duplicate notification (both sockets) or already polled
    _inflight.add(sig)
    try:
        sw = _read_and_store(conn, rpc, wallet, sig, tries, received_at, solusd, s, escalate)
    except Exception:
        _release(sig)  # e.g. database locked: retry_locked calls again, or the polling net picks it up
        raise
    finally:
        _inflight.discard(sig)
    return sw


_budget_cache: dict = {"at": 0.0, "ok": False}
MAIN_READS = {"early": 0}  # websocket reads sent to the main RPC after one failed public try (budget rich)


def main_budget_ok(s: Settings | None, now: float | None = None) -> bool:
    """More than CT_WS_MAIN_RESERVE_PCT of today's Helius budget left (checked at most every 30 s)."""
    now = now or time.time()
    if now - _budget_cache["at"] < 30:
        return _budget_cache["ok"]
    ok = False
    try:
        from core import http as core_http  # type: ignore

        st = core_http.credits("copytrade") or {}
        budget, left = st.get("credits_day_budget"), core_http.credits_left_today("copytrade")
        pct = getattr(s, "ws_main_reserve_pct", 0.0) if s is not None else 0.0
        # no reserve (the user keeps an eye on the key's quota, 18.10.): Helius until the ledger says it is spent
        ok = (left is None or left > 0) if pct <= 0 else bool(budget) and left is not None and left > budget * pct / 100
    except Exception as e:
        log.debug("credit check failed: %s", e)
    _budget_cache.update(at=now, ok=ok)
    return ok


def _read_and_store(conn, rpc, wallet, sig, tries, received_at, solusd, s=None, escalate=True):
    tx = None
    # main RPC credits: while the Helius budget allows, every swap-venue transaction goes there first (the public
    # pool answers 429 for minutes at times, and a late whale buy is worth less than a credit); otherwise
    # only for wallets worth them, and only on the last try. Anyone else is picked up by the polling net later
    key = _key_wallet(conn, wallet)
    params = [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0, "commitment": "confirmed"}]
    for i in range(tries):
        if i == 0 and escalate and hasattr(rpc, "call") and main_budget_ok(s):
            try:
                tx = rpc.call("getTransaction", params, "live")
                MAIN_READS["early"] += 1
            except Exception as e:
                log.debug("ws tx %s on main failed: %s", sig[:8], e)
            if tx:
                break
        last_try = key and escalate and i == tries - 1
        try:
            tx = rpc.read("getTransaction", params, main_if_empty=last_try, priority="live", main_on_error=last_try)
        except Exception as e:
            log.debug("ws tx %s failed: %s", sig[:8], e)
        if tx:
            break
        time.sleep(0.7)
    if not tx:
        _release(sig)
        return None  # the polling net picks it up later
    sw = chain.extract_swap(tx, wallet, solusd)
    if sw:
        store.save_swaps(conn, [sw], solusd)
        DETECTED["ws"] += 1
        record_detection(conn, sw, "ws", received_at, solusd)
        if received_at:
            WS_LATENCY.append(time.time() - received_at)
    return sw


VERIFY: dict = {"checked": 0, "corrected": 0, "rejected": 0}  # log-read swaps checked against the full tx


def process_logs(conn, s: Settings, wallet: str, sig: str, logs, received_at: float | None = None) -> chain.Swap | None:
    """Fast path: a pump.fun trade read from the notification's logs (no RPC call). None when the logs are not
    a clear pump.fun trade of this wallet; then process_signature reads the transaction as before."""
    sw = chain.swap_from_logs(logs, wallet, sig)
    if not sw:
        return None
    solusd = market.sol_usd(s)  # before the claim: an error here must not leave the signature claimed
    if not _claim(conn, sig, wallet):
        return None
    try:
        store.save_swaps(conn, [sw], solusd)
    except Exception:
        _release(sig)  # database locked: the retry (retry_locked) must be able to claim it again
        raise
    DETECTED["wslog"] += 1
    record_detection(conn, sw, "wslog", received_at, solusd)
    if received_at:
        WS_LATENCY.append(time.time() - received_at)
    return sw


def verify_swap(conn, s: Settings, rpc: chain.Rpc, sw: chain.Swap) -> str:
    """Re-read a log-read swap from the full transaction (public RPC, bulk lane) and store the exact amounts
    (fees included). 'ok' | 'corrected' | 'rejected' | 'unread' (try again later)."""
    try:
        tx = rpc.read("getTransaction", [sw.signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0,
                                                        "commitment": "confirmed"}], priority="bulk")
    except Exception:
        return "unread"
    if not tx:
        return "unread"
    VERIFY["checked"] += 1
    full = chain.extract_swap(tx, sw.wallet, market.sol_usd(s))
    if not full or full.mint != sw.mint or full.side != sw.side:
        VERIFY["rejected"] += 1
        log.info("log-read swap %s did not hold up against the transaction", sw.signature[:8])
        store.event(conn, "verify", f"Swap aus Logs nicht bestätigt: {sw.signature[:8]}…", "warn",
                    {"sig": sw.signature, "wallet": sw.wallet, "mint": sw.mint})
        # a swap that never happened must not feed ranking, learning, the brain or the signals
        for sql in ("DELETE FROM ct_swaps WHERE sig=? AND wallet=? AND mint=?",
                    "DELETE FROM trades WHERE sig=? AND wallet=? AND mint=? AND source='copytrade'"):
            try:
                conn.execute(sql, (sw.signature, sw.wallet, sw.mint))
            except Exception as e:  # noqa: BLE001 - a table missing in a stripped-down setup
                log.debug("cleanup of rejected swap failed: %s", e)
        conn.commit()
        return "rejected"
    conn.execute("UPDATE ct_swaps SET token_amount=?, sol_amount=?, price_sol=?, ts=COALESCE(NULLIF(?, 0), ts) "
                 "WHERE sig=? AND wallet=? AND mint=?", (full.token_amount, full.sol_amount, full.price_sol,
                                                        full.block_time, sw.signature, sw.wallet, sw.mint))
    conn.commit()
    if abs(full.sol_amount - sw.sol_amount) > 0.05 * max(sw.sol_amount, 1e-9):
        VERIFY["corrected"] += 1
        return "corrected"
    return "ok"


DETECT_KEEP_DAYS = 14


def prune(conn, s: Settings) -> int:
    conn.execute("DELETE FROM ct_detect WHERE detected_at < ?", (time.time() - DETECT_KEEP_DAYS * 86400,))
    cur = conn.execute(
        """UPDATE ct_wallets SET active=0 WHERE source NOT IN ('manual','block0') AND address IN
           (SELECT address FROM ct_wallet_rank WHERE samples >= ? AND score < 0)""",
        (s.min_wallet_samples * 2,),
    )
    conn.commit()
    return cur.rowcount
