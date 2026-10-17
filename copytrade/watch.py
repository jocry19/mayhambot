"""Real-time watching of whale wallets: websocket streams + their own worker thread.

CT_WS_PARALLEL independent connections (each starting on a different endpoint of CT_WS_URLS) subscribe the
same wallets, so one dropped connection or a missed notification on one endpoint is no gap. Notifications
are handled by a dedicated thread with its own database connection and strategy, so a slow polling round
(rate limits, timeouts) never delays copying a fresh whale buy. Polling stays as the safety net.
"""
from __future__ import annotations

import collections
import logging
import queue
import threading
import time

from . import chain, store, tracker, ws
from .config import Settings

log = logging.getLogger("copytrade.watch")

# whale swaps from the websocket worker and from polling go through the same Strategy rules; this lock makes
# the decisions one at a time, so two whales buying the same coin at once cannot both pass "already in the
# coin" / the per-coin cap / max open positions
ENTRY_LOCK = threading.Lock()


def handle_swap(strat, sw) -> None:
    if sw.side == "buy":
        with ENTRY_LOCK:
            strat.on_whale_buy(sw)
    else:
        # exits are claimed in the database (status 'closing'), so a sell needs no process lock and never
        # blocks a parallel whale buy while its swap runs
        strat.on_whale_sell(sw)


HELIUS_WS_CREDITS_PER_100KB = 2.0  # Helius billing for standard websockets (helius.dev/docs/billing/credits, 10/2026)


def w_in(st, wallet: str) -> bool:
    with st._lock:
        return wallet in st._subs


