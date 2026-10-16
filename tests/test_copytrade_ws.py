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


def test_websocket_path_applies_the_same_entry_rules(env, monkeypatch):
    """A whale buy from the websocket worker goes through Strategy.on_whale_buy with all its gates."""
    from copytrade import watch
    s, conn, ex, state = env
    from copytrade import strategy as strat_mod
    import dataclasses
    st = strat_mod.Strategy(conn, dataclasses.replace(s, explore_unranked=False), ex)
    from test_copytrade import whale_buy
    watch.handle_swap(st, whale_buy())  # unranked whale: only measured, never bought (CT_EXPLORE_UNRANKED off)
    assert conn.execute("SELECT COUNT(*) FROM ct_positions").fetchone()[0] == 0
    assert watch.ENTRY_LOCK.acquire(blocking=False)  # released again after the decision
    watch.ENTRY_LOCK.release()


class _SigRpc:
    def __init__(self, fail=False):
        self.fail = fail

    def signatures(self, wallet, limit=80, **kw):
        if self.fail:
            raise RuntimeError("rpc down")
        return [{"signature": "sigB", "blockTime": int(time.time())}]

    def transaction(self, sig, **kw):
        return None


def test_backfill_loads_new_finds_once_best_first_and_gives_up_on_dead_wallets(env):
    s, conn, ex, state = env
    old, new, ultra = (f"Bbbb{i:040d}"[:44] for i in range(3))
    store.add_wallets(conn, [old], "auto")
    conn.execute("UPDATE ct_wallets SET added_at=added_at-100 WHERE address=?", (old,))
    store.add_wallets(conn, [new, ultra], "auto")
    conn.execute("INSERT OR REPLACE INTO wallets(address, tier) VALUES (?, 'ultra')", (ultra,))
    store.add_wallets(conn, [W], "manual")
    # a fresh find that already has one live swap (the old query skipped it forever)
    conn.execute("INSERT INTO ct_swaps(sig, wallet, mint, side, ts, seen_at) VALUES ('x', ?, ?, 'buy', 1, 1)",
                 (new, MINT))
    assert tracker.backfill_due(conn, 10) == [W, ultra, new, old]
    tracker.backfill(conn, s, _SigRpc(), W)
    assert W not in tracker.backfill_due(conn, 10)  # done once, never again
    for _ in range(3):
        with pytest.raises(RuntimeError):
            tracker.backfill(conn, s, _SigRpc(fail=True), old)
    assert tracker.backfill_due(conn, 10) == [ultra, new]  # dead wallet no longer blocks a slot


def test_migration_marks_wallets_with_history_as_backfilled(tmp_path):
    import sqlite3

    db = tmp_path / "old.db"
    raw = sqlite3.connect(db)
    raw.executescript("""CREATE TABLE ct_wallets (address TEXT PRIMARY KEY, label TEXT DEFAULT '',
        source TEXT DEFAULT 'auto', added_at INTEGER, last_sig TEXT, last_polled INTEGER DEFAULT 0,
        active INTEGER DEFAULT 1);
        CREATE TABLE ct_swaps (sig TEXT, wallet TEXT, mint TEXT, side TEXT, ts INTEGER, token_amount REAL,
        sol_amount REAL, price_sol REAL, seen_at INTEGER, PRIMARY KEY (sig, wallet, mint));""")
    raw.executemany("INSERT INTO ct_wallets(address, added_at) VALUES (?, 1)", [("rich",), ("empty",)])
    raw.executemany("INSERT INTO ct_swaps(sig, wallet, mint) VALUES (?, 'rich', 'm')", [(f"s{i}",) for i in range(6)])
    raw.commit()
    raw.close()
    conn = store.connect(Settings(db_path=db))
    assert tracker.backfill_due(conn, 10) == ["empty"]


def test_failed_decimals_lookup_is_not_cached(monkeypatch):
    real = chain.token_decimals
    class R:
        n = 0

        def read(self, method, params, priority="live", **kw):
            R.n += 1
            if R.n == 1:
                raise RuntimeError("429")
            return {"value": {"decimals": 9}}

    chain._decimals_cache.pop("MintX", None)
    assert real(R(), "MintX") == 6
    assert real(R(), "MintX") == 9 and real(R(), "MintX") == 9 and R.n == 2


# ------------------------------------------------------------------ pump.fun trades read from the logs
import base64 as _b64
import hashlib as _hl
import struct as _st

_USER = bytes(range(1, 33))
_MINTB = bytes(range(40, 72))
WHALE = chain._b58(_USER)
PMINT = chain._b58(_MINTB)


