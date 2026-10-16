"""mayhambot signal runner.

  python -m signals.run                 # loop forever (CYCLE_SECONDS)
  python -m signals.run --once          # one cycle
  python -m signals.run add-wallet <ADDRESS> [--label NAME]
  python -m signals.run import-wallets wallets.txt   # one address per line, optional ",label"
  python -m signals.run test-telegram
"""
import argparse
import logging
import time

from core import db, scoring, wallets
from core.config import settings
from core.sources import market, wallet_source
from signals import engine, notifier

log = logging.getLogger("mayhambot.run")


def refresh_watchlist(conn, cycle):
    if cycle % settings.discover_every_cycles == 0:
        found = market.discover_tokens(limit=60, min_liquidity=settings.min_liquidity_usd,
                                       max_mcap=settings.max_mcap_usd)
        db.upsert_tokens(conn, found, tracked=True)
        log.info("discovery: %d tokens", len(found))
    # tokens smart wallets bought in the last 2h become watched too
    hot = [r[0] for r in conn.execute(
        """SELECT DISTINCT t.mint FROM trades t JOIN wallets w ON w.address=t.wallet
           WHERE w.is_smart=1 AND t.side='buy' AND t.ts > ?""", (int(time.time()) - 7200,))]
    tracked = [r[0] for r in conn.execute("SELECT mint FROM tokens WHERE tracked=1")]
    infos = market.token_info(list(dict.fromkeys(tracked + hot)))
    db.upsert_tokens(conn, [t for t in infos if t.mint in set(hot)], tracked=True)
    db.upsert_tokens(conn, [t for t in infos if t.mint not in set(hot)], tracked=None)
    # drop dead tokens from the watchlist
    conn.execute("UPDATE tokens SET tracked=0 WHERE tracked=1 AND (liquidity_usd < ? OR last_updated < ?)",
                 (settings.min_liquidity_usd / 4, int(time.time()) - 6 * 3600))
    conn.commit()
    # priority: smart buyers right now, then recent score, then hour volume. Ranking by score alone kept the same
    # ~25 coins in the list for hours (each cycle re-scores them), so fresh smart buys never got trade fetches.
    now = int(time.time())
    return conn.execute(
        """SELECT t.* FROM tokens t
           LEFT JOIN (SELECT mint, MAX(score) s FROM signal_metrics WHERE ts > ? GROUP BY mint) m ON m.mint=t.mint
           LEFT JOIN (SELECT tr.mint, COUNT(DISTINCT tr.wallet) n FROM trades tr JOIN wallets w ON w.address=tr.wallet
                      WHERE w.tier IN ('ultra','smart') AND tr.side='buy' AND tr.ts > ? GROUP BY tr.mint) sb
                  ON sb.mint=t.mint
           WHERE t.tracked=1 AND t.pool_address != ''
           ORDER BY COALESCE(sb.n,0) DESC, COALESCE(m.s,0) DESC, t.volume_h1 DESC LIMIT ?""",
        (now - 3600, now - settings.signal_window_min * 60, settings.max_tracked_tokens)).fetchall()


def smart_active_mints(conn, now=None, limit=100):
    """Coins with an Ultra/Smart buy inside the signal window, watched or not: the signal only needs the tiered
    wallets' own trades, which wallet tracking and backfill already store."""
    now = now or int(time.time())
    return [r[0] for r in conn.execute(
        """SELECT tr.mint FROM trades tr JOIN wallets w ON w.address=tr.wallet
           WHERE w.tier IN ('ultra','smart') AND tr.side='buy' AND tr.ts > ? AND tr.ts <= ?
           GROUP BY tr.mint ORDER BY COUNT(DISTINCT tr.wallet) DESC, MAX(tr.ts) DESC LIMIT ?""",
        (now - settings.signal_window_min * 60, now, limit))]


def _row_token(r):
    from core.models import Token
    return Token(mint=r["mint"], symbol=r["symbol"] or "", pool_address=r["pool_address"] or "")


# GeckoTerminal returns the last 300 swaps of a pool. Refetch when about this many new ones can have
# happened (from DexScreener's txn counts), so quiet coins cost no calls and busy ones lose no trades.
REFETCH_AFTER_TRADES = 150
REFETCH_MAX_SECONDS = 900


