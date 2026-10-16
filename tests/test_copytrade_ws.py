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
    monkeypatch.setattr(chain, "_core_http", None)  # without core's endpoint pool (tested below)
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


def test_public_reads_rotate_over_core_endpoint_pool(monkeypatch):
    import types
    sent = []
    pool = types.SimpleNamespace(
        public_rpc_urls=lambda: ["https://api.mainnet-beta.solana.com", "https://solana-rpc.publicnode.com"],
        pick_public_rpc=lambda prio: "https://solana-rpc.publicnode.com" if prio == "live"
        else "https://api.mainnet-beta.solana.com",
        post_json=lambda url, p, **k: sent.append((url, p["method"], k["priority"], k["retries"])) or {"result": 7})
    monkeypatch.setattr(chain, "_core_http", pool)
    rpc = chain.Rpc(Settings(rpc_url="https://mainnet.helius-rpc.com/?api-key=k", poll_rpc_url=""))
    assert rpc.public.pooled and not rpc.pooled
    assert rpc.transaction("s", priority="live") == 7
    rpc.signatures(W)
    assert sent == [("https://solana-rpc.publicnode.com", "getTransaction", "live", 1),
                    ("https://api.mainnet-beta.solana.com", "getSignaturesForAddress", "bulk", 3)]
    assert rpc.endpoints() == {"solana-rpc.publicnode.com": 1, "api.mainnet-beta.solana.com": 1}


def test_stream_switches_endpoint_after_a_drop():
    node = FakeNode()
    dead = "ws://127.0.0.1:1"  # nothing listens: connection refused
    st = ws.WalletStream(Settings(), url=[dead, f"ws://127.0.0.1:{node.port}"]).start()
    try:
        st.set_wallets([W])
        assert _wait(lambda: W in st.covered())
        assert st.url.endswith(str(node.port)) and st.stats["errors"] >= 1
        node.kick()  # drop -> next endpoint (the dead one), then back round to the live one
        assert _wait(lambda: st.stats["connects"] >= 2 and W in st.covered(), 15)
    finally:
        st.stop()


def test_ws_urls_setting(monkeypatch):
    monkeypatch.setenv("CT_WS_URLS", "wss://a, wss://b")
    assert Settings().ws_urls == ["wss://a", "wss://b"] and Settings().ws_url == "wss://a"
    monkeypatch.setenv("CT_WS_URLS", "off")
    assert Settings().ws_url == "off"
    monkeypatch.delenv("CT_WS_URLS")
    assert len(Settings().ws_urls) == 3


# ---------------------------------------------------------------------- latency, gaps, coverage
def _helius_rpc(monkeypatch, tx_for):
    calls = []

    def fake_call(self, method, params, priority="live"):
        calls.append((method, priority))
        if method == "getTransaction":
            return tx_for(params[0])
        return [{"signature": s} for s in ("sigNew", "sigMid")]  # newest first

    monkeypatch.setattr(chain.Rpc, "call", fake_call)
    monkeypatch.setattr(chain, "_core_http", None)
    return chain.Rpc(Settings(rpc_url="https://mainnet.helius-rpc.com/?api-key=k", poll_rpc_url="")), calls


def test_poll_never_skips_a_transaction_it_could_not_read(env, monkeypatch):
    s, conn, ex, state = env
    store.add_wallets(conn, [W], "manual")
    conn.execute("UPDATE ct_wallets SET last_sig='sigOld' WHERE address=?", (W,))
    tracker._seen.clear()
    ok = {"sigMid": False}
    rpc, _ = _helius_rpc(monkeypatch, lambda sig: parsed_tx(-3_000_000_000, 0, 1_000_000) if ok.get(sig, True) else None)
    row = conn.execute("SELECT * FROM ct_wallets WHERE address=?", (W,)).fetchone()
    assert tracker.poll(conn, s, rpc, row, "live") == []  # sigMid (older) not readable yet -> stop before it
    assert conn.execute("SELECT last_sig FROM ct_wallets").fetchone()[0] == "sigOld"
    ok["sigMid"] = True
    row = conn.execute("SELECT * FROM ct_wallets WHERE address=?", (W,)).fetchone()
    got = tracker.poll(conn, s, rpc, row, "live", covered=True)
    assert len(got) == 2 and conn.execute("SELECT last_sig FROM ct_wallets").fetchone()[0] == "sigNew"
    assert tracker.DETECTED["gap"] >= 2  # the websocket watched this wallet and missed both