def _trade_log(sol=2.0, tokens=50_000_000.0, buy=True, user=_USER, mint=_MINTB, ts=None):
    raw = (_hl.sha256(b"event:TradeEvent").digest()[:8] + mint + _st.pack("<QQ", int(sol * 1e9), int(tokens * 1e6))
           + bytes([1 if buy else 0]) + user + _st.pack("<qQQ", ts or int(time.time()), 40 * 10**9, 800 * 10**12)
           + bytes(100))  # newer program versions append fee/creator fields
    return "Program data: " + _b64.b64encode(raw).decode()


def _logs(*events):
    return [f"Program {chain.PUMP_PROGRAM} invoke [1]", "Program log: Instruction: Buy", *events,
            f"Program {chain.PUMP_PROGRAM} success"]


def test_pump_trade_is_read_from_the_notification_logs():
    sw = chain.swap_from_logs(_logs(_trade_log(1.5, 30e6), _trade_log(0.5, 10e6)), WHALE, "sigL")
    assert sw and (sw.side, sw.mint, sw.wallet, sw.signature) == ("buy", PMINT, WHALE, "sigL")
    assert sw.sol_amount == pytest.approx(2.0) and sw.token_amount == pytest.approx(40e6)
    assert sw.price_sol == pytest.approx(2.0 / 40e6) and abs(sw.block_time - time.time()) < 5
    sell = chain.swap_from_logs(_logs(_trade_log(buy=False)), WHALE, "s")
    assert sell and sell.side == "sell"
    # unclear cases go the normal getTransaction way
    assert chain.swap_from_logs(_logs(_trade_log(user=bytes(32 * [9]))), WHALE, "s") is None  # someone else
    assert chain.swap_from_logs(_logs(_trade_log(), _trade_log(buy=False)), WHALE, "s") is None  # buy + sell
    assert chain.swap_from_logs(_logs(_trade_log(), "Log truncated"), WHALE, "s") is None
    assert chain.swap_from_logs(["Program log: Instruction: Route"], WHALE, "s") is None  # Jupiter/Raydium
    assert chain.swap_from_logs(None, WHALE, "s") is None


class _NoTxRpc:
    def __init__(self, tx=None):
        self.tx, self.reads = tx, []

    def read(self, method, params, **kw):
        self.reads.append((method, kw.get("priority")))
        return self.tx


def test_watcher_copies_a_pump_buy_without_any_rpc_call_and_verifies_later(env, monkeypatch):
    from copytrade import watch

    s, conn, ex, state = env
    seen = []

    class Strat:
        def on_whale_buy(self, sw):
            seen.append(sw)

        def process_pending(self):
            pass

    full_tx = parsed_tx(-2_200_000_000, 0, 40e6, payer=WHALE)  # with pump.fun's fee: 2.2 SOL really spent
    for b in full_tx["meta"]["preTokenBalances"] + full_tx["meta"]["postTokenBalances"]:
        b.update(owner=WHALE, mint=PMINT)
    rpc = _NoTxRpc(full_tx)
    w = watch.Watcher(Settings(db_path=s.db_path, ws_urls=["off"]), rpc, {}, threading.Event())
    t0 = time.time()
    sw = w.handle(conn, Strat(), WHALE, "sigFast", t0, _logs(_trade_log(2.0, 40e6)))
    assert sw and seen == [sw] and rpc.reads == [] and time.time() - t0 < 0.5 and w.fast == 1
    assert conn.execute("SELECT source FROM ct_detect WHERE sig='sigFast'").fetchone()[0] == "wslog"
    # the second socket reports the same transaction: handled once
    assert w.handle(conn, Strat(), WHALE, "sigFast", t0, _logs(_trade_log(2.0, 40e6))) is None and len(seen) == 1
    # later, when idle: checked against the full transaction on the public bulk lane
    w._verify[0] = (0, 0, w._verify[0][2])
    w._verify_one(conn)
    assert rpc.reads == [("getTransaction", "bulk")] and tracker.VERIFY["corrected"] >= 1
    sol = conn.execute("SELECT sol_amount FROM ct_swaps WHERE sig='sigFast'").fetchone()[0]
    assert sol == pytest.approx(2.2, abs=0.01)  # exact amount from the transaction replaces the curve amount
    # a log-read swap the transaction does not confirm is flagged
    rpc.tx = parsed_tx(-2_200_000_000, 0, 40e6)  # someone else's balances
    w._verify.append((0, 0, sw))
    w._verify_one(conn)
    assert rpc.reads[-1:] == [("getTransaction", "bulk")] and tracker.VERIFY["rejected"] == 0  # 2-s spacing
    w._last_verify = 0
    w._verify_one(conn)
    assert tracker.VERIFY["rejected"] >= 1
    # sells read from the logs are not re-read (they keep the curve amounts)
    n = len(w._verify)
    w.handle(conn, Strat(), WHALE, "sigSell", t0, _logs(_trade_log(1.0, 20e6, buy=False)))
    assert len(w._verify) == n


