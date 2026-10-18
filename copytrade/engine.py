"""Main loop: discover whales -> rank them -> watch them -> copy -> sell into the first pump."""
from __future__ import annotations

import json
import logging
import signal
import threading
import time

from . import chain, learn, opportunity, ranking, stats, store, tracker, watch, ws
from .config import Settings
from .execution import make_executors
from .strategy import Strategy

log = logging.getLogger("copytrade.engine")


class Engine:
    def __init__(self, s: Settings, stop_event: threading.Event | None = None):
        self.s = s
        self.conn = store.connect(s)
        self.rpc = chain.Rpc(s)
        self.rpc.main_first = lambda: tracker.main_budget_ok(s)
        self.exs = make_executors(s, self.rpc)
        self.strategy = Strategy(self.conn, s, self.exs)
        self._stop = stop_event or threading.Event()
        self.watcher: watch.Watcher | None = None
        self._last_usage = time.time()
        self._last_ws_sync = 0.0

    # ------------------------------------------------------------------ maintenance
    def maintain(self, discover: bool = True) -> None:
        s, c = self.s, self.conn
        tracker.load_manual(c, s)
        tracker.load_core_smart(c)
        try:
            tracker.load_user_seeds(c, s)
        except Exception as e:
            log.warning("user wallets failed: %s", e)
        try:
            tracker.load_watch(c, s)
        except Exception as e:
            log.warning("learning universe failed: %s", e)
        try:
            tracker.load_block0(c, s)
        except Exception as e:
            log.warning("block-0 wallets failed: %s", e)
        last_disc = float(store.get_state(c, "last_discover") or 0)
        if discover and (time.time() - last_disc >= s.discover_every_s or not tracker.active_wallets(c)):
            store.set_state(c, "last_discover", str(int(time.time())))
            try:
                tracker.discover(c, s, self.rpc)
            except Exception as e:
                log.warning("discover failed: %s", e)
        # backfill wallets we have no history for yet (a few per round, RPC is the bottleneck)
        for addr in tracker.backfill_due(c):
            try:
                n = tracker.backfill(c, s, self.rpc, addr)
                log.info("backfill %s… %d swaps", addr[:6], n)
            except Exception as e:
                log.warning("backfill failed: %s", e)
        try:
            n = ranking.evaluate_pending(c, s)
            k = ranking.rank_wallets(c, s)
            tracker.prune(c, s)
            log.info("ranking: %d buys evaluated, %d wallets ranked", n, k)
        except Exception as e:
            log.warning("ranking failed: %s", e)
        try:
            res = learn.run(c, s)
            log.info("learning: %s", {k: v for k, v in res.items() if k != "tuned"})
        except Exception as e:
            log.warning("learning failed: %s", e)
        if time.time() - float(store.get_state(c, "last_bookkeeping") or 0) >= stats.LEDGER_EVERY_S:
            store.set_state(c, "last_bookkeeping", str(int(time.time())))
            try:
                fix = stats.fix_ledger_once(c, s)
                if fix and fix["applied"]:
                    log.warning("ledger fix %s: balance %.4f -> %.4f SOL (%s)", fix["mode"], fix["old"], fix["new"],
                                fix["cause"])
                line = stats.bookkeeping_line(c, s)
                (log.warning if "DIFF" in line else log.info)(line)
                store.set_state(c, "bookkeeping", json.dumps({"ledger": stats.ledger(c, s), "costs": stats.costs(c)}))
            except Exception as e:
                log.warning("bookkeeping check failed: %s", e)

    # ------------------------------------------------------------------ watching
    def poll_round(self) -> int:
        """Poll the wallets that are due (hot ones every few seconds, quiet ones every 1-5 min), within the RPC budget."""
        budget = max(1, int(self.s.rpc_max_rps * self.s.wallet_poll_seconds * 0.5))
        covered = self.watcher.covered() if self.watcher else set()
        solid = self.watcher.solid() if self.watcher else set()
        batch = tracker.due_wallets(self.conn, self.s, covered=covered, solid=solid)[:budget]
        hot = set(tracker.hot_wallets(self.conn, self.s)) - covered  # no live watch: polling is the fast path
        seen = 0
        for w in batch:
            try:
                prio = "live" if w["address"] in hot else "bulk"
                for sw in store.retry_locked(tracker.poll, self.conn, self.s, self.rpc, w, prio,
                                             w["address"] in covered):
                    seen += 1
                    self._on_swap(sw)
            except Exception as e:
                log.debug("poll %s failed: %s", w["address"][:6], e)
        if self.watcher and time.time() - self._last_ws_sync > 60:
            self._last_ws_sync = time.time()
            self.watcher.set_wallets(tracker.ws_wallets(self.conn, self.s))
        return seen

    def _on_swap(self, sw) -> None:
        watch.handle_swap(self.strategy, sw)  # same rules and one decision at a time with the websocket worker

    def usage_line(self, force: bool = False) -> str | None:
        """Once an hour: swaps found by websocket vs. polling, and RPC calls public vs. Helius."""
        # every 15 min: the app restarts on each auto-update, an hourly line would rarely be written
        if not force and time.time() - self._last_usage < 900:
            return None
        u = self.rpc.usage()
        pub = sum(v for k, v in u.items() if k.startswith("public:"))
        hel = sum(v for k, v in u.items() if k.startswith("helius:"))
        lat = tracker.WS_LATENCY
        line = (f"detection last {int((time.time() - self._last_usage) / 60)} min: {tracker.DETECTED['wslog']} pump.fun swaps read from websocket logs,"
                f" {tracker.DETECTED['ws']} via websocket + transaction"
                + (f" (mean {sum(lat) / len(lat):.2f}s notification->swap)" if lat else "")
                + f", {tracker.DETECTED['poll']} via polling, {tracker.DETECTED['gap']} gaps (polling found what"
                f" the websocket missed); whale buy lag block->recognised: {watch.lag_text(self._last_usage)}"
                + f"; websockets: {self.watcher.status() if self.watcher else 'off'}"
                + f"; rpc calls public {pub}"
                + (f" ({', '.join(f'{h} {n}' for h, n in sorted(self.rpc.endpoints().items(), key=lambda x: -x[1]))})"
                   if self.rpc.endpoints() else "")
                + f", helius {hel} ({', '.join(f'{k[7:]} {v}' for k, v in sorted(u.items(), key=lambda x: -x[1]) if k.startswith('helius:'))}"
                  f"; {tracker.MAIN_READS['early']} early websocket reads, "
                  f"{self.rpc.main_first_n} reads Helius-first), fallbacks "
                  f"{self.rpc.fallbacks}, public bulk failures {self.rpc.bulk_failures}"
                + f"; opportunities to the brain {opportunity.SKIPPED['sent']} "
                  f"({opportunity.SKIPPED['stale']} older than {opportunity.STALE_S} s not sent)"
                + f"; poll backlog older than {tracker.BACKFILL_S} s kept out of the lag {sum(tracker.BACKFILL.values())}"
                  f" (websocket-covered {tracker.BACKFILL['gap']})")
        opportunity.SKIPPED.update(sent=0, stale=0)
        tracker.BACKFILL.clear()
        tracker.DETECTED.update(wslog=0, ws=0, poll=0, gap=0)
        tracker.MAIN_READS["early"] = 0
        self.rpc.fallbacks = self.rpc.bulk_failures = self.rpc.main_first_n = 0
        tracker.WS_LATENCY.clear()
        chain.LOG_STATS.clear()
        chain.LOG_VENUES.clear()
        self.rpc.counts.clear()
        if self.rpc.public:
            self.rpc.public.counts.clear()
            self.rpc.public.hosts.clear()
        self._last_usage = time.time()
        log.info(line)
        return line

    def run(self, maintain_every_s: float = 600) -> None:
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGINT, lambda *_: self._stop.set())
            signal.signal(signal.SIGTERM, lambda *_: self._stop.set())
        live = "LIVE bereit" if self.exs.get("live") else "nur Papier"
        store.event(self.conn, "start", f"Copy-Trader gestartet ({live})")
        log.info("executors=%s rpc=%s", live, self.s.rpc_url.split("?")[0])

        def entry_loop():
            # buys (dashboard commands, signals, launches) may wait up to a minute on a live swap: own thread,
            # so stop-losses in price_loop keep running meanwhile
            conn = store.connect(self.s)
            strat = Strategy(conn, self.s, self.exs)
            while not self._stop.is_set():
                try:
                    store.retry_locked(strat.process_commands)
                    store.retry_locked(follow_signals, conn, strat)
                    store.retry_locked(follow_launches, conn, strat)
                except Exception as e:
                    log.warning("entries failed: %s", e)
                self._stop.wait(self.s.price_poll_seconds)

        def price_loop():
            # exits must not wait for wallet polling or buys: own thread, own sqlite connection
            conn = store.connect(self.s)
            strat = Strategy(conn, self.s, self.exs)
            last_eq = 0.0
            while not self._stop.is_set():
                try:
                    store.retry_locked(strat.update)
                    if time.time() - last_eq > 60:
                        store.retry_locked(strat.equity_snapshot)
                        last_eq = time.time()
                except Exception as e:
                    log.warning("position update failed: %s", e)
                self._stop.wait(self.s.price_poll_seconds)

        def maint_loop():
            # discovery, backfill and ranking are slow (rate-limited APIs): keep them off the hot path
            eng = Engine.__new__(Engine)
            eng.s, eng.rpc, eng.conn = self.s, self.rpc, store.connect(self.s)
            while not self._stop.is_set():
                try:
                    store.retry_locked(eng.maintain, discover=True)
                except Exception as e:
                    log.warning("maintenance failed: %s", e)
                # no wallets yet (e.g. network was down): retry soon instead of in 10 min
                wait = maintain_every_s if tracker.active_wallets(eng.conn) else 60
                self._stop.wait(wait)

        tracker.load_manual(self.conn, self.s)
        threading.Thread(target=price_loop, name="positions", daemon=True).start()
        threading.Thread(target=entry_loop, name="entries", daemon=True).start()
        threading.Thread(target=maint_loop, name="maintenance", daemon=True).start()
        if self.s.ws_url != "off" and ws.available():
            # websocket notifications get their own thread: a slow polling round never delays a copy
            self.watcher = watch.Watcher(self.s, self.rpc, self.exs, self._stop).start(
                tracker.ws_wallets(self.conn, self.s))
            self._last_ws_sync = time.time()
        while not self._stop.is_set():
            t0 = time.time()
            try:
                self.poll_round()
                with watch.ENTRY_LOCK:
                    self.strategy.process_pending()
                self.usage_line()
            except Exception as e:
                log.warning("poll round failed: %s", e)
            self._stop.wait(max(0.5, self.s.wallet_poll_seconds - (time.time() - t0)))
        if self.watcher:
            self.watcher.stop()
        store.event(self.conn, "stop", "Copy-Trader gestoppt")


