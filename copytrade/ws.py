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


def _redact(text: str) -> str:
    """Errors may quote the URL, and the Helius one carries the API key."""
    import re

    return re.sub(r"(api-key=)[^&\s'\"]+", r"\1***", text)

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

    # public nodes limit subscription *attempts* per connection/IP and close with 1013 "Too many subscriptions
    # attempted": requests are paced by a token bucket, best wallets first, and an endpoint that closed with
    # 1013 gets a lower cap (80 % of what it held) and a 10-minute rest while the stream uses the next one
    SUBS_PER_SEC = 4.0
    REST_AFTER_LIMIT_S = 600
    MIN_CAP = 20
    # shared by all streams in the process (the limit is per IP, not per connection): url -> cap / rest-until
    caps: dict[str, int] = {}
    rest_until: dict[str, float] = {}

    def __init__(self, s: Settings, url: str | list[str] | None = None, events: queue.Queue | None = None,
                 name: str = "wallet-ws", complement: bool = False):
        urls = [url] if isinstance(url, str) else (url or [u for u in s.ws_urls if u != "off"])
        self.urls = urls
        self._idx = 0
        self.name = name
        self.complement = complement  # second stream: covers what a capped first stream cannot hold
        self.max_wallets = s.ws_max_wallets
        self.events: queue.Queue = events if events is not None else queue.Queue()
        self.connected = False
        self.stats = {"notifications": 0, "connects": 0, "errors": 0, "kicks": 0}
        self.connected_at = 0.0
        self.last_notification = 0.0
        # signature -> (received_at, wallet) of recent notifications, so the watcher can compare streams
        self.recent: "collections.OrderedDict[str, tuple[float, str]]" = collections.OrderedDict()
        self._kick: str | None = None
        self._ever: set[str] = set()  # wallets this stream had subscribed before (a resubscribe means a gap)
        self.sub_at: dict[str, float] = {}  # wallet -> when its subscription was confirmed (watchdog fairness)
        self._tokens, self._tok_at = 1.0, 0.0
        self.stats["limited"] = 0
        self.bytes = 0  # received on this stream (Helius bills streamed data)
        self.notes: collections.deque = collections.deque(maxlen=50000)  # (received_at, wallet, bytes) of notifications
        self._wanted: list[str] = []  # best first (manual, Ultra, Smart, rank)
        self._subs: dict[str, int] = {}  # wallet -> subscription id
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ API for the engine
    def set_wallets(self, wallets) -> None:
        with self._lock:
            self._wanted = list(dict.fromkeys(wallets))[: self.max_wallets]

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

    def _pick(self, wanted: list[str], cap: int) -> list[str]:
        """Which wallets to subscribe under a cap. The primary stream takes the best `cap`; a complement stream
        takes the ones the primary cannot hold first and fills up with the best, so two capped connections
        cover up to twice the cap instead of the same wallets twice."""
        if len(wanted) <= cap or not self.complement:
            return wanted[:cap]
        rest = wanted[cap:][:cap]
        return wanted[: cap - len(rest)] + rest

    def _allow(self) -> bool:
        """Token bucket: SUBS_PER_SEC subscription requests per second, no bursts."""
        now = time.time()
        self._tokens = min(1.0, self._tokens + (now - (self._tok_at or now)) * self.SUBS_PER_SEC)
        self._tok_at = now
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        return False

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
            if self.rest_until.get(url, 0) > time.time() and any(
                    self.rest_until.get(u, 0) <= time.time() for u in self.urls):
                self._idx = (self._idx + 1) % len(self.urls)  # resting after a subscription limit: next one
                continue
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
                txt = str(e)
                if "1013" in txt or "Too many subscriptions" in txt:
                    with self._lock:
                        held = len(self._subs)
                    cap = max(self.MIN_CAP, int(held * 0.8))
                    self.caps[url] = min(cap, self.caps.get(url, cap))
                    self.rest_until[url] = time.time() + self.REST_AFTER_LIMIT_S
                    self.stats["limited"] += 1
                    log.info("wallet websocket %s: subscription limit at %d wallets, cap %d, resting %d min",
                             url.split("?")[0], held, self.caps[url], self.REST_AFTER_LIMIT_S // 60)
                else:
                    log.info("wallet websocket %s dropped: %s", url.split("?")[0], _redact(txt)[:120])
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
            if time.time() - last_sync > 0.25:
                last_sync = time.time()
                cap = self.caps.get(self.url, self.max_wallets)
                with self._lock:
                    want_list, have = self._pick(list(self._wanted), cap), dict(self._subs)
                want = set(want_list)
                busy = {w for _, w in pending.values()}
                for w in [w for w in want_list if w not in have and w not in busy]:
                    if len(have) + len(busy) >= cap or not self._allow():
                        break
                    busy.add(w)
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
                raw = await asyncio.wait_for(sock.recv(), timeout=0.25)
            except asyncio.TimeoutError:
                continue
            self.bytes += len(raw)
            self._last_len = len(raw)
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
                    self.sub_at[w] = time.time()
                    if w in self._ever:  # back after a drop: whatever it did meanwhile is read at once
                        self.events.put((w, None, time.time(), "catchup"))
                    self._ever.add(w)
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
            self.notes.append((now, w, getattr(self, "_last_len", 0)))
            while len(self.recent) > 3000:
                self.recent.popitem(last=False)
            # the logs ride along: pump.fun trades can be read from them without asking for the transaction
            self.events.put((w, v["signature"], time.time(), v.get("logs")))