def refetch_interval(r, cycle_seconds):
    """Seconds until a token's pool trades are worth fetching again."""
    keys = r.keys() if hasattr(r, "keys") else r
    m5 = (r["txns_m5"] if "txns_m5" in keys else 0) or 0
    h1 = (r["txns_h1"] if "txns_h1" in keys else 0) or 0
    per_s = max(m5 / 300, h1 / 3600)
    if per_s <= 0:
        return REFETCH_MAX_SECONDS
    return min(REFETCH_MAX_SECONDS, max(cycle_seconds, REFETCH_AFTER_TRADES / per_s))


def due_for_trades(watch, now, cycle_seconds):
    """Tokens whose trades are due, most overdue first (never-fetched tokens first of all)."""
    due = []
    for r in watch:
        last = r["last_trades_fetch"] or 0
        over = now - last - refetch_interval(r, cycle_seconds)
        if not last or over >= 0:
            due.append((-(1e12 if not last else over), r))
    return [r for _, r in sorted(due, key=lambda x: x[0])]


def fetch_trades(conn, watch):
    """Fetch pool trades of the tokens that are due (see refetch_interval), most overdue first, within
    TRADES_BUDGET_SECONDS per cycle, so a rate-limited API never blocks the cycle."""
    total, t0, done = 0, time.time(), 0
    due = due_for_trades(watch, int(t0), settings.cycle_seconds)
    if len(due) < len(watch):
        log.info("trades: %d/%d tokens due, rest quiet since last fetch", len(due), len(watch))
    for r in due:
        if time.time() - t0 > settings.trades_budget_seconds:
            log.info("trades budget used after %d/%d due tokens, rest next cycle", done, len(due))
            break
        done += 1
        conn.execute("UPDATE tokens SET last_trades_fetch=? WHERE mint=?", (int(time.time()), r["mint"]))
        conn.commit()  # never hold the write lock across the network call below
        try:
            trades = market.pool_trades(_row_token(r))
        except Exception as e:  # noqa: BLE001
            log.warning("trades %s failed: %s", r["symbol"], e)
            continue
        total += db.retry_locked(conn, lambda: db.insert_trades(conn, trades))
    return total


def backfill(conn, deadline=None):
    """History for wallets research/ does not cover yet, via the paid Helius RPC lane (public pool as fallback).
    Stops at `deadline` (time.time() value); unfinished wallets stay unmarked for the next round."""
    n = 0
    mints = set()
    from core.sources.wallet_history import SkipForNow
    for w in wallets.backfill_candidates(conn, settings.backfill_wallets_per_cycle):
        if deadline and time.time() > deadline:
            break
        try:
            trades = wallet_source.wallet_trades(w, limit=settings.backfill_tx_limit, enhanced_fallback=False,
                                                 helius_rpc=True)
        except SkipForNow:
            break  # rate limited: keep the remaining wallets for the next round, unmarked
        n += db.retry_locked(conn, lambda: db.insert_trades(conn, trades))
        mints |= {t.mint for t in trades}
        wallets.mark_backfilled(conn, w)
    known = {r[0] for r in conn.execute("SELECT mint FROM tokens")}
    new_mints = [m for m in mints if m not in known]
    if new_mints:
        db.upsert_tokens(conn, market.token_info(new_mints), tracked=False)
    return n


class Backfiller:
    """Runs backfill() in its own thread with its own DB connection, so a slow or rate-limited RPC never
    holds up the signal cycle (market data, scoring, signals). The cycle only reads the counter."""

    def __init__(self, every=60):
        import threading
        self.every, self.total, self.last_error = every, 0, None
        self._t = threading.Thread(target=self._loop, name="backfill", daemon=True)

    def start(self):
        self._t.start()
        return self

    def take(self):
        n, self.total = self.total, 0
        return n

    def _loop(self):
        conn = db.connect()
        while True:
            t0 = time.time()
            try:
                self.total += backfill(conn, deadline=t0 + 120)
            except Exception as e:  # noqa: BLE001
                self.last_error = e
                log.warning("backfill round failed: %s", e)
            time.sleep(max(5, self.every - (time.time() - t0)))


