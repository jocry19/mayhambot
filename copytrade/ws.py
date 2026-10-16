"""Real-time wallet activity over the Solana websocket (logsSubscribe, one subscription per wallet).

Free public endpoint by default (CT_WS_URL, "off" disables), so detecting a whale's transaction costs no
Helius credits and arrives within about a second instead of at the next poll. Notifications only carry the
signature; the engine reads the transaction (public RPC first) and treats it like a polled swap. Polling stays
on as the safety net for anything the socket misses (disconnects, dropped notifications).
"""
from __future__ import annotations

import asyncio
import collections
import json
import logging
import queue
import threading
import time

from .config import Settings

log = logging.getLogger("copytrade.ws")

try:
    import websockets  # type: ignore
except Exception:  # optional dependency: without it the copy trader only polls
    websockets = None


def available() -> bool:
    return websockets is not None


class WalletStream:
    """Background thread keeping one websocket with a logsSubscribe per wanted wallet.

    events: queue of (wallet, signature, received_at) for successful transactions mentioning the wallet.
    """

    SUBSCRIBE_BATCH = 25  # new subscriptions per 5-s sync (no burst of 150 requests on a fresh connection)

    def __init__(self, s: Settings, url: str | list[str] | None = None, events: queue.Queue | None = None,
                 name: str = "wallet-ws"):
        urls = [url] if isinstance(url, str) else (url or [u for u in s.ws_urls if u != "off"])
        self.urls = urls
        self._idx = 0
        self.name = name
        self.max_wallets = s.ws_max_wallets
        self.events: queue.Queue = events if events is not None else queue.Queue()
        self.connected = False
        self.stats = {"notifications": 0, "connects": 0, "errors": 0, "kicks": 0}
        self.connected_at = 0.0
        self.last_notification = 0.0
        # signature -> (received_at, wallet) of recent notifications, so the watcher can compare streams
        self.recent: "collections.OrderedDict[str, tuple[float, str]]" = collections.OrderedDict()
        self._kick: str | None = None
        self._wanted: set[str] = set()
        self._subs: dict[str, int] = {}  # wallet -> subscription id
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ API for the engine
    def set_wallets(self, wallets) -> None:
        with self._lock:
            self._wanted = set(list(wallets)[: self.max_wallets])

    def covered(self) -> set[str]:
        """Wallets with a confirmed subscription on a live connection (their polling can slow down)."""
        if not self.connected:
            return set()
        with self._lock:
            return set(self._subs)

    def start(self) -> "WalletStream":
        if websockets is None:
            log.warning("websockets not installed: wallet detection by polling only")
            return self
        self._thread = threading.Thread(target=lambda: asyncio.run(self._main()), name=self.name, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def reconnect(self, reason: str) -> None:
        """Drop the connection and move on to the next endpoint (a silent socket looks alive to pings)."""
        if self.connected and not self._kick:
            self._kick = reason
            self.stats["kicks"] += 1

    def saw(self, sig: str) -> bool:
        return sig in self.recent

    # ------------------------------------------------------------------ internals
    @property
    def url(self) -> str:
        """Endpoint in use (or tried next)."""
        return self.urls[self._idx % len(self.urls)]

    async def _main(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            url = self.url
            self._kick = None
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=20, max_size=2 ** 22,
                                              open_timeout=15) as sock:
                    self.connected, self.connected_at = True, time.time()
                    self.stats["connects"] += 1
                    backoff = 1.0
                    log.info("wallet websocket connected (%s)", url.split("?")[0])
                    await self._session(sock)
            except Exception as e:
                self.stats["errors"] += 1
                log.info("wallet websocket %s dropped: %s", url.split("?")[0], str(e)[:120])
            finally:
                self.connected = False
                with self._lock:
                    self._subs.clear()
            # next endpoint right away; after a full round through the list wait with growing backoff
            self._idx = (self._idx + 1) % len(self.urls)
            wait = backoff if self._idx == 0 else 0.5
            if self._idx == 0:
                backoff = min(backoff * 2, 60.0)
            if self._stop.wait(wait):
                break

    async def _session(self, sock) -> None:
        req_id = 0
        pending: dict[int, tuple[str, str]] = {}  # request id -> (op, wallet)
        sub_wallet: dict[int, str] = {}
        last_sync = 0.0
        while not self._stop.is_set():
            if self._kick:
                raise ConnectionError(f"stale stream: {self._kick}")
            if time.time() - last_sync > 5:
                last_sync = time.time()
                with self._lock:
                    want, have = set(self._wanted), dict(self._subs)
                busy = {w for _, w in pending.values()}
                for w in sorted(want - set(have) - busy)[: self.SUBSCRIBE_BATCH]:
                    req_id += 1
                    pending[req_id] = ("sub", w)
                    await sock.send(json.dumps({"jsonrpc": "2.0", "id": req_id, "method": "logsSubscribe",
                                                "params": [{"mentions": [w]}, {"commitment": "confirmed"}]}))
                for w in sorted(set(have) - want - busy):
                    req_id += 1
                    pending[req_id] = ("unsub", w)
                    await sock.send(json.dumps({"jsonrpc": "2.0", "id": req_id, "method": "logsUnsubscribe",
                                                "params": [have[w]]}))
            try:
                raw = await asyncio.wait_for(sock.recv(), timeout=5)
            except asyncio.TimeoutError:
                continue
            self._handle(json.loads(raw), pending, sub_wallet)

    def _handle(self, msg: dict, pending: dict, sub_wallet: dict) -> None:
        if "id" in msg and msg["id"] in pending:
            op, w = pending.pop(msg["id"])
            if "error" in msg or "result" not in msg:
                log.debug("ws %s %s failed: %s", op, w[:6], msg.get("error"))
                return
            with self._lock:
                if op == "sub":
                    self._subs[w] = msg["result"]
                    sub_wallet[msg["result"]] = w
                else:
                    sid = self._subs.pop(w, None)
                    sub_wallet.pop(sid, None)
            return
        if msg.get("method") != "logsNotification":
            return
        p = msg.get("params") or {}
        w = sub_wallet.get(p.get("subscription"))
        v = ((p.get("result") or {}).get("value")) or {}
        if w and v.get("signature") and not v.get("err"):
            self.stats["notifications"] += 1
            now = self.last_notification = time.time()
            self.recent[v["signature"]] = (now, w)
            while len(self.recent) > 3000:
                self.recent.popitem(last=False)
            # the logs ride along: pump.fun trades can be read from them without asking for the transaction
            self.events.put((w, v["signature"], time.time(), v.get("logs")))