SIGNAL_TRIES = 3
_signal_tries: dict[int, int] = {}
_launch_tries: dict[str, int] = {}


def follow_signals(conn, strat: Strategy) -> int:
    """Mode 2: hand new rows of the signal engine's `signals` table to the strategy."""
    try:
        last = store.get_state(conn, "last_signal_id")
        if last is None:  # first start: do not buy old signals
            r = conn.execute("SELECT COALESCE(MAX(id), 0) FROM signals").fetchone()
            store.set_state(conn, "last_signal_id", r[0])
            return 0
        rows = conn.execute("SELECT * FROM signals WHERE id > ? ORDER BY id", (int(last),)).fetchall()
    except Exception:  # signal engine not installed / table missing
        return 0
    for sig in rows:
        # only v1 calls (smart buying now): presets, gates and the learned filters are fitted on them; a v2 call
        # (accumulation over hours, signals kind='v2') has another score scale and is not mixed in
        if (sig["kind"] if "kind" in sig.keys() else None) not in (None, "v1"):
            store.set_state(conn, "last_signal_id", sig["id"])
            continue
        try:
            strat.on_signal(sig)
        except Exception as e:  # e.g. database locked: retry next round, give up after SIGNAL_TRIES
            n = _signal_tries[sig["id"]] = _signal_tries.get(sig["id"], 0) + 1
            log.warning("signal %s failed (%d/%d): %s", sig["id"], n, SIGNAL_TRIES, e)
            if n < SIGNAL_TRIES:
                break
        _signal_tries.pop(sig["id"], None)
        store.set_state(conn, "last_signal_id", sig["id"])
    return len(rows)


