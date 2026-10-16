"""Which wallets we follow, and reading their swaps.

Sources of whale wallets (all merged into ct_wallets):
  manual   CT_WATCH_WALLETS in .env and copytrade/wallets.txt (one address per line)
  core     smart wallets from the signal module's registry (core.wallets), when present
  auto     big buyers on trending Solana tokens (DexScreener + GeckoTerminal)
Wallets whose history proves bad (enough samples, negative score) are deactivated.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict, deque
from pathlib import Path

from . import chain, market, store
from .config import Settings

log = logging.getLogger("copytrade.tracker")
WALLETS_FILE = Path(__file__).with_name("wallets.txt")

# signatures already handled (websocket and polling both see the same transaction: act once)
_seen: OrderedDict[str, None] = OrderedDict()
_seen_lock = threading.Lock()  # websocket worker and polling run in different threads
DETECTED = {"ws": 0, "poll": 0, "gap": 0}  # swaps per source since the last hourly line; gap = poll found a
#                                            swap of a wallet the websocket was watching (missed notification)
WS_LATENCY: list[float] = []  # seconds from websocket notification to recognised swap (hourly mean)
# (source, side, seconds from the swap's block time to our recognition) of recent swaps, for the hourly line
LAG: deque = deque(maxlen=5000)


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


def record_detection(conn, sw: chain.Swap, source: str, notified_at: float | None = None) -> float:
    """Store how late we saw a swap (block time -> recognised), per source. Returns the lag in seconds."""
    now = time.time()
    lag = max(0.0, now - sw.block_time) if sw.block_time else float("nan")
    LAG.append((source, sw.side, lag, now))
    try:
        conn.execute("INSERT OR IGNORE INTO ct_detect VALUES (?,?,?,?,?,?,?,?)",
                     (sw.signature, sw.wallet, sw.side, source, sw.block_time, notified_at, now, sw.sol_amount))
    except Exception as e:  # metrics never block trading
        log.debug("detect log failed: %s", e)
    return lag


def lag_summary(since: float = 0.0, side: str | None = "buy") -> dict:
    """{source: {n, p50, p90, max}} of block-time -> recognition lags (seconds) since `since`."""
    out: dict = {}
    for src in ("ws", "poll", "gap"):
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
    room = max(0, s.max_tracked_wallets - len(active_wallets(conn)))
    added = store.add_wallets(conn, new[: max(room, 3)], "auto")
    if added:
        store.event(conn, "discover", f"{added} neue Wal-Wallets aus {len(hot)} heißen Coins", data=new[:added])
    return added


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
        return (r["source"] != "manual", {"ultra": 0, "smart": 1}.get(tier, 2),
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


def due_wallets(conn, s: Settings, now: float | None = None, covered: set | frozenset = frozenset()) -> list:
    """Active wallets (max CT_MAX_TRACKED_WALLETS, best ranked) whose poll interval has passed, longest waiting first.
    covered: wallets the websocket watches live; they are only polled as a safety net (warm interval)."""
    now = now or time.time()
    rows = _tracked(conn, s)

    def interval(r) -> float:
        iv = poll_interval(s, r["last_swap"], now, r["source"] == "manual")
        return max(iv, s.poll_warm_seconds) if r["address"] in covered else iv

    due = [r for r in rows if now - (r["last_polled"] or 0) >= interval(r) - 0.5]
    return sorted(due, key=lambda r: r["last_polled"] or 0)


def backfill(conn, s: Settings, rpc: chain.Rpc, wallet: str, limit: int = 80) -> int:
    """Load a wallet's recent history so it can be ranked. Flags obvious bots."""
    sigs = rpc.signatures(wallet, limit=limit)
    if len(sigs) >= limit:
        span = (sigs[0].get("blockTime") or 0) - (sigs[-1].get("blockTime") or 0)
        if 0 <= span < 15 * 60:  # >80 tx in 15 min: MEV/bot, not a whale to copy
            conn.execute("UPDATE ct_wallets SET active=0, label='bot' WHERE address=? AND source!='manual'",
                         (wallet,))
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
    conn.execute("UPDATE ct_wallets SET last_sig=COALESCE(last_sig, ?), last_polled=? WHERE address=?",
                 (newest, int(time.time()), wallet))
    conn.commit()
    return n


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
    if not sigs:
        conn.execute("UPDATE ct_wallets SET last_polled=? WHERE address=?", (now, wallet))
        return []
    if not last:  # first contact: only set the cursor, do not trade old history
        conn.execute("UPDATE ct_wallets SET last_sig=?, last_polled=? WHERE address=?",
                     (sigs[0]["signature"], now, wallet))
        conn.commit()
        return []
    solusd = market.sol_usd(s)
    out = []
    cursor = last
    for sg in reversed(sigs):  # oldest first
        if sg.get("err") or not _claim(conn, sg["signature"], wallet):
            cursor = sg["signature"]  # failed on chain, or already handled from the websocket
            continue
        try:
            tx = rpc.transaction(sg["signature"], priority="live")
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
        store.save_swaps(conn, out, solusd)
        src = "gap" if covered else "poll"
        DETECTED[src] += len(out)
        for sw in out:
            record_detection(conn, sw, src)
    # advance the cursor only after the swaps are stored (a locked write is retried without losing them)
    conn.execute("UPDATE ct_wallets SET last_sig=?, last_polled=? WHERE address=?",
                 (cursor, int(time.time()), wallet))
    conn.commit()
    return out


def process_signature(conn, s: Settings, rpc: chain.Rpc, wallet: str, sig: str, tries: int = 3,
                      received_at: float | None = None) -> chain.Swap | None:
    """A transaction the websocket reported for a watched wallet -> stored swap (or None). Public RPC first; a
    brand-new transaction can be missing there for a moment, so retry and ask the main RPC on the last try."""
    if not _claim(conn, sig, wallet):
        return None  # duplicate notification (both sockets) or already polled
    tx = None
    for i in range(tries):
        try:
            tx = rpc.read("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0,
                                                    "commitment": "confirmed"}], main_if_empty=i == tries - 1,
                          priority="live")
        except Exception as e:
            log.debug("ws tx %s failed: %s", sig[:8], e)
        if tx:
            break
        time.sleep(0.7)
    if not tx:
        _release(sig)
        return None  # the polling net picks it up later
    solusd = market.sol_usd(s)
    sw = chain.extract_swap(tx, wallet, solusd)
    if sw:
        store.save_swaps(conn, [sw], solusd)
        DETECTED["ws"] += 1
        record_detection(conn, sw, "ws", received_at)
        if received_at:
            WS_LATENCY.append(time.time() - received_at)
    return sw


def prune(conn, s: Settings) -> int:
    cur = conn.execute(
        """UPDATE ct_wallets SET active=0 WHERE source NOT IN ('manual') AND address IN
           (SELECT address FROM ct_wallet_rank WHERE samples >= ? AND score < 0)""",
        (s.min_wallet_samples * 2,),
    )
    conn.commit()
    return cur.rowcount
