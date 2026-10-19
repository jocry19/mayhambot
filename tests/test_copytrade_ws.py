"""Websocket wallet detection, public-RPC-first reads and dedupe between websocket and polling (offline)."""
import asyncio
import json
import queue
import threading
import time

import pytest

from copytrade import chain, store, tracker, ws
from copytrade.config import Settings

from test_copytrade import MINT, W, env, parsed_tx  # noqa: F401  (fixture)


@pytest.fixture(autouse=True)
def _lean_helius_budget(monkeypatch):
    """The real credit ledger of the machine must not decide how a test reads: budget 'tight' unless a test says so."""
    monkeypatch.setattr(tracker, "main_budget_ok", lambda *a, **kw: False)
    monkeypatch.delenv("HELIUS_API_KEY", raising=False)  # no extra Helius stream from the machine's environment

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
        ev = st.events.get(timeout=5)
        assert ev[1] is None and ev[3] == "catchup"  # resubscribe asks for a gap poll
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
    # on the Helius stream: the safety net waits 3 minutes instead of one
    conn.execute("UPDATE ct_wallets SET last_polled=?", (int(now - 90),))
    assert [r["address"] for r in tracker.due_wallets(conn, s, now, covered={W})] == [W]
    assert tracker.due_wallets(conn, s, now, covered={W}, solid={W}) == []
    conn.execute("UPDATE ct_wallets SET last_polled=?", (int(now - 200),))
    assert [r["address"] for r in tracker.due_wallets(conn, s, now, covered={W}, solid={W})] == [W]


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
    conn.execute("INSERT OR REPLACE INTO wallets(address, tier) VALUES (?, 'ultra')", (others[0],))
    now = int(time.time())
    conn.executemany("INSERT INTO ct_swaps(sig, wallet, mint, side, ts) VALUES (?, ?, 'm', 'buy', ?)",
                     [(f"b{i}", others[3], now - 60 * i) for i in range(3)] + [("old", others[1], now - 2 * 86400)])
    store._winfo_cache.clear()
    got = tracker.ws_wallets(conn, s)
    # manual first, then the most buys in 24 h; the ultra tier earns no place (it predicts nothing, v13)
    assert got[0] == W and got[1] == others[3] and got[2] == others[1] and len(got) == 6
    s2 = Settings(db_path=s.db_path, explore_tiers="ultra,smart", strict_whale=True, whale_emergency_stop=0.0, ws_max_wallets=2)
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
    s2 = Settings(db_path=s.db_path, explore_tiers="ultra,smart", strict_whale=True, whale_emergency_stop=0.0, ws_urls=[f"ws://127.0.0.1:{a.port}", f"ws://127.0.0.1:{b.port}"], ws_parallel=2)
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
    assert real(R(), "MintX") == 9  # first read failed, the retry got it
    assert real(R(), "MintX") == 9 and real(R(), "MintX") == 9 and R.n == 2  # one retry already succeeds


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
    w = watch.Watcher(Settings(db_path=s.db_path, explore_tiers="ultra,smart", strict_whale=True, whale_emergency_stop=0.0, ws_urls=["off"]), rpc, {}, threading.Event())
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
    tracker.GAPS.clear()
    for _ in range(3):
        tracker.GAPS.append((now - 60, W))
    assert w.watchdog(now) == [b.name]  # both had the wallet: the one silent longest
    b._kick = None
    assert w.watchdog(now + 60) == []
    # gaps of a wallet only a had subscribed (b benched it) are a's, however silent b is
    w2 = _fake_streams()
    a2, b2 = w2.streams
    a2.last_notification, b2.last_notification = now - 30, now - 900
    X = "X" * 44
    a2._subs = {W: 1, X: 2}
    tracker.GAPS.clear()
    for _ in range(3):
        tracker.GAPS.append((now - 60, X))
    assert w2.watchdog(now) == [a2.name]
    tracker.GAPS.clear()


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
        assert st.events.get(timeout=5)[3] == "catchup"
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


def test_watchdog_does_not_count_misses_before_the_own_subscription():
    w = _fake_streams()
    a, b = w.streams
    now = time.time()
    b.sub_at[W] = now - 30  # b's subscription of W only came up 30 s ago (fresh connection still subscribing)
    for i in range(6):
        a.recent[f"s{i}"] = (now - 60 - i, W)
    assert w.watchdog(now) == []


