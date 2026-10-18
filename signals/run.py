"""mayhambot signal runner.

  python -m signals.run                 # loop forever (CYCLE_SECONDS)
  python -m signals.run --once          # one cycle
  python -m signals.run add-wallet <ADDRESS> [--label NAME]
  python -m signals.run import-wallets wallets.txt   # one address per line, optional ",label"
  python -m signals.run test-telegram
"""
import argparse
import logging
import os
import time

from core import db, scoring, wallets
from core.config import settings
from core.sources import market, wallet_source
from signals import accum, engine, funnel, notifier, plan, shadow

log = logging.getLogger("mayhambot.run")


def refresh_watchlist(conn, cycle):
    if cycle % settings.discover_every_cycles == 0:
        found = market.discover_tokens(limit=60, min_liquidity=settings.min_liquidity_usd,
                                       max_mcap=settings.max_mcap_usd)
        db.retry_locked(conn, lambda: db.upsert_tokens(conn, found, tracked=True))
        log.info("discovery: %d tokens", len(found))
    # tokens smart wallets bought in the last 2h become watched too
    hot = [r[0] for r in conn.execute(
        """SELECT DISTINCT t.mint FROM trades t JOIN wallets w ON w.address=t.wallet
           WHERE w.is_smart=1 AND t.side='buy' AND t.ts > ?""", (int(time.time()) - 7200,))]
    # calls of the last hours stay watched, so their prices keep coming for the target / stop follow-up
    hot += [r[0] for r in conn.execute("SELECT DISTINCT mint FROM signals WHERE ts > ?",
                                       (int(time.time()) - plan.FOLLOW_HOURS * 3600,)) if r[0] not in hot]
    tracked = [r[0] for r in conn.execute("SELECT mint FROM tokens WHERE tracked=1")]
    infos = market.token_info(list(dict.fromkeys(tracked + hot)))
    dense = dense_mints(conn, infos)
    # 18.10.: cycles 14-19 failed on "database is locked" here (TokenRefresher and research write tokens too)
    db.retry_locked(conn, lambda: db.upsert_tokens(conn, [t for t in infos if t.mint in set(hot)], tracked=True,
                                                   snap_every=SNAP_EVERY_S, dense=dense))
    db.retry_locked(conn, lambda: db.upsert_tokens(conn, [t for t in infos if t.mint not in set(hot)], tracked=None,
                                                   snap_every=SNAP_EVERY_S, dense=dense))
    # drop dead tokens from the watchlist
    db.retry_locked(conn, lambda: (conn.execute(
        "UPDATE tokens SET tracked=0 WHERE tracked=1 AND (liquidity_usd < ? OR last_updated < ?)",
        (settings.min_liquidity_usd / 4, int(time.time()) - 6 * 3600)), conn.commit()))
    # priority: smart buyers right now, then recent score, then hour volume. Ranking by score alone kept the same
    # ~25 coins in the list for hours (each cycle re-scores them), so fresh smart buys never got trade fetches.
    now = int(time.time())
    return conn.execute(
        """SELECT t.*, COALESCE(sb.n, 0) AS smart_n FROM tokens t
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
    """Seconds until a token's pool trades are worth fetching again. A coin smart wallets are buying right now is
    due every cycle: its pool trades feed the call, and every skipped cycle is latency for whoever trades by hand."""
    keys = r.keys() if hasattr(r, "keys") else r
    if "smart_n" in keys and (r["smart_n"] or 0) > 0:
        return cycle_seconds
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
            hot = "smart_n" in r.keys() and (r["smart_n"] or 0) > 0 if hasattr(r, "keys") else False
            due.append((not hot, -(1e12 if not last else over), r))
    return [r for *_, r in sorted(due, key=lambda x: x[:2])]  # smart-bought coins first, then most overdue


def fetch_trades(conn, watch, budget=None):
    """Fetch pool trades of the tokens that are due (see refetch_interval), most overdue first, within
    TRADES_BUDGET_SECONDS per cycle, so a rate-limited API never blocks the cycle."""
    total, t0, done = 0, time.time(), 0
    due = due_for_trades(watch, int(t0), settings.cycle_seconds)
    if len(due) < len(watch):
        log.info("trades: %d/%d tokens due, rest quiet since last fetch", len(due), len(watch))
    for r in due:
        if time.time() - t0 > (settings.trades_budget_seconds if budget is None else budget):
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


# Backfill may take the paid lane only while more than this share of today's Helius credits is left. 0 = always
# (18.10.: the user watches the Helius quota himself; per-second limits and 429 handling stay in core/http.py).
HELIUS_RESERVE = float(os.getenv("SIGNALS_HELIUS_RESERVE", 0))
BACKFILL_REFRESH_S = int(float(os.getenv("BACKFILL_REFRESH_HOURS", 6)) * 3600)  # a wallet's history again after


def helius_ok():
    """Backfill may use the paid Helius lane only while more than HELIUS_RESERVE of today's share is left;
    below that it reads the public RPC pool only (slower, free)."""
    from core import http
    left = http.credits_left_today()
    if left is None:
        return True
    st = http.credits(http._service) or {}
    budget = (st.get("credits_day_budget") or 0) * http.CREDIT_BURST.get(http._service, 1.0)
    ok = not budget or left > HELIUS_RESERVE * budget
    if not ok:
        log.info("backfill on the public RPC only: %.0f Helius credits left today (reserve %.0f%%)",
                 left, HELIUS_RESERVE * 100)
    return ok


def backfill(conn, deadline=None):
    """History for wallets research/ does not cover yet, via the paid Helius RPC lane (public pool as fallback).
    Stops at `deadline` (time.time() value); unfinished wallets stay unmarked for the next round."""
    n = 0
    mints = set()
    from core.sources.wallet_history import SkipForNow
    use_helius = helius_ok()
    for w in wallets.backfill_candidates(conn, settings.backfill_wallets_per_cycle,
                                          refresh_s=BACKFILL_REFRESH_S):
        if deadline and time.time() > deadline:
            break
        try:
            trades = wallet_source.wallet_trades(w, limit=settings.backfill_tx_limit, enhanced_fallback=False,
                                                 helius_rpc=use_helius)
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


# token_prices (Betrieb 18.10., all four readers agreed): a 60 s row for followed coins (call or shadow row in 4 h, open
# copytrade / brain positions) and coins under 60 min (safety's staged check), the rest at most every SNAP_EVERY_S unless
# price or liquidity moved 3 % (core.db.upsert_tokens). Was ~930k rows/day from 60 s rows of ~900 watched coins.
SNAP_EVERY_S = int(os.getenv("TOKEN_SNAPSHOT_SECONDS", 300))
YOUNG_S = 3600


def dense_mints(conn, infos, now=None):
    now = now or int(time.time())
    out = followed_mints(conn, now)
    for t in infos:
        c = t.pair_created_at or 0
        c = c / 1000 if c > now * 10 else c  # DexScreener gives ms
        if c and now - c < YOUNG_S:
            out.add(t.mint)
    return out


def followed_mints(conn, now=None):
    now = now or int(time.time())
    out = {r[0] for r in conn.execute("SELECT DISTINCT mint FROM signals WHERE ts > ?", (now - plan.FOLLOW_HOURS * 3600,))}
    try:
        out |= {r[0] for r in conn.execute("SELECT DISTINCT mint FROM signal_shadow WHERE ts > ?", (now - 4 * 3600,))}
    except Exception:  # noqa: BLE001 - table absent in old DBs
        pass
    return out | set(held_mints(conn, now))  # held: exits and labels need minutes


TOKEN_REFRESH_S = int(os.getenv("TOKEN_REFRESH_SECONDS", 60))
TOKEN_REFRESH_MAX = 900  # mints per round: 30 per DexScreener request, 30 requests of the 300/min pair limit


def held_mints(conn, now=None):
    """Coins the paper books hold or the brain bought in the last 2 h: most are new coins nobody else watches, and
    without this they got no token_prices rows at all (18.10.: >80 % of the brain's losses had no price path)."""
    now = now or int(time.time())
    out = []
    for q, args in (("SELECT DISTINCT mint FROM brain_positions WHERE status='open' OR opened_at > ?", (now - 7200,)),
                    ("SELECT DISTINCT mint FROM ct_positions WHERE status='open'", ()),
                    ("SELECT DISTINCT mint FROM opportunities WHERE decided_at > ? AND decision IN ('learn','perform')",
                     (now - 7200,))):
        try:
            out += [r[0] for r in conn.execute(q, args)]
        except Exception:  # noqa: BLE001 - table absent (service not installed / old DB)
            pass
    return list(dict.fromkeys(out))


def refresh_tokens(conn, now=None):
    """Prices, momentum and activity (price_change_m5, txns_m5, volume_h1, last_updated) of every watched coin and
    of the calls still followed, written to tokens (+ a token_prices snapshot) without touching the watch flags."""
    now = now or int(time.time())
    mints = held_mints(conn, now)  # first, so the TOKEN_REFRESH_MAX cut never drops a held or just-bought coin
    mints += [r[0] for r in conn.execute("SELECT mint FROM tokens WHERE tracked=1 ORDER BY last_updated DESC")]
    mints += [r[0] for r in conn.execute("SELECT DISTINCT mint FROM signals WHERE ts > ?",
                                         (now - plan.FOLLOW_HOURS * 3600,))]
    mints = list(dict.fromkeys(mints))[:TOKEN_REFRESH_MAX]
    infos = market.token_info(mints)
    dense = dense_mints(conn, infos, now)
    db.retry_locked(conn, lambda: db.upsert_tokens(conn, infos, tracked=False, snap_every=SNAP_EVERY_S, dense=dense))
    # a row stamped ahead of the clock (17.10.: one 52 min in the future, writer unknown) would pass every freshness
    # filter as "just updated" until then: pulled back to now, logged so the writer can be found
    ahead = conn.execute("SELECT mint, last_updated FROM tokens WHERE last_updated > ?", (now + 120,)).fetchall()
    if ahead:
        log.warning("tokens.last_updated ahead of the clock, reset to now: %s",
                    ", ".join(f"{r[0][:6]} +{(r[1] - now) // 60} min" for r in ahead[:5]))
        db.retry_locked(conn, lambda: (conn.execute("UPDATE tokens SET last_updated=? WHERE last_updated > ?",
                                                    (now, now + 120)), conn.commit()))
    return len(infos)


class TokenRefresher:
    """refresh_tokens() every TOKEN_REFRESH_S in its own thread and DB connection: the cycle (trades, scoring,
    signals) takes 2-3 min, and the brain and the shadow book read tokens as a minute-fresh market view."""

    def __init__(self, every=TOKEN_REFRESH_S):
        import threading
        self.every, self.last_n, self.last_error = every, 0, None
        self._t = threading.Thread(target=self._loop, name="token-refresh", daemon=True)

    def start(self):
        self._t.start()
        return self

    def _loop(self):
        conn = db.connect()
        while True:
            t0 = time.time()
            try:
                self.last_n = refresh_tokens(conn)
            except Exception as e:  # noqa: BLE001
                self.last_error = e
                log.warning("token refresh failed: %s", e)
            time.sleep(max(10, self.every - (time.time() - t0)))


_backfiller = None
_token_refresher = None


def cycle(conn, i):
    t0 = time.time()
    watch = refresh_watchlist(conn, i)
    # coins smart wallets are buying first, evaluated and alerted right after; the quiet rest is fetched once the
    # alerts are out (it feeds the next cycle), so the trades budget no longer sits between a smart buy and its alert
    hot = [r for r in watch if "smart_n" in r.keys() and (r["smart_n"] or 0) > 0]
    t_fetch = time.time()
    new_trades = fetch_trades(conn, hot)
    t_fetch = time.time() - t_fetch
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
    engine.last_funnel.update(alert_s=round(time.time() - t0, 1), fetch_s=round(t_fetch, 1))
    try:  # v2: smart money accumulating over hours (own type, own cooldown; same alerts and outcome rating)
        sigs2 = accum.evaluate(conn)
        notifier.notify(conn, sigs2)
        sigs = sigs + sigs2
    except Exception:  # noqa: BLE001 - v2 must never stop v1
        log.exception("signal v2 (accumulation) failed")
    hot_mints = {r["mint"] for r in hot}
    new_trades += fetch_trades(conn, [r for r in watch if r["mint"] not in hot_mints],
                               budget=max(10, settings.trades_budget_seconds - t_fetch))
    db.retry_locked(conn, lambda: engine.update_outcomes(conn))  # a lock must not cost the cycle its outcomes
    try:
        shadow.update(conn)  # what the rejected coins did since: the funnel's gates checked on data
    except Exception:  # noqa: BLE001
        log.exception("signal shadow update failed")
    try:
        notifier.notify_followups(plan.check_levels(conn) + plan.check_staged(conn))  # target / stop reached: follow-up alert for manual trading
    except Exception:  # noqa: BLE001 - follow-ups must never stop signals
        log.exception("signal follow-up check failed")
    if funnel.report_due():  # after evaluate, so last_cycle is filled; due by the file's age, not process uptime
        try:
            funnel.write_report(conn)
        except Exception:  # noqa: BLE001 - a report must never stop signals
            log.exception("signals report failed")
    db.retry_locked(conn, lambda: engine.prune_metrics(conn))
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
        mg = fu.get("model") or {}
        if mg.get("fallbacks"):
            log.warning("model fallbacks this cycle: %d of %d coins used the heuristic (%s)", mg["fallbacks"],
                        mg["calls"], "; ".join(f"{k} x{n}" for k, n in mg["top_errors"]))
    f2 = accum.last_funnel
    if f2:
        log.info("signal v2 funnel: %d coins (%d receiver wallets known) -> <%d holders %d, share %d, net <$%.0f %d, "
                 "span %d, stale %d, price %d, thin %d, safety %d, cooldown %d, fired %d (max holders %d)",
                 f2["coins"], f2["receivers"], accum.MIN_HOLDERS, f2["few_holders"], f2["low_share"],
                 f2.get("min_net", 0), f2["low_net"], f2["short_span"], f2["stale"], f2["stale_price"], f2["thin_liq"],
                 f2["safety_drop"], f2["cooldown"], f2["fired"], f2["max_holders"])
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
        engine.model_guard.start(conn)
        _backfiller = Backfiller().start()
        global _token_refresher
        _token_refresher = TokenRefresher().start()
    i = 0
    while True:
        t_cycle = time.time()
        try:
            cycle(conn, i)
        except engine.ModelCodeStale as e:  # the supervisor (mayham.py run) restarts us with fresh code
            # last line before exit 3: the supervisor / watchdog quote it as the reason of the planned restart
            log.error("planned restart (exit 3) after a model change: %s", e)
            logging.shutdown()
            raise SystemExit(3)
        except Exception:  # noqa: BLE001
            log.exception("cycle %d failed", i)
        if a.once:
            break
        i += 1
        time.sleep(max(5, settings.cycle_seconds - (time.time() - t_cycle)))  # a cycle every CYCLE_SECONDS, not a pause after it


if __name__ == "__main__":
    main()