_backfiller = None


def cycle(conn, i):
    t0 = time.time()
    watch = refresh_watchlist(conn, i)
    new_trades = fetch_trades(conn, watch)
    if _backfiller is not None:  # history arrives in the background; picked up by this rescore
        bf = _backfiller.take()
    else:  # --once / tests: inline, but with a hard time cap
        bf = backfill(conn, deadline=time.time() + 20)
    db.retry_locked(conn, lambda: scoring.rescore_all(conn))
    engine.last_cluster_stats.update(wallets=0, entities=0)
    if engine.ev_mod.due(conn):
        try:
            engine.ev_mod.calibrate(conn)
        except Exception:  # noqa: BLE001 - calibration must never stop signals
            log.exception("signal EV calibration failed, fixed thresholds stay")
    mints = list(dict.fromkeys([r["mint"] for r in watch] + smart_active_mints(conn)))
    sigs = engine.evaluate(conn, mints)
    notifier.notify(conn, sigs)
    engine.update_outcomes(conn)
    smart = conn.execute("SELECT COUNT(*) FROM wallets WHERE is_smart=1").fetchone()[0]
    ts = scoring.last_tier_sync
    cs = engine.last_cluster_stats
    if ts:
        log.info("tiers synced from model v%s: ultra %d / smart %d / good %d (%d scored, %d clusters, "
                 "%d unscored kept heuristic); smart buyers this cycle: %d wallets = %d entities",
                 ts["version"], ts["ultra"], ts["smart"], ts["good"], ts["scored"], ts["clusters"], ts["heuristic"],
                 cs["wallets"], cs["entities"])
    else:
        log.info("tiers: heuristic (no active model version)")
    fu = engine.last_funnel
    if fu:
        log.info("signal funnel: %d coins -> no smart buyer %d, <%d entities %d, net <$%.0f %d, score < %s %d, "
                 "EV <= min %d, safety %d, cooldown %d, fired %d (max score %.1f, EV table %s, max EV %s)",
                 fu["coins"], fu["no_smart"], settings.signal_min_wallets, fu["few_entities"], fu["min_net"],
                 fu["low_net"], fu["min_score"], fu["low_score"], fu["low_ev"], fu["safety_drop"], fu["cooldown"],
                 fu["fired"], fu["max_score"], "active" if fu["ev_active"] else "off",
                 "-" if fu["max_ev"] is None else f"{fu['max_ev']:+.1%}")
    log.info("cycle %d: %d tokens (+%d smart-active evaluated), +%d trades, +%d backfill trades, %d smart wallets, %d signals (%.0fs)",
             i, len(watch), len(mints) - len(watch), new_trades, bf, smart, len(sigs), time.time() - t0)
    return sigs


def main():
    ap = argparse.ArgumentParser(description="mayhambot smart-money signals")
    ap.add_argument("cmd", nargs="?", default="run", choices=["run", "add-wallet", "import-wallets", "test-telegram"])
    ap.add_argument("arg", nargs="?")
    ap.add_argument("--label", default="")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    conn = db.connect()
    if a.cmd == "add-wallet":
        wallets.add_wallet(conn, a.arg, a.label)
        print("added", a.arg)
        return
    if a.cmd == "import-wallets":
        for line in open(a.arg):
            parts = [p.strip() for p in line.split(",")]
            if parts and parts[0] and not parts[0].startswith("#"):
                wallets.add_wallet(conn, parts[0], parts[1] if len(parts) > 1 else "")
        print("imported")
        return
    if a.cmd == "test-telegram":
        ns = notifier.active_notifiers()
        if len(ns) < 2:
            print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set in .env")
        for n in ns:
            n.send("mayhambot: Telegram-Test ✅")
        return
    global _backfiller
    if not a.once:
        _backfiller = Backfiller().start()
    i = 0
    while True:
        try:
            cycle(conn, i)
        except Exception:  # noqa: BLE001
            log.exception("cycle %d failed", i)
        if a.once:
            break
        i += 1
        time.sleep(max(5, settings.cycle_seconds))


if __name__ == "__main__":
    main()