def test_two_capped_streams_cover_more_wallets_together():
    ws_ = [f"w{i}" for i in range(150)]
    a = ws.WalletStream(Settings(), url="ws://x")
    b = ws.WalletStream(Settings(), url="ws://x", complement=True)
    pa, pb = a._pick(ws_, 80), b._pick(ws_, 80)
    assert pa == ws_[:80] and pb[:10] == ws_[:10] and pb[10:] == ws_[80:]
    assert len(set(pa) | set(pb)) == 150  # instead of 80 twice
    assert b._pick(ws_[:50], 80) == ws_[:50]  # no cap pressure: both watch everything


def test_unread_notifications_are_counted_by_reason_and_venue():
    chain.LOG_STATS.clear()
    chain.LOG_VENUES.clear()
    chain.swap_from_logs(None, WHALE, "s")
    chain.swap_from_logs(["Program pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA invoke [1]",
                          "Program pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA success"], WHALE, "s")
    chain.swap_from_logs(_logs(_trade_log()), WHALE, "s")
    assert chain.LOG_STATS == {"no_logs": 1, "not_pump": 1, "read": 1}
    assert chain.LOG_VENUES == {"pumpswap": 1}


def test_catch_up_polls_a_resubscribed_wallet_at_most_every_two_minutes(tmp_path, monkeypatch):
    from copytrade import watch

    s = Settings(db_path=tmp_path / "c.db", ws_urls=["ws://n0"], ws_parallel=1)
    conn = store.connect(s)
    conn.execute("INSERT INTO ct_wallets(address, added_at, last_sig) VALUES (?, 1, 'old')", (W,))
    conn.execute("INSERT INTO ct_wallets(address, added_at) VALUES ('fresh', 1)")
    calls = []
    monkeypatch.setattr(tracker, "poll", lambda c, st, rpc, row, prio, covered: calls.append(
        (row["address"], prio, covered)) or ["swap"])
    w = watch.Watcher(s, None, {}, threading.Event())
    assert w.catch_up(conn, W, now=1000.0) == ["swap"]
    assert calls == [(W, "bulk", True)] and w.catchups == 1  # bulk lane: never Helius
    assert w.catch_up(conn, W, now=1100.0) == [] and len(calls) == 1
    assert w.catch_up(conn, W, now=1121.0) == ["swap"] and w.catchups == 2
    assert w.catch_up(conn, "fresh", now=1000.0) == []  # nothing read yet: the backfill covers it
    assert "2 catch-ups after reconnects" in w.status()


def test_fetchers_route_notifications_and_catch_ups_to_the_decider(tmp_path, monkeypatch):
    from copytrade import watch

    s = Settings(db_path=tmp_path / "f.db", ws_urls=["ws://n0"], ws_parallel=1)
    store.connect(s)
    stop = threading.Event()
    w = watch.Watcher(s, None, {}, stop)
    monkeypatch.setattr(w, "fetch", lambda c, wallet, sig, got, logs: f"sw-{sig}" if sig != "junk" else None)
    monkeypatch.setattr(w, "catch_up", lambda c, wallet: [f"cu-{wallet}"])
    threads = [threading.Thread(target=w._fetch_loop, daemon=True) for _ in range(2)]
    for t in threads:
        t.start()
    w.events.put((W, "a", time.time(), None))
    w.events.put((W, "junk", time.time(), None))
    w.events.put((W, None, time.time(), "catchup"))
    got = {w.decide.get(timeout=5) for _ in range(2)}
    stop.set()
    assert got == {"sw-a", f"cu-{W}"} and w.decide.empty()


def test_watchdog_leaves_a_kicked_stream_alone_for_ten_minutes():
    w = _fake_streams()
    a, b = w.streams
    now = time.time()
    for i in range(6):
        a.recent[f"s{i}"] = (now - 60 - i, W)
    assert w.watchdog(now) == [b.name]
    b._kick = None
    assert w.watchdog(now + 60) == []  # same misses, still cooling down
    for i in range(6):
        a.recent[f"t{i}"] = (now + 590 - i, W)
    assert w.watchdog(now + 620) == [b.name]
    # five misses are too few to judge
    w2 = _fake_streams()
    a2, _ = w2.streams
    for i in range(5):
        a2.recent[f"s{i}"] = (now - 60 - i, W)
    assert w2.watchdog(now) == []


def test_status_reports_queue_wait_and_read_times():
    w = _fake_streams(1)
    w.waits.extend([0.1] * 9 + [4.0])
    w.fetch_s.extend([0.5] * 9 + [3.0])
    txt = w.status()
    assert "queue wait p50 0.1s p90 4.0s" in txt and "transaction read p50 0.5s p90 3.0s" in txt