def _fake_streams(n=2):
    from copytrade import watch

    w = watch.Watcher(Settings(ws_urls=[f"ws://n{i}" for i in range(n)], ws_parallel=n), None, {}, threading.Event())
    for st in w.streams:
        st.connected, st.connected_at = True, time.time() - 1000
        st._subs = {W: 1}
    return w


def test_watchdog_reconnects_a_stream_that_went_silent():
    w = _fake_streams()
    a, b = w.streams
    now = time.time()
    for i in range(6):  # a got six transactions of a wallet both watch, b none of them
        a.recent[f"s{i}"] = (now - 60 - i, W)
    assert w.watchdog(now) == [b.name] and b._kick and not a._kick and b.stats["kicks"] == 1
    assert "1 watchdog" in w.status()
    # a stream that got (nearly) everything stays; very fresh transactions are not held against anyone yet
    w2 = _fake_streams()
    a2, b2 = w2.streams
    for i in range(6):
        a2.recent[f"s{i}"] = (now - 60, W)
        if i:
            b2.recent[f"s{i}"] = (now - 60, W)
    a2.recent["fresh"] = (now - 2, W)
    assert w2.watchdog(now) == []


def test_watchdog_acts_on_gaps_polling_found_once_per_ten_minutes():
    w = _fake_streams()
    a, b = w.streams
    a.last_notification, b.last_notification = time.time() - 30, time.time() - 900
    now = time.time()
    tracker.LAG.clear()
    for _ in range(3):
        tracker.LAG.append(("gap", "buy", 50.0, now - 60))
    assert w.watchdog(now) == [b.name]  # the one silent longest
    b._kick = None
    assert w.watchdog(now + 60) == []
    tracker.LAG.clear()


def test_kicked_stream_moves_to_the_next_endpoint():
    a, b = FakeNode(), FakeNode()
    st = ws.WalletStream(Settings(), url=[f"ws://127.0.0.1:{a.port}", f"ws://127.0.0.1:{b.port}"])
    st.set_wallets([W])
    st.start()
    try:
        assert _wait(lambda: W in st.covered() and str(a.port) in st.url)
        st.reconnect("test")
        assert _wait(lambda: W in st.covered() and str(b.port) in st.url)
        assert st.stats["connects"] == 2 and st.stats["kicks"] == 1
        b.notify(W, "sigK")
        assert st.events.get(timeout=5)[1] == "sigK" and st.saw("sigK")
    finally:
        st.stop()


def test_detections_carry_value_venue_and_market_cap_at_the_time(env):
    s, conn, ex, state = env
    tracker._seen.clear()
    sw = tracker.process_logs(conn, s, WHALE, "sigV", _logs(_trade_log(2.0, 40e6)), time.time())
    assert sw.dex == "pump" and sw.mcap_sol == pytest.approx(40 / 800e6 * 1e9)
    r = conn.execute("SELECT usd, price_usd, dex, mcap_usd FROM ct_detect WHERE sig='sigV'").fetchone()
    assert r["usd"] == pytest.approx(200.0) and r["dex"] == "pump"  # SOL at $100 in the fixture
    assert r["price_usd"] == pytest.approx(2.0 / 40e6 * 100) and r["mcap_usd"] == pytest.approx(5000.0)
    from copytrade import stats

    summ = stats.detection_summary(conn, 1)
    assert list(summ["source"]) == ["wslog"] and summ["within_10s"].iloc[0] == 1.0


def test_venue_is_read_from_the_programs_a_transaction_touches():
    tx = parsed_tx(-3_000_000_000, 0, 1_000_000)
    assert chain.extract_swap(tx, W).dex == ""
    tx["transaction"]["message"]["accountKeys"].append({"pubkey": "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"})
    assert chain.extract_swap(tx, W).dex == "pumpswap"
    tx["meta"]["innerInstructions"] = [{"instructions": [{"programId": "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUJoi5QNyVTaV4"}]}]
    assert chain.dex_of(tx) == "jupiter"  # routed through the aggregator