def test_detection_lag_is_recorded_per_source(env, monkeypatch):
    from copytrade import stats
    s, conn, ex, state = env
    store.add_wallets(conn, [W], "manual")
    tracker._seen.clear()
    tx = parsed_tx(-3_000_000_000, 0, 1_000_000)
    tx["blockTime"] = int(time.time()) - 4
    rpc, _ = _helius_rpc(monkeypatch, lambda sig: tx)
    sw = tracker.process_signature(conn, s, rpc, W, "sigWS", received_at=time.time() - 2)
    assert sw
    df = stats.detection(conn)
    assert list(df.source) == ["ws"] and 3.5 <= df.lag_s.iloc[0] < 30 and 1.5 <= df.notify_lag_s.iloc[0] < 30
    sm = stats.detection_summary(conn)
    assert sm.iloc[0]["n"] == 1 and sm.iloc[0]["within_30s"] == 1.0
    assert tracker.lag_summary(0)["ws"]["n"] >= 1


def test_every_active_wallet_is_watched_live_best_first(env):
    s, conn, ex, state = env
    others = [f"Aaaa{i:040d}"[:44] for i in range(5)]
    store.add_wallets(conn, others, "auto")
    store.add_wallets(conn, [W], "manual")
    conn.execute("INSERT OR REPLACE INTO wallets(address, tier) VALUES (?, 'ultra')", (others[3],))
    store._winfo_cache.clear()
    got = tracker.ws_wallets(conn, s)
    assert got[0] == W and got[1] == others[3] and len(got) == 6  # not only "hot" wallets any more
    s2 = Settings(db_path=s.db_path, ws_max_wallets=2)
    assert tracker.ws_wallets(conn, s2) == [W, others[3]]


def test_watcher_copies_from_its_own_thread_and_dedupes_two_streams(env, monkeypatch):
    from copytrade import watch
    s, conn, ex, state = env
    store.add_wallets(conn, [W], "manual")
    tracker._seen.clear()
    tx = parsed_tx(-3_000_000_000, 0, 1_000_000)
    rpc, calls = _helius_rpc(monkeypatch, lambda sig: tx)
    seen = []
    from copytrade import strategy as strat_mod
    monkeypatch.setattr(strat_mod.Strategy, "on_whale_buy", lambda self, sw, deferred=False: seen.append(sw.signature))
    a, b = FakeNode(), FakeNode()
    stop = threading.Event()
    s2 = Settings(db_path=s.db_path, ws_urls=[f"ws://127.0.0.1:{a.port}", f"ws://127.0.0.1:{b.port}"], ws_parallel=2)
    w = watch.Watcher(s2, rpc, {"paper": ex}, stop).start([W])
    try:
        assert _wait(lambda: W in w.covered() and all(W in st.covered() for st in w.streams))
        a.notify(W, "sigX")
        b.notify(W, "sigX")  # same transaction on both connections
        assert _wait(lambda: len(seen) == 1)  # (the swap carries the transaction's own signature)
        time.sleep(0.5)
        assert len(seen) == 1 and w.handled == 1 and calls == [("getTransaction", "live")]
        assert "up" in w.status()
    finally:
        stop.set()
        w.stop()


def test_stream_survives_answers_without_result():
    st = ws.WalletStream(Settings(), url="ws://x")
    pending = {1: ("sub", W)}
    st._handle({"jsonrpc": "2.0", "id": 1}, pending, {})  # no result, no error: ignored, not a crash
    assert not st._subs and not pending