def test_a_locked_save_does_not_lose_the_swaps_on_retry(env, monkeypatch):
    """Audit round 3 #9: the retried poll used to see its own claimed signatures as handled and drop them."""
    import sqlite3

    s, conn, ex, state = env
    store.add_wallets(conn, [W], "manual")
    conn.execute("UPDATE ct_wallets SET last_sig='sigOld' WHERE address=?", (W,))
    tracker._seen.clear()
    tracker._seen["sigWsBusy"] = None  # the websocket is handling another transaction right now
    def tx_of(sig):
        tx = parsed_tx(-3_000_000_000, 0, 1_000_000)
        tx["transaction"]["signatures"] = [sig]
        return tx

    rpc, _ = _helius_rpc(monkeypatch, tx_of)
    real, n = store.save_swaps, {"calls": 0}

    def flaky(*a, **kw):
        n["calls"] += 1
        if n["calls"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return real(*a, **kw)

    monkeypatch.setattr(store, "save_swaps", flaky)
    monkeypatch.setattr(store.time, "sleep", lambda x: None)
    row = conn.execute("SELECT * FROM ct_wallets WHERE address=?", (W,)).fetchone()
    got = store.retry_locked(tracker.poll, conn, s, rpc, row, "live")
    assert {sw.signature for sw in got} == {"sigMid", "sigNew"} and n["calls"] == 2
    assert conn.execute("SELECT last_sig FROM ct_wallets").fetchone()[0] == "sigNew"
    assert "sigWsBusy" in tracker._seen  # only its own claims were handed back
    # stored but the cursor write failed: the swaps still go to the strategy, the next poll skips them
    tracker._seen.clear()
    conn.execute("UPDATE ct_wallets SET last_sig='sigOld' WHERE address=?", (W,))
    row = conn.execute("SELECT * FROM ct_wallets WHERE address=?", (W,)).fetchone()
    assert tracker.poll(conn, s, rpc, row, "live") == []


def test_decimals_are_learnt_from_reads_and_strict_mode_never_guesses():
    chain._decimals_cache.pop(MINT, None)
    tx = parsed_tx(-3_000_000_000, 0, 1_000_000)
    for b in tx["meta"]["preTokenBalances"] + tx["meta"]["postTokenBalances"]:
        b["uiTokenAmount"]["decimals"] = 9
    assert chain.extract_swap(tx, W) and chain._decimals_cache[MINT] == 9
    chain._decimals_cache.pop(PMINT, None)
    assert chain.swap_from_logs(_logs(_trade_log(1.0, 20e6)), WHALE, "sigP")
    assert chain._decimals_cache[PMINT] == 6

    class Down:
        n = 0

        def read(self, *a, **kw):
            Down.n += 1
            raise RuntimeError("429")

    chain._decimals_cache.pop("MintY", None)
    assert chain.token_decimals(Down(), "MintY") == 6 and Down.n == 2  # retried once, guess not cached
    with pytest.raises(chain.DecimalsUnknown):
        chain.token_decimals(Down(), "MintY", strict=True)
    assert "MintY" not in chain._decimals_cache


def test_rich_budget_sends_any_swap_wallet_straight_to_main(env, monkeypatch):
    s, conn, ex, state = env
    calls = []

    class Pub:
        counts, hosts = {}, {}

        def call(self, method, params, priority="bulk"):
            calls.append("public")
            raise RuntimeError("429")

    rpc = chain.Rpc(Settings(rpc_url="https://mainnet.helius-rpc.com/?api-key=k", poll_rpc_url=""))
    rpc.public = Pub()
    monkeypatch.setattr(chain.Rpc, "call", lambda self, m, p, priority="live": calls.append("main") or parsed_tx(
        -3_000_000_000, 0, 1_000_000))
    monkeypatch.setattr(tracker.time, "sleep", lambda x: None)
    monkeypatch.setattr(tracker, "main_budget_ok", lambda *a, **kw: True)
    store.add_wallets(conn, [W], "auto")  # unranked: no main credits on a tight budget
    store._winfo_cache.clear()
    tracker._seen.clear()
    n = tracker.MAIN_READS["early"]
    assert tracker.process_signature(conn, s, rpc, W, "sigN", 3)
    assert calls == ["main"] and tracker.MAIN_READS["early"] == n + 1


def test_main_budget_check_keeps_a_reserve(monkeypatch):
    from core import http as core_http

    monkeypatch.undo()  # the real check, with a faked ledger
    left = {"v": 2000.0}
    monkeypatch.setattr(core_http, "credits", lambda svc=None: {"credits_day_budget": 7500.0})
    monkeypatch.setattr(core_http, "credits_left_today", lambda svc=None: left["v"])
    tracker._budget_cache.update(at=0.0, ok=False)
    R = Settings(ws_main_reserve_pct=25.0)
    assert tracker.main_budget_ok(R, now=1000.0)  # 2000 > 25 % of 7500
    left["v"] = 1800.0
    assert tracker.main_budget_ok(R, now=1010.0)  # cached for 30 s
    assert not tracker.main_budget_ok(R, now=1031.0)
    left["v"] = None
    assert not tracker.main_budget_ok(R, now=1200.0)  # no budget known: stay lean
    tracker._budget_cache.update(at=0.0, ok=False)
    # default since 18.10.: no reserve, Helius until the ledger says the credits are spent
    left["v"] = 10.0
    assert tracker.main_budget_ok(Settings(), now=2000.0)
    left["v"] = 0.0
    assert not tracker.main_budget_ok(Settings(), now=2100.0)
    tracker._budget_cache.update(at=0.0, ok=False)


def test_helius_stream_is_metered_paused_below_the_reserve_and_never_shows_the_key(monkeypatch):
    from copytrade import watch
    from core import http as core_http

    booked = []
    monkeypatch.setattr(core_http, "_charge", lambda cost, force=False: booked.append(cost))
    s = Settings(ws_urls=["ws://n0"], ws_parallel=1, helius_api_key="SECRETKEY", ws_helius=True)
    w = watch.Watcher(s, None, {}, threading.Event())
    h = w.helius
    assert h is not None and h in w.streams and h.max_wallets == s.ws_helius_max and not h.complement
    w.set_wallets([W, "B"])
    assert h._wanted == [W, "B"]
    h.bytes = 250_000  # 2.5 x 0.1 MB
    monkeypatch.setattr(tracker, "main_budget_ok", lambda *a, **kw: True)
    w.meter()
    assert booked == [5.0] and w.helius_credits == 5.0 and not w.helius_paused
    monkeypatch.setattr(tracker, "main_budget_ok", lambda *a, **kw: False)
    w.meter()
    assert w.helius_paused and h._wanted == [] and booked == [5.0]  # nothing new streamed, nothing booked
    w.set_wallets([W])
    assert h._wanted == []  # stays empty while paused
    monkeypatch.setattr(tracker, "main_budget_ok", lambda *a, **kw: True)
    w.meter()
    assert not w.helius_paused and h._wanted == [W]
    h.connected = True
    txt = w.status()
    assert "SECRETKEY" not in txt and "mainnet.helius-rpc.com" in txt and "credits" in txt

    def refuse(cost, force=False):
        raise RuntimeError("used up")

    monkeypatch.setattr(core_http, "_charge", refuse)
    h.bytes += 100_000
    w.meter()
    assert "credits" in w.helius_paused and h._wanted == []
    assert ws._redact("connect wss://x/?api-key=SECRETKEY failed") == "connect wss://x/?api-key=*** failed"


def test_no_helius_stream_without_a_key_or_when_switched_off():
    from copytrade import watch

    assert watch.Watcher(Settings(ws_urls=["ws://n0"], ws_parallel=1, helius_api_key=""), None, {},
                         threading.Event()).helius is None
    assert watch.Watcher(Settings(ws_urls=["ws://n0"], ws_parallel=1, helius_api_key="K", ws_helius=False), None, {},
                         threading.Event()).helius is None


def test_catch_up_is_skipped_when_another_stream_watched_the_wallet_all_along():
    w = _fake_streams()
    a, b = w.streams
    now = time.time()
    a.sub_at[W] = now - 300
    b.sub_at[W] = now  # b just resubscribed and asks for a catch-up
    assert w.covered_elsewhere(W, now)
    a.connected = False
    assert not w.covered_elsewhere(W, now)


def test_notifications_without_a_swap_venue_never_spend_main_credits(env, monkeypatch):
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
    monkeypatch.setattr(tracker, "main_budget_ok", lambda *a, **kw: True)
    store.add_wallets(conn, [W], "manual")  # a key wallet: would get main credits on the last try
    tracker._seen.clear()
    plain = ["Program 11111111111111111111111111111111 invoke [1]",
             "Program 11111111111111111111111111111111 success"]
    assert chain.log_venues(plain) == set()
    assert chain.log_venues(plain[:1] + ["Program pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA invoke [2]"]) == {"pumpswap"}
    assert chain.log_venues(None) is None and chain.log_venues(["Log truncated"]) is None
    from copytrade import watch

    w = watch.Watcher(Settings(ws_urls=["ws://n0"], ws_parallel=1), rpc, {}, threading.Event())
    w.s = s
    w.fetch(conn, W, "sigT", time.time(), plain)
    assert calls == ["public"] * 3 and w.lean == 1  # a transfer: public pool only, even for a manual wallet
    calls.clear()
    w.fetch(conn, W, "sigS", time.time(), plain + ["Program 6EF8rrDYGrSquN2vUbW2kH7JMLtQFZyD3J4bLy1F2uY3 invoke [1]"])
    assert "main" in calls and w.lean == 1


def test_block0_group_is_resolved_watched_first_and_never_pruned_as_bots(env):
    s, conn, ex, state = env
    full = "52bZXsA9XHvWP5JYgmt1r8GQJ5bse3FK5ALf6j3pVu4e"
    seen = "3dy8rcQbtuyi5KHDM69oVuFsUwsw98xopsRsRF3Qabcd"
    twin_a, twin_b = "9ZYMMXaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa11", "9ZYMMXbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb22"
    conn.executemany("INSERT INTO ct_swaps(sig, wallet, mint, side, ts) VALUES (?, ?, 'm', 'buy', 1)",
                     [("s1", seen), ("s2", twin_a), ("s3", twin_b), ("s4", "3dY8rcLowerCaseMustNotMatchxxxxxxxxxxxxxxxx")])
    store.add_wallets(conn, [f"Aaaa{i:040d}"[:44] for i in range(3)], "auto")
    tracker._BLOCK0_FOUND.clear()
    tracker._BLOCK0_TRIED["at"] = 0.0
    s.block0_wallets = [full, "3dy8rc…abcd", "9ZYMMX", "AopdiF…Qtke"]
    got = tracker.load_block0(conn, s)
    assert got == [full, seen]  # 9ZYMMX is ambiguous, AopdiF unseen: both wait
    assert tracker.BLOCK0 == {full, seen}
    rows = dict(conn.execute("SELECT address, source FROM ct_wallets WHERE source='block0'").fetchall())
    assert set(rows) == {full, seen}
    store._winfo_cache.clear()
    assert set(tracker.ws_wallets(conn, s)[:2]) == {full, seen}  # watched right after manual
    # a block-0 wallet trades in bursts: it must not be switched off as a bot or by the ranking prune
    class Burst:
        def signatures(self, w, limit=80, **kw):
            return [{"signature": f"x{i}", "blockTime": 2000 - i} for i in range(limit)]  # 80 tx in 80 s, newest first

    tracker.backfill(conn, s, Burst(), full)
    plain = f"Aaaa{0:040d}"[:44]
    tracker.backfill(conn, s, Burst(), plain)
    act = dict(conn.execute("SELECT address, active FROM ct_wallets").fetchall())
    assert act[full] == 1 and act[plain] == 0
    # the group's detection lag is reported on its own
    tracker.LAG.clear()
    sw = chain.Swap("sigB0", full, int(time.time()) - 2, "buy", "m", 1.0, 0.01, 0.01)
    tracker.record_detection(conn, sw, "ws")
    assert tracker.lag_summary(0)["block0"]["n"] == 1
    tracker.LAG.clear()
    tracker.BLOCK0.clear()


def test_every_recognised_buy_is_offered_as_an_opportunity_with_feed_features(env):
    from copytrade import opportunity

    s, conn, ex, state = env
    got = []
    opportunity.set_sink(lambda c, f: got.append(f))
    try:
        store.add_wallets(conn, [W], "manual")
        conn.execute("INSERT INTO ct_wallet_rank(address, samples, hit_rate, expected_value, score) "
                     "VALUES (?, 12, 0.4, 0.08, 0.6)", (W,))
        sw = chain.Swap("sigO", W, int(time.time()) - 3, "buy", "m", 1000.0, 0.5, 0.0005, dex="pump", mcap_sol=300.0)
        tracker.record_detection(conn, sw, "ws", notified_at=time.time() - 1, sol_usd=100.0)
        f = got[0]
        assert f["kind"] == "whale_buy" and f["detect_path"] == "ws" and 2.5 < f["lag_s"] < 30
        assert f["group"] == "manual" and f["usd"] == 50.0 and f["mcap_usd"] == 30000.0 and f["dex"] == "pump"
        assert f["rank_samples"] == 12 and f["rank_ev"] == 0.08
        tracker.record_detection(conn, chain.Swap("sigS", W, int(time.time()), "sell", "m", 1.0, 0.5, 0.5), "ws")
        assert len(got) == 1  # sells are not opportunities

        def broken(c, f):
            raise RuntimeError("table missing")

        opportunity.set_sink(broken)
        tracker.record_detection(conn, chain.Swap("sigX", W, int(time.time()), "buy", "m", 1.0, 0.5, 0.5), "ws")
    finally:
        opportunity.set_sink(None)


def test_buys_land_in_the_brains_opportunity_table(env):
    from copytrade import opportunity

    s, conn, ex, state = env
    opportunity.set_sink("auto")
    tracker._B0_LAST.clear()
    tracker.B0_REPEAT.clear()
    try:
        tracker.BLOCK0.add(W)
        sw = chain.Swap("sigQ", W, int(time.time()) - 4, "buy", "m", 1000.0, 0.005, 0.000005, dex="pump", mcap_sol=50.0)
        tracker.record_detection(conn, sw, "ws", sol_usd=100.0)
        tracker.record_detection(conn, sw, "ws", sol_usd=100.0)  # a repeat is ignored
        rows = conn.execute("SELECT source, ref, mint, src_ts, mcap_usd, features FROM opportunities").fetchall()
        assert len(rows) == 1 and rows[0]["source"] == "copytrade" and rows[0]["ref"] == f"sigQ:{W}"
        f = json.loads(rows[0]["features"])
        assert f["dex"] == "pumpfun" and f["copytrade.block0"] is True and f["copytrade.group"] == "block0"
        assert f["mc_usd"] == 5000.0 and 3 < f["delay_s"] < 30 and f["copytrade.size_sol"] == 0.005
        # a buy the polling net found minutes later is history, not an opportunity: not sent
        old = chain.Swap("sigOld", W, int(time.time()) - 900, "buy", "m2", 1000.0, 0.005, 0.000005, dex="pump")
        stale = opportunity.SKIPPED["stale"]
        tracker.record_detection(conn, old, "gap", sol_usd=100.0)
        assert opportunity.SKIPPED["stale"] == stale + 1
        assert conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0] == 1
        # the same block-0 bot buying the same coin again: stored, but neither decided nor offered again
        again = chain.Swap("sigQ2", W, int(time.time()) - 2, "buy", "m", 1000.0, 0.005, 0.000005, dex="pump")
        tracker.record_detection(conn, again, "ws", sol_usd=100.0)
        assert "sigQ2" in tracker.B0_REPEAT and "sigQ" not in tracker.B0_REPEAT
        assert conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0] == 1
        from copytrade import watch

        class Strat:
            def on_whale_buy(self, sw):
                raise AssertionError("a repeat must not reach the strategy")

        watch.handle_swap(Strat(), again)
        # another wallet of the group in the same coin: the group buys together, one report per coin
        W3 = "B0two" + "z" * 39
        tracker.BLOCK0.add(W3)
        mate = chain.Swap("sigQ3", W3, int(time.time()) - 2, "buy", "m", 1000.0, 0.005, 0.000005, dex="pump")
        tracker.record_detection(conn, mate, "ws", sol_usd=100.0)
        assert "sigQ3" in tracker.B0_REPEAT
        assert conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0] == 1
        tracker.BLOCK0.discard(W3)
    finally:
        tracker.BLOCK0.discard(W)
        opportunity.set_sink(None)


