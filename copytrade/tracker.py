"""Which wallets we follow, and reading their swaps.

Sources of whale wallets (all merged into ct_wallets):
  manual   CT_WATCH_WALLETS in .env and copytrade/wallets.txt (one address per line)
  core     smart wallets from the signal module's registry (core.wallets), when present
  auto     big buyers on trending Solana tokens (DexScreener + GeckoTerminal)
Wallets whose history proves bad (enough samples, negative score) are deactivated.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from . import chain, market, store
from .config import Settings

log = logging.getLogger("copytrade.tracker")
WALLETS_FILE = Path(__file__).with_name("wallets.txt")


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
    """Ultra/smart wallets of the research model (model.wallet_tiers), else core's smart wallets."""
    added = 0
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


def poll(conn, s: Settings, rpc: chain.Rpc, wallet_row) -> list[chain.Swap]:
    """New swaps since the last poll (oldest first)."""
    wallet = wallet_row["address"]
    last = wallet_row["last_sig"]
    sigs = rpc.signatures(wallet, limit=15, until=last) if last else rpc.signatures(wallet, limit=1)
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
    for sg in reversed(sigs):
        if sg.get("err"):
            continue
        try:
            sw = chain.extract_swap(rpc.transaction(sg["signature"]), wallet, solusd)
        except Exception as e:
            log.debug("tx failed: %s", e)
            continue
        if sw:
            out.append(sw)
    if out:
        store.save_swaps(conn, out, solusd)
    # advance the cursor only after the swaps are stored (a locked write is retried without losing them)
    conn.execute("UPDATE ct_wallets SET last_sig=?, last_polled=? WHERE address=?",
                 (sigs[0]["signature"], int(time.time()), wallet))
    conn.commit()
    return out


def prune(conn, s: Settings) -> int:
    cur = conn.execute(
        """UPDATE ct_wallets SET active=0 WHERE source NOT IN ('manual') AND address IN
           (SELECT address FROM ct_wallet_rank WHERE samples >= ? AND score < 0)""",
        (s.min_wallet_samples * 2,),
    )
    conn.commit()
    return cur.rowcount
