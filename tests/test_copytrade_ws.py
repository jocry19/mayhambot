"""Websocket wallet detection, public-RPC-first reads and dedupe between websocket and polling (offline)."""
import asyncio
import json
import threading
import time

import pytest

from copytrade import chain, store, tracker, ws
from copytrade.config import Settings

from test_copytrade import MINT, W, env, parsed_tx  # noqa: F401  (fixture)

websockets = pytest.importorskip("websockets")


class FakeNode:
    """Minimal Solana websocket: answers logsSubscribe and can push a notification; kick() drops the client."""

    def __init__(self):
        self.subs = {}
        self.clients = []
        self.loop = None
        self.port = None
        ready = threading.Event()

        async def handler(sock):
            self.clients.append(sock)
            async for raw in sock:
                m = json.loads(raw)
                if m["method"] == "logsSubscribe":
                    sid = len(self.subs) + 100
                    self.subs[m["params"][0]["mentions"][0]] = (sock, sid)
                    await sock.send(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": sid}))
                elif m["method"] == "logsUnsubscribe":
                    await sock.send(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": True}))

        async def main():
            self.loop = asyncio.get_running_loop()
            async with websockets.serve(handler, "127.0.0.1", 0) as srv:
                self.port = srv.sockets[0].getsockname()[1]
                ready.set()
                await asyncio.Future()

        threading.Thread(target=lambda: asyncio.run(main()), daemon=True).start()
        ready.wait(5)

    def notify(self, wallet, sig, err=None):
        sock, sid = self.subs[wallet]
        msg = {"jsonrpc": "2.0", "method": "logsNotification",
               "params": {"subscription": sid, "result": {"context": {"slot": 1},
                                                         "value": {"signature": sig, "err": err, "logs": []}}}}
        asyncio.run_coroutine_threadsafe(sock.send(json.dumps(msg)), self.loop).result(5)

    def kick(self):
        for c in self.clients:
            asyncio.run_coroutine_threadsafe(c.close(), self.loop).result(5)
        self.subs.clear()


def _wait(cond, t=8.0):
    end = time.time() + t
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_stream_subscribes_notifies_and_reconnects():
    node = FakeNode()
    st = ws.WalletStream(Settings(), url=f"ws://127.0.0.1:{node.port}").start()
    try:
        st.set_wallets([W])
        assert _wait(lambda: W in st.covered())
        node.notify(W, "sigA")
        node.notify(W, "sigFail", err={"x": 1})  # failed tx: ignored
        assert st.events.get(timeout=5)[:2] == (W, "sigA")
        assert st.events.empty() or st.events.get(timeout=1)[1] != "sigFail"
        node.kick()  # connection lost -> reconnect with backoff and subscribe again
        assert _wait(lambda: st.stats["connects"] >= 2 and W in st.covered())
        node.notify(W, "sigB")
        assert st.events.get(timeout=5)[1] == "sigB"
    finally:
        st.stop()


def test_ws_swap_is_handled_once_even_when_polled_later(env, monkeypatch):
    s, conn, ex, state = env
    store.add_wallets(conn, [W], "manual")
    rpc = chain.Rpc(Settings(rpc_url="https://mainnet.helius-rpc.com/?api-key=k", poll_rpc_url=""))
    tx = parsed_tx(-3_000_000_000, 0, 1_000_000)
    calls = []

    def fake_call(self, method, params, priority="live"):
        calls.append((self.url.split("?")[0], method, priority))
        if method == "getTransaction":
            return tx
        return [{"signature": "sig1"}]

    monkeypatch.setattr(chain.Rpc, "call", fake_call)
    tracker._seen.clear()
    sw = tracker.process_signature(conn, s, rpc, W, "sig1", received_at=time.time() - 0.4)
    assert sw and sw.side == "buy" and tracker.DETECTED["ws"] >= 1
    # public RPC (no Helius credit), live lane (a fresh whale buy may be copied)
    assert calls == [("https://api.mainnet-beta.solana.com", "getTransaction", "live")]
    assert tracker.WS_LATENCY and tracker.WS_LATENCY[-1] >= 0.4
    assert tracker.process_signature(conn, s, rpc, W, "sig1") is None  # duplicate notification
    conn.execute("UPDATE ct_wallets SET last_sig='old' WHERE address=?", (W,))
    row = conn.execute("SELECT * FROM ct_wallets WHERE address=?", (W,)).fetchone()
    assert tracker.poll(conn, s, rpc, row) == []  # the polling net sees sig1 again but does not act twice
    assert calls[-1] == ("https://api.mainnet-beta.solana.com", "getSignaturesForAddress", "bulk")


def test_covered_wallets_are_only_polled_as_safety_net(env):
    s, conn, ex, state = env
    now = time.time()
    store.add_wallets(conn, [W], "auto")
    conn.execute("INSERT INTO ct_swaps(sig, wallet, mint, side, ts) VALUES ('x', ?, ?, 'buy', ?)", (W, MINT, int(now - 60)))
    conn.execute("UPDATE ct_wallets SET last_polled=?", (int(now - 20),))
    assert [r["address"] for r in tracker.due_wallets(conn, s, now)] == [W]
    assert tracker.due_wallets(conn, s, now, covered={W}) == []
    assert tracker.hot_wallets(conn, s, now) == [W]


def test_rpc_reads_public_first_live_paths_on_main(monkeypatch):
    from copytrade import http as cthttp
    urls = []
    monkeypatch.setattr(cthttp, "post_json", lambda url, p, **k: urls.append((url.split("?")[0], p["method"]))
                        or prios.append((k["priority"], k["rpc_method"])) or {"result": {"value": 1}})
    prios = []
    rpc = chain.Rpc(Settings(rpc_url="https://mainnet.helius-rpc.com/?api-key=k", poll_rpc_url=""))
    monkeypatch.setattr(chain, "_core_rpc_url", lambda m, u: u)
    rpc.transaction("s")
    rpc.signatures(W)
    rpc.transaction("own", main=True)
    rpc.balance_sol(W)
    hel = "https://mainnet.helius-rpc.com/"
    assert [u for u, _ in urls] == [chain.PUBLIC_RPC, chain.PUBLIC_RPC, hel, hel]
    assert prios == [("bulk", "getTransaction"), ("bulk", "getSignaturesForAddress"), ("live", "getTransaction"),
                     ("live", "getBalance")]
    # today's Helius share used up: core routes even the live read to the public RPC
    monkeypatch.setattr(chain, "_core_rpc_url", lambda m, u: chain.PUBLIC_RPC)
    rpc.balance_sol(W)
    assert urls[-1][0] == chain.PUBLIC_RPC
    u = rpc.usage()
    assert u["public:getTransaction"] == 1 and u["helius:getBalance"] == 1 and u["public:getBalance"] == 1