def _venue_logs(prog: str | None) -> list[str]:
    from copytrade import chain

    pid = next(iter(chain.DEX_PROGRAMS)) if prog == "dex" else "11111111111111111111111111111111"
    return [f"Program {pid} invoke [1]", f"Program {pid} success"]


def test_whale_notifications_jump_ahead_and_venueless_traffic_is_never_read(monkeypatch):
    from copytrade import watch

    B0 = "B0bot1111111111111111111111111111111111111"
    monkeypatch.setattr(tracker, "BLOCK0", {B0})
    q = watch.EventLanes()
    now = time.time()
    tracker._seen.clear()
    q.put((B0, "b0-transfer", now, _venue_logs(None)))  # launch bot, no swap venue: never read
    q.put((B0, None, now, "catchup"))  # polling covers the launch bots
    q.put((B0, "b0-swap", now, _venue_logs("dex")))
    q.put((W, "w-transfer", now, _venue_logs(None)))
    q.put((W, None, now, "catchup"))
    q.put((W, "w-swap", now, _venue_logs("dex")))
    q.put((W, "w-nologs", now, None))
    order = [q.get(timeout=1)[1] for _ in range(4)]
    assert order == ["w-swap", "w-nologs", None, "b0-swap"] and q.empty()
    assert q.dropped == {"block0_noswap": 1, "block0_catchup": 1, "noswap": 1}
    # dropped ones count as handled: the polling net does not read them either
    assert not tracker._mark("b0-transfer") and not tracker._mark("w-transfer") and tracker._mark("w-swap")
    for i in range(watch.EventLanes.LOW_MAX + 5):  # the bulk lane is bounded, the whale lane never waits on it
        q.put((B0, f"b{i}", now, _venue_logs("dex")))
    q.put((W, "w2", now, None))
    assert q.get(timeout=1)[1] == "w2" and q.dropped["overflow"] == 5 and q.qsize() == watch.EventLanes.LOW_MAX
    with pytest.raises(queue.Empty):
        watch.EventLanes().get(timeout=0.05)