def test_websocket_reads_spend_main_rpc_credits_only_on_key_wallets(env, monkeypatch):
    s, conn, ex, state = env
    calls = []

    class Pub:
        counts, hosts = {}, {}

        def call(self, method, params, priority="bulk"):
            calls.append("public")
            raise RuntimeError("429")

    rpc = chain.Rpc(Settings(rpc_url="https://mainnet.helius-rpc.com/?api-key=k", poll_rpc_url=""))
    rpc.public = Pub()
    monkeypatch.setattr(chain.Rpc, "call", lambda self, m, p, priority="live": calls.append("main"))
    monkeypatch.setattr(tracker.time, "sleep", lambda x: None)
    store.add_wallets(conn, ["Noname1111"], "auto")
    store._winfo_cache.clear()
    tracker._seen.clear()
    assert tracker.process_signature(conn, s, rpc, "Noname1111", "sigN", 3) is None
    assert calls == ["public"] * 3  # unranked wallet: public pool only, polling picks it up
    calls.clear()
    store.add_wallets(conn, [W], "manual")
    tracker.process_signature(conn, s, rpc, W, "sigM", 3)
    assert calls == ["public", "public", "public", "main"]  # main RPC once, on the last try


def test_look_alike_trade_events_from_other_programs_are_ignored():
    fake = ["Program Bait111111111111111111111111111111111111 invoke [1]", _trade_log(),
            "Program Bait111111111111111111111111111111111111 success"]
    assert chain.swap_from_logs(fake, WHALE, "s") is None  # printed by some other program: bait
    nested = [f"Program {chain.PUMP_PROGRAM} invoke [1]", "Program Other invoke [2]", "Program Other success",
              _trade_log(), f"Program {chain.PUMP_PROGRAM} success"]
    assert chain.swap_from_logs(nested, WHALE, "s")  # back in pump.fun after an inner call
    inner = ["Program JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUJoi5QNyVTaV4 invoke [1]",
             f"Program {chain.PUMP_PROGRAM} invoke [2]", _trade_log(), f"Program {chain.PUMP_PROGRAM} success",
             _trade_log(mint=bytes(32 * [7])), "Program JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUJoi5QNyVTaV4 success"]
    sw = chain.swap_from_logs(inner, WHALE, "s")  # routed through Jupiter: pump.fun's own event counts, the
    assert sw and sw.mint == PMINT                # look-alike printed by the router does not


def test_subscriptions_are_paced_best_first_and_capped_after_a_limit_close(monkeypatch):
    closed = []
    a, b = FakeNode(), FakeNode()
    monkeypatch.setattr(ws.WalletStream, "SUBS_PER_SEC", 8.0)
    monkeypatch.setattr(ws.WalletStream, "MIN_CAP", 1)
    monkeypatch.setattr(ws.WalletStream, "caps", {})
    monkeypatch.setattr(ws.WalletStream, "rest_until", {})
    wallets = [f"Wal{i}" for i in range(6)]
    ua, ub = f"ws://127.0.0.1:{a.port}", f"ws://127.0.0.1:{b.port}"
    st = ws.WalletStream(Settings(), url=[ua, ub])
    st.set_wallets(wallets)

    def watch_a():
        # play the node's limit: after 3 confirmed subscriptions close the client with 1013
        while not closed:
            if len(a.subs) >= 3 and a.clients:
                sock = a.clients[-1]
                asyncio.run_coroutine_threadsafe(sock.close(1013, "Too many subscriptions attempted"),
                                                 a.loop).result(5)
                closed.append(True)
            time.sleep(0.02)

    threading.Thread(target=watch_a, daemon=True).start()
    st.start()
    try:
        assert _wait(lambda: closed and len(st.covered()) == 6 and st.url == ub)
        assert ws.WalletStream.caps[ua] <= 3 and ws.WalletStream.rest_until[ua] > time.time()
        assert st.stats["limited"] == 1
        assert list(a.subs)[:3] == wallets[:3]  # best wallets subscribed first, in order
    finally:
        st.stop()


def test_whale_sells_do_not_take_the_entry_lock():
    from copytrade import watch

    got = []

    class Strat:
        def on_whale_sell(self, sw):
            got.append(watch.ENTRY_LOCK.locked())

        def on_whale_buy(self, sw):
            got.append(watch.ENTRY_LOCK.locked())

    sw = chain.Swap("s", W, 0, "sell", MINT, 1, 1, 1)
    watch.handle_swap(Strat(), sw)
    watch.handle_swap(Strat(), chain.Swap("s", W, 0, "buy", MINT, 1, 1, 1))
    assert got == [False, True]
