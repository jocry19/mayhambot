"""Real-time wallet activity over the Solana websocket (logsSubscribe, one subscription per wallet).

Free public endpoint by default (CT_WS_URL, "off" disables), so detecting a whale's transaction costs no
Helius credits and arrives within about a second instead of at the next poll. Notifications only carry the
signature; the engine reads the transaction (public RPC first) and treats it like a polled swap. Polling stays
on as the safety net for anything the socket misses (disconnects, dropped notifications).
"""
from __future__ import annotations

import asyncio
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

    def __init__(self, s: Settings, url: str | None = None):
        self.url = url or s.ws_url
        self.max_wallets = s.ws_max_wallets
        self.events: queue.Queue = queue.Queue()
        self.connected = False
        self.stats = {"notifications": 0, "connects": 0, "errors": 0}
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
        self._thread = threading.Thread(target=lambda: asyncio.run(self._main()), name="wallet-ws", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------ internals
    async def _main(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                async with websockets.connect(self.url, ping_interval=20, ping_timeout=20, max_size=2 ** 22,
                                              open_timeout=15) as sock:
                    self.connected = True
                    self.stats["connects"] += 1
                    backoff = 1.0
                    log.info("wallet websocket connected (%s)", self.url.split("?")[0])
                    await self._session(sock)
            except Exception as e:
                self.stats["errors"] += 1
                log.info("wallet websocket dropped: %s (reconnect in %.0fs)", str(e)[:120], backoff)
            finally:
                self.connected = False
                with self._lock:
                    self._subs.clear()
            if self._stop.wait(backoff):
                break
            backoff = min(backoff * 2, 60.0)

    async def _session(self, sock) -> None:
        req_id = 0
        pending: dict[int, tuple[str, str]] = {}  # request id -> (op, wallet)
        sub_wallet: dict[int, str] = {}
        last_sync = 0.0
        while not self._stop.is_set():
            if time.time() - last_sync > 5:
                last_sync = time.time()
                with self._lock:
                    want, have = set(self._wanted), dict(self._subs)
                busy = {w for _, w in pending.values()}
                for w in sorted(want - set(have) - busy):
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
            if "error" in msg:
                log.debug("ws %s %s failed: %s", op, w[:6], msg["error"])
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
            self.events.put((w, v["signature"], time.time()))