def test_noisy_wallets_leave_the_paid_helius_stream_for_a_while(tmp_path, monkeypatch):
    from copytrade import watch

    B0 = "B0bot1111111111111111111111111111111111111"
    monkeypatch.setattr(tracker, "BLOCK0", {B0})
    s = Settings(db_path=tmp_path / "h.db", ws_urls=["ws://n0"], ws_parallel=1)
    w = watch.Watcher(s, None, {}, threading.Event())
    w.helius = w.streams[0].__class__(s, url="ws://helius", events=w.events, name="wallet-ws-helius")
    w.streams.append(w.helius)
    w.set_wallets([W, B0])
    assert w.helius._wanted == [W, B0]  # block-0 rides Helius like everyone else
    now = time.time()
    for i in range(41):
        w.helius.notes.append((now - i, B0, 100_000))
    w.helius.notes.append((now, W, 3_000_000))  # a few big notifications stay under 4 MB
    assert w.bench_noisy(now) == [B0]
    assert w.streams[0]._wanted == [W, B0] and w.helius._wanted == [W]  # the public stream keeps it
    assert "1 noisy wallets benched" in w.status()
    w.set_wallets([W, B0])
    assert w.helius._wanted == [W]
    w.helius.notes.clear()
    w.bench_noisy(now + watch.HELIUS_BENCH_S + 1)
    assert not w.benched
    w.helius_paused = "x"
    w.set_wallets([W, B0])
    assert w.helius._wanted == []