def follow_launches(conn, strat: Strategy) -> int:
    """Mode 3: hand launches the sniper entered (sniper_launches.decision='enter') to the strategy."""
    from . import control

    if control.mode(conn, "launch") == "off":
        return 0
    # decided_ts is the stage-1 time; 'enter' is written at stage 2 together with the sniper's paper position,
    # so the cursor runs on the position's opened_ts (a decided_ts cursor skipped launches entered later)
    try:
        last = store.get_state(conn, "last_launch_entry_ts")
        if last is None:  # first start: do not buy old launches
            r = conn.execute("SELECT COALESCE(MAX(opened_ts), 0) FROM sniper_positions").fetchone()
            store.set_state(conn, "last_launch_entry_ts", str(r[0]))
            return 0
        rows = conn.execute("""SELECT l.*, p.opened_ts AS entered_ts FROM sniper_launches l
                               JOIN sniper_positions p ON p.mint = l.mint
                               WHERE l.decision='enter' AND p.opened_ts > ? ORDER BY p.opened_ts""",
                            (int(last),)).fetchall()
    except Exception:  # sniper/ not installed or its table missing
        return 0
    for row in rows:
        try:
            strat.on_launch(row)
        except Exception as e:  # retried (the intent row prevents a second buy), given up after SIGNAL_TRIES
            n = _launch_tries[row["mint"]] = _launch_tries.get(row["mint"], 0) + 1
            log.warning("launch %s failed (%d/%d): %s", row["mint"][:6], n, SIGNAL_TRIES, e)
            if n < SIGNAL_TRIES:
                break
        _launch_tries.pop(row["mint"], None)
        store.set_state(conn, "last_launch_entry_ts", str(row["entered_ts"]))
    return len(rows)


def start(stop_event: threading.Event | None = None, background: bool = False,
          settings: Settings | None = None) -> threading.Thread | None:
    """Entry point for the mayhambot launcher.

    Paper mode unless .env has LIVE_TRADING=true and a key. background=True runs the loop
    in a daemon thread and returns it; set stop_event to stop it cleanly.
    """
    from .config import load

    eng = Engine(settings or load(), stop_event)
    if not background:
        eng.run()
        return None
    t = threading.Thread(target=eng.run, name="copytrade", daemon=True)
    t.start()
    return t
