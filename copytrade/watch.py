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

from . import store, tracker, ws
from .config import Settings

log = logging.getLogger("copytrade.watch")

# whale swaps from the websocket worker and from polling go through the same Strategy rules; this lock makes
# the decisions one at a time, so two whales buying the same coin at once cannot both pass "already in the
# coin" / the per-coin cap / max open positions
ENTRY_LOCK = threading.Lock()


def handle_swap(strat, sw) -> None:
    with ENTRY_LOCK:
        if sw.side == "buy":
            strat.on_whale_buy(sw)
        else:
            strat.on_whale_sell(sw)


class Watcher:
    def __init__(self, s: Settings, rpc, exs, stop: threading.Event):
        self.s, self.rpc, self.exs, self._stop = s, rpc, exs, stop
        self.events: queue.Queue = queue.Queue()
        urls = [u for u in s.ws_urls if u != "off"]
        n = max(1, min(s.ws_parallel, len(urls))) if urls else 0
        # stream i starts on endpoint i and rotates through the rest on drops
        self.streams = [ws.WalletStream(s, url=urls[i:] + urls[:i], events=self.events, name=f"wallet-ws-{i}")
                        for i in range(n)]
        self.handled = 0
        self.fast = 0  # swaps read straight from the notification logs (no getTransaction)
        self._verify: collections.deque = collections.deque(maxlen=500)  # (due_at, tries, swap)
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ control
    def start(self, wallets) -> "Watcher":
        if not self.streams or not ws.available():
            return self
        for st in self.streams:
            st.set_wallets(wallets)
            st.start()
        self._thread = threading.Thread(target=self._work, name="wallet-ws-worker", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        for st in self.streams:
            st.stop()

    def set_wallets(self, wallets) -> None:
        for st in self.streams:
            st.set_wallets(wallets)

    def covered(self) -> set[str]:
        out: set[str] = set()
        for st in self.streams:
            out |= st.covered()
        return out

    def status(self) -> str:
        parts = [f"{st.url.split('?')[0].replace('wss://', '')} {'up' if st.connected else 'down'} "
                 f"({len(st.covered())} wallets, {st.stats['connects']} connects)" for st in self.streams]
        v = tracker.VERIFY
        return ("; ".join(parts) or "off") + (f"; {self.fast} read from logs, verified {v['checked']}"
                                              f" (corrected {v['corrected']}, rejected {v['rejected']})")

    # ------------------------------------------------------------------ worker
    def _work(self) -> None:
        from .strategy import Strategy

        conn = store.connect(self.s)
        strat = Strategy(conn, self.s, self.exs)
        while not self._stop.is_set():
            try:
                wallet, sig, got, *rest = self.events.get(timeout=0.5)
            except queue.Empty:
                self._pending(strat)
                self._verify_one(conn)
                continue
            self.handle(conn, strat, wallet, sig, got, rest[0] if rest else None)
            self._pending(strat)

    def handle(self, conn, strat, wallet: str, sig: str, got: float, logs=None):
        """One notification: pump.fun trades straight from the logs (decided within milliseconds, verified
        against the full transaction afterwards), everything else via getTransaction."""
        sw = None
        try:
            sw = store.retry_locked(tracker.process_logs, conn, self.s, wallet, sig, logs, got)
            if sw:
                self.fast += 1
                self._verify.append((time.time() + 3, 0, sw))
            else:
                sw = store.retry_locked(tracker.process_signature, conn, self.s, self.rpc, wallet, sig, 3, got)
            if sw:
                self.handled += 1
                handle_swap(strat, sw)
        except Exception as e:
            log.debug("ws swap %s failed: %s", sig[:8], e)
        return sw

    def _verify_one(self, conn) -> None:
        """When idle: check the oldest due log-read swap against its transaction (public RPC, never Helius)."""
        if not self._verify or self._verify[0][0] > time.time():
            return
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