def test_a_wallet_at_bot_pace_is_switched_off_instead_of_read_sixty_times(env, monkeypatch):
    s, conn, ex, state = env
    store.add_wallets(conn, [W], "auto")
    conn.execute("UPDATE ct_wallets SET last_sig='old' WHERE address=?", (W,))
    now = int(time.time())
    sigs = [{"signature": f"b{i}", "blockTime": now - i * 5, "err": None} for i in range(60)]  # 60 in 5 min
    reads = []

    class R:
        def poll_signatures(self, *a, **kw):
            return sigs

        def transaction(self, sig, **kw):
            reads.append(sig)

    row = conn.execute("SELECT * FROM ct_wallets WHERE address=?", (W,)).fetchone()
    assert tracker.poll(conn, s, R(), row) == [] and reads == []
    r = conn.execute("SELECT active, label, last_sig FROM ct_wallets WHERE address=?", (W,)).fetchone()
    assert (r["active"], r["label"], r["last_sig"]) == (0, "bot", "b0")
    # a busy whale spread over an hour stays, and so does a manual wallet at bot pace
    assert not tracker._burst_bot(conn, {"address": "X", "source": "auto"},
                                  [{"signature": f"c{i}", "blockTime": now - i * 60} for i in range(60)])
    assert not tracker._burst_bot(conn, {"address": "Y", "source": "manual"}, sigs)