class Watcher:
    def __init__(self, s: Settings, rpc, exs, stop: threading.Event):
        self.s, self.rpc, self.exs, self._stop = s, rpc, exs, stop
        self.events: queue.Queue = queue.Queue()
        urls = [u for u in s.ws_urls if u != "off"]
        n = max(1, min(s.ws_parallel, len(urls))) if urls else 0
        # stream i starts on endpoint i and rotates through the rest on drops
        self.streams = [ws.WalletStream(s, url=urls[i:] + urls[:i], events=self.events, name=f"wallet-ws-{i}",
                                     complement=i > 0)
                        for i in range(n)]
        # Helius stream: every watched wallet on one more connection that does not drop notifications
        self.helius = None
        if getattr(s, "ws_helius", False) and getattr(s, "helius_api_key", "") and ws.available():
            self.helius = ws.WalletStream(s, url=f"wss://mainnet.helius-rpc.com/?api-key={s.helius_api_key}",
                                          events=self.events, name="wallet-ws-helius")
            self.helius.SUBS_PER_SEC = 10.0
            self.helius.max_wallets = s.ws_helius_max
            self.streams.append(self.helius)
        self.helius_paused = ""  # why the Helius stream holds no wallets right now ("" = running)
        self.helius_credits = 0.0  # booked for streamed data since start
        self._metered = 0
        self._wallets: list[str] = []
        self.handled = 0
        self.fast = 0  # swaps read straight from the notification logs (no getTransaction)
        self._verify: collections.deque = collections.deque(maxlen=500)  # (due_at, tries, swap)
        self._thread: threading.Thread | None = None
        # transactions are read by several fetchers at once (a slow or rate-limited read no longer holds up the
        # next whale), decisions stay one at a time in the decider thread
        self.decide: queue.Queue = queue.Queue()
        self.fetchers = max(1, int(getattr(s, "ws_fetchers", 4)))
        self.waits: collections.deque = collections.deque(maxlen=2000)  # notification -> fetch start (s)
        self.fetch_s: collections.deque = collections.deque(maxlen=2000)  # transaction read (s)
        self.catchups = 0
        self.skipped_catchups = 0
        self.lean = 0  # notifications without a swap venue: read on the public pool only
        self._caught: dict[str, float] = {}
        self._kicked_at: dict[str, float] = {}
        self._last_watchdog = 0.0
        self._last_verify = 0.0
        self._last_gap_kick = 0.0

    # ------------------------------------------------------------------ control
    def start(self, wallets) -> "Watcher":
        if not self.streams or not ws.available():
            return self
        self._wallets = list(wallets)
        for st in self.streams:
            st.set_wallets([] if st is self.helius and self.helius_paused else wallets)
            st.start()
        self._thread = threading.Thread(target=self._work, name="wallet-ws-decider", daemon=True)
        self._thread.start()
        for i in range(self.fetchers):
            threading.Thread(target=self._fetch_loop, name=f"wallet-ws-fetch-{i}", daemon=True).start()
        return self

    def stop(self) -> None:
        for st in self.streams:
            st.stop()

    def set_wallets(self, wallets) -> None:
        self._wallets = list(wallets)
        for st in self.streams:
            st.set_wallets([] if st is self.helius and self.helius_paused else wallets)

    def meter(self, now: float | None = None) -> None:
        """Book the Helius stream's data (2 credits per 0.1 MB) and pause it below the day's reserve (the same one
        that guards the early transaction reads); it resumes when the budget recovers."""
        h = self.helius
        if h is None:
            return
        d, self._metered = h.bytes - self._metered, h.bytes
        why = ""
        if d > 0:
            cost = d / 1e5 * HELIUS_WS_CREDITS_PER_100KB
            self.helius_credits += cost
            try:
                from core import http as core_http  # type: ignore

                core_http._charge(cost)
            except ImportError:
                pass
            except Exception as e:  # CreditsExhausted: today's share is used up
                why = f"credits: {e}"
        if not why and not tracker.main_budget_ok(self.s, now):
            why = "Helius day budget under the reserve"
        if why and not self.helius_paused:
            self.helius_paused = why
            h.set_wallets([])
            log.info("Helius wallet stream paused: %s", why)
        elif not why and self.helius_paused:
            self.helius_paused = ""
            h.set_wallets(self._wallets)
            log.info("Helius wallet stream resumed")

    def covered(self) -> set[str]:
        out: set[str] = set()
        for st in self.streams:
            out |= st.covered()
        return out

    def status(self) -> str:
        parts = [f"{st.url.split('?')[0].replace('wss://', '')} {'up' if st.connected else 'down'} "
                 f"({len(st.covered())} wallets, {st.stats['connects']} connects, {st.stats['kicks']} watchdog)"
                 for st in self.streams]
        def pct(xs, q):
            xs = sorted(xs)
            return xs[min(len(xs) - 1, int(len(xs) * q))] if xs else 0.0

        parts.append(f"queue wait p50 {pct(self.waits, .5):.1f}s p90 {pct(self.waits, .9):.1f}s, transaction read "
                     f"p50 {pct(self.fetch_s, .5):.1f}s p90 {pct(self.fetch_s, .9):.1f}s, "
                     f"{self.catchups} catch-ups after reconnects ({self.skipped_catchups} skipped, covered elsewhere), "
                     f"{self.lean} without swap venue read public-only"
                     + (f", Helius stream {self.helius.bytes / 1e6:.1f} MB = {self.helius_credits:.0f} credits"
                        + (f" (paused: {self.helius_paused})" if self.helius_paused else "") if self.helius else ""))
        v = tracker.VERIFY
        why = ", ".join(f"{k} {n}" for k, n in chain.LOG_STATS.most_common())
        venues = ", ".join(f"{k} {n}" for k, n in chain.LOG_VENUES.most_common(6))
        return ("; ".join(parts) or "off") + (f"; {self.fast} read from logs, verified {v['checked']}"
                                              f" (corrected {v['corrected']}, rejected {v['rejected']})"
                                              + (f"; log reading: {why}" if why else "")
                                              + (f"; unread venues: {venues}" if venues else ""))

    # ------------------------------------------------------------------ workers
    def _fetch_loop(self) -> None:
        conn = store.connect(self.s)
        while not self._stop.is_set():
            try:
                wallet, sig, got, *rest = self.events.get(timeout=0.5)
            except queue.Empty:
                continue
            extra = rest[0] if rest else None
            if sig is None and extra == "catchup":
                if self.covered_elsewhere(wallet, got):
                    self.skipped_catchups += 1
                    continue  # another stream watched it all along: nothing was missed
                for sw in self.catch_up(conn, wallet):
                    self.decide.put(sw)
                continue
            sw = self.fetch(conn, wallet, sig, got, extra)
            if sw:
                self.decide.put(sw)

    def _work(self) -> None:
        from .strategy import Strategy

        conn = store.connect(self.s)
        strat = Strategy(conn, self.s, self.exs)
        while not self._stop.is_set():
            try:
                sw = self.decide.get(timeout=0.5)
            except queue.Empty:
                self._pending(strat)
                self._verify_one(conn)
                if time.time() - self._last_watchdog > 30:
                    self._last_watchdog = time.time()
                    self.watchdog()
                    self.meter()
                continue
            self._decide(strat, sw)
            self._pending(strat)

    def _decide(self, strat, sw) -> None:
        try:
            self.handled += 1
            handle_swap(strat, sw)
        except Exception as e:
            log.debug("ws decision %s failed: %s", sw.signature[:8], e)

    def fetch(self, conn, wallet: str, sig: str, got: float, logs=None):
        """One notification -> swap: pump.fun trades straight from the logs (verified against the full
        transaction afterwards), everything else via getTransaction."""
        start = time.time()
        self.waits.append(max(0.0, start - got))
        sw = None
        try:
            sw = store.retry_locked(tracker.process_logs, conn, self.s, wallet, sig, logs, got)
            if sw:
                self.fast += 1
                if sw.side == "buy":  # buys feed ranking and entries; sells keep the curve amounts (saves RPC)
                    self._verify.append((time.time() + 3, 0, sw))
            else:
                # Helius credits only for transactions that touch a swap venue (or whose logs we cannot see)
                venues = chain.log_venues(logs) if logs is not None else None
                escalate = venues is None or bool(venues)
                if not escalate:
                    self.lean += 1
                sw = store.retry_locked(tracker.process_signature, conn, self.s, self.rpc, wallet, sig, 3, got,
                                        escalate)
                self.fetch_s.append(time.time() - start)
        except Exception as e:
            log.debug("ws swap %s failed: %s", sig[:8], e)
        return sw

    def handle(self, conn, strat, wallet: str, sig: str, got: float, logs=None):
        """Fetch and decide inline (one notification, start to end)."""
        sw = self.fetch(conn, wallet, sig, got, logs)
        if sw:
            self._decide(strat, sw)
        return sw

    def covered_elsewhere(self, wallet: str, since: float) -> bool:
        """A connected stream had this wallet subscribed since before `since` (minus a margin)."""
        return any(st.connected and w_in(st, wallet) and st.sub_at.get(wallet, since) < since - 5
                   and st.connected_at < since - 5 for st in self.streams)

    def catch_up(self, conn, wallet: str, now: float | None = None) -> list:
        """A wallet's subscription came back after a drop: read what it did in the meantime right away instead
        of waiting for the safety-net poll (at most once per 2 minutes per wallet)."""
        now = now or time.time()
        if now - self._caught.get(wallet, 0) < 120:
            return []
        self._caught[wallet] = now
        try:
            row = conn.execute("SELECT * FROM ct_wallets WHERE address=?", (wallet,)).fetchone()
            if not row or not row["last_sig"]:
                return []
            out = store.retry_locked(tracker.poll, conn, self.s, self.rpc, row, "bulk", True)
        except Exception as e:
            log.debug("catch-up %s failed: %s", wallet[:6], e)
            return []
        self.catchups += 1
        return out

    def watchdog(self, now: float | None = None) -> list[str]:
        """Find sockets that are connected but no longer deliver (the node stopped sending, pings still pass) and
        reconnect them on the next endpoint. Two checks: (1) a stream that missed at least half of the recent
        transactions another stream got for wallets it had subscribed too; (2) polling found transactions all
        streams missed (gaps): the stream that was silent longest is reconnected, at most every 10 minutes."""
        now = now or time.time()
        kicked = []
        for a in self.streams:
            if not a.connected or now - a.connected_at < 120 or now - self._kicked_at.get(a.name, 0) < 600:
                continue  # a reconnect costs a resubscribe ramp: at most one per stream every 10 minutes
            mine = a.covered()
            # only transactions after a's own subscription of that wallet was confirmed: while a fresh connection
            # still subscribes (2 per second), the other stream's notifications are no miss
            cand = {sig for b in self.streams if b is not a for sig, (t, w) in list(b.recent.items())
                    if w in mine and max(a.connected_at, a.sub_at.get(w, 0)) + 5 < t < now - 20 and t > now - 600}
            missed = [sig for sig in cand if not a.saw(sig)]
            if len(cand) >= 6 and len(missed) * 5 >= len(cand) * 3:  # >= 60 % of at least 6
                a.reconnect(f"missed {len(missed)} of {len(cand)}")
                kicked.append(a.name)
        gaps = sum(1 for src, _, _, t in list(tracker.LAG) if src == "gap" and t > now - 600)
        live = [a for a in self.streams if a.connected and now - a.connected_at > 300 and a.name not in kicked]
        if gaps >= 3 and live and now - self._last_gap_kick > 600:
            a = min(live, key=lambda x: x.last_notification)
            a.reconnect(f"{gaps} gaps found by polling")
            kicked.append(a.name)
            self._last_gap_kick = now
        for name in kicked:
            self._kicked_at[name] = now
            log.info("wallet websocket %s reconnected by the watchdog", name)
        return kicked

    def _verify_one(self, conn) -> None:
        """When idle: check the oldest due log-read swap against its transaction (public RPC, never Helius)."""
        if not self._verify or self._verify[0][0] > time.time() or time.time() - self._last_verify < 2:
            return
        self._last_verify = time.time()  # at most one check every 2 s: the public pool serves live reads first
        due, tries, sw = self._verify.popleft()
        try:
            res = tracker.verify_swap(conn, self.s, self.rpc, sw)
        except Exception as e:
            log.debug("verify %s failed: %s", sw.signature[:8], e)
            res = "unread"
        if res == "unread" and tries < 4:
            self._verify.append((time.time() + 10 * (tries + 1), tries + 1, sw))

    @staticmethod
    def _pending(strat) -> None:
        try:
            with ENTRY_LOCK:
                strat.process_pending()
        except Exception as e:
            log.debug("pending entries failed: %s", e)


def lag_text(since: float) -> str:
    """'ws p50 1.2s p90 3.4s (n 40), poll ...' for whale buys since `since`."""
    sm = tracker.lag_summary(since)
    return ", ".join(f"{src} p50 {v['p50']:.1f}s p90 {v['p90']:.1f}s max {v['max']:.0f}s (n {v['n']})"
                     for src, v in sm.items()) or "no buys"