def test_transaction_and_signature_reads_go_to_helius_first_within_a_share(monkeypatch):
    calls = []

    class Pub:
        counts, hosts = {}, {}

        def call(self, method, params, priority="bulk"):
            calls.append("public")
            return "pub"

    rpc = chain.Rpc(Settings(rpc_url="https://mainnet.helius-rpc.com/?api-key=k", poll_rpc_url=""))
    rpc.public = Pub()
    main = {"fail": False}

    def fake_call(self, m, p, priority="live"):
        calls.append("main")
        if main["fail"]:
            raise RuntimeError("429")
        return "main"

    monkeypatch.setattr(chain.Rpc, "call", fake_call)
    assert rpc.read("getTransaction", []) == "pub" and calls == ["public"]  # no main_first: public as before
    rpc.main_first = lambda: True
    calls.clear()
    assert rpc.read("getTransaction", []) == "main" and rpc.read("getSignaturesForAddress", []) == "main"
    assert rpc.read("getTokenSupply", []) == "pub"  # other methods unchanged
    main["fail"] = True
    assert rpc.read("getTransaction", []) == "pub"  # Helius error: public fallback
    main["fail"] = False
    rpc._mf_off_until = 0.0
    monkeypatch.setattr(chain.Rpc, "MAIN_FIRST_PER_MIN", 3)
    calls.clear()
    assert rpc.read("getTransaction", []) == "pub" and calls == ["public"]  # share of the minute used up
    rpc.main_first = lambda: False
    rpc._mf.clear()
    assert rpc.read("getTransaction", []) == "pub"
    # a refused key (credits used up, 429/401/403) is left alone for a while: public only, no try per read
    monkeypatch.setattr(chain.Rpc, "MAIN_FIRST_PER_MIN", 160)
    rpc._mf_off_until = 0.0
    rpc.main_first = lambda: True
    main["fail"] = True
    calls.clear()
    assert rpc.read("getTransaction", []) == "pub" and rpc.read("getTransaction", []) == "pub"
    assert calls == ["main", "public", "public"]
    rpc._mf_off_until = 0.0

    def exhausted(self, m, p, priority="live"):
        calls.append("main")
        raise chain.CreditsExhausted("used up")

    monkeypatch.setattr(chain.Rpc, "call", exhausted)
    calls.clear()
    assert rpc.read("getSignaturesForAddress", []) == "pub" and rpc.read("getTransaction", []) == "pub"
    assert calls == ["main", "public", "public"] and rpc._mf_off_until > time.time() + 200


def test_old_poll_finds_stay_out_of_the_lag_and_the_detection_table(env):
    s, conn, ex, state = env
    tracker.LAG.clear()
    tracker.BACKFILL.clear()
    w = "Wold" + "x" * 40
    store.add_wallets(conn, [w], "auto")
    now = int(time.time())

    class R:
        def poll_signatures(self, wallet, limit=50, **kw):  # newest first
            return [{"signature": "new1", "blockTime": now - 5}, {"signature": "old1", "blockTime": now - 4000}]

        def transaction(self, sig, priority="bulk"):
            return sig

    old_extract = chain.extract_swap
    chain.extract_swap = lambda tx, wallet, solusd=0.0: chain.Swap(
        tx, wallet, now - (5 if tx == "new1" else 4000), "buy", "m", 1.0, 0.01, 0.01)
    try:
        row = conn.execute("SELECT * FROM ct_wallets WHERE address=?", (w,)).fetchone()
        conn.execute("UPDATE ct_wallets SET last_sig='before' WHERE address=?", (w,))
        row = conn.execute("SELECT * FROM ct_wallets WHERE address=?", (w,)).fetchone()
        got = tracker.poll(conn, s, R(), row)
    finally:
        chain.extract_swap = old_extract
    assert {sw.signature for sw in got} == {"new1", "old1"}  # both still reach the strategy and ct_swaps
    assert [r[0] for r in conn.execute("SELECT sig FROM ct_detect")] == ["new1"]
    assert sum(tracker.BACKFILL.values()) == 1
    tracker.LAG.clear()
    tracker.BACKFILL.clear()


def test_volume_bots_are_never_block0_and_switched_off(env):
    s, conn, ex, state = env
    bot = "9ZYMMXvVPSfq6JdUjXBv9yCo1J1XbphPQMbWS5oDThJ"
    full = "52bZXsA9XHvWP5JYgmt1r8GQJ5bse3FK5ALf6j3pVu4e"
    assert bot not in Settings().block0_wallets and bot in Settings().volume_bots
    store.add_wallets(conn, [bot], "block0", label="block0")
    s.block0_wallets, s.volume_bots = [full, bot], [bot]
    assert tracker.load_block0(conn, s) == [full] and tracker.BLOCK0 == {full}
    assert tuple(conn.execute("SELECT active, label FROM ct_wallets WHERE address=?", (bot,)).fetchone()) == (0, "volume bot")
    tracker.BLOCK0.clear()
