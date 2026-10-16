"""live/ offline: guard limits, dry run, real-send flow against a fake RPC and fake Jupiter, late landings,
sell retries with on-chain balance, token-account close, Telegram commands, go-live check."""
import base64
import time

import pytest

pytest.importorskip("solders")
from solders.hash import Hash  # noqa: E402
from solders.keypair import Keypair  # noqa: E402
from solders.message import MessageV0  # noqa: E402
from solders.pubkey import Pubkey  # noqa: E402
from solders.signature import Signature  # noqa: E402
from solders.system_program import TransferParams, transfer  # noqa: E402
from solders.transaction import VersionedTransaction  # noqa: E402

from copytrade import chain, control, market, store as ct_store, strategy  # noqa: E402
from copytrade.config import SOL_MINT, Settings  # noqa: E402
from copytrade.execution import PaperExecutor  # noqa: E402
from live import guard, readiness, service, store  # noqa: E402
from live.config import LiveConfig  # noqa: E402
from live.executor import GuardedLiveExecutor, parse_deltas  # noqa: E402

MINT = "Meme1111111111111111111111111111111111pump"
TOKEN_PROG = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"


def cfg(**kw):
    c = LiveConfig(enabled=True, dry_run=False, stage_no=1, require_gate=False)
    for k, v in kw.items():
        object.__setattr__(c, k, v)
    return c


class FakeJup:
    def __init__(self, kp, out=1_000_000_000, impact=0.01):
        self.kp, self.out, self.impact, self.quotes, self.fail_quote = kp, out, impact, [], False

    def quote(self, s, inp, outp, amount, slip):
        self.quotes.append((inp, outp, amount, slip))
        if self.fail_quote:
            return None
        out = self.out if inp == SOL_MINT else int(amount * 1e-3)  # sell: 1000 raw token -> 1 lamport
        return {"outAmount": str(out), "otherAmountThreshold": str(int(out * (1 - slip / 1e4))),
                "priceImpactPct": str(self.impact), "routePlan": [{"swapInfo": {"label": "Pump.fun"}}]}

    def swap_tx(self, s, q, pub, max_prio, level):
        ix = transfer(TransferParams(from_pubkey=self.kp.pubkey(), to_pubkey=Pubkey.new_unique(), lamports=1))
        msg = MessageV0.try_compile(self.kp.pubkey(), [ix], [], Hash.new_unique())
        tx = VersionedTransaction.populate(msg, [Signature.default()])
        return {"swapTransaction": base64.b64encode(bytes(tx)).decode(), "lastValidBlockHeight": 1000,
                "prioritizationFeeLamports": 200_000}


class FakeRpc:
    """Lands every sent transaction unless told otherwise; balances follow the swaps."""

    def __init__(self, owner):
        self.owner = owner
        self.lamports = 500_000_000
        self.tokens = 0
        self.height = 900
        self.sent, self.calls = [], []
        self.landing = "landed"  # landed | never | error | preflight
        self.txs, self.statuses = {}, {}
        self.side = None
        self.closed = 0

    def call(self, method, params, priority="live"):
        self.calls.append(method)
        if method == "getBalance":
            return {"value": self.lamports}
        if method == "getTokenSupply":
            return {"value": {"decimals": 6}}
        if method == "getBlockHeight":
            return self.height
        if method == "getLatestBlockhash":
            return {"value": {"blockhash": str(Hash.new_unique()), "lastValidBlockHeight": 1200}}
        if method == "getTokenAccountsByOwner":
            return {"value": [{"pubkey": str(Pubkey.new_unique()), "account": {
                "owner": TOKEN_PROG, "lamports": 2_039_280,
                "data": {"parsed": {"info": {"mint": MINT, "tokenAmount": {"amount": str(self.tokens),
                                                                           "decimals": 6}}}}}}]
                    if self.tokens or self.side else []}
        if method == "simulateTransaction":
            return {"value": {"err": None, "unitsConsumed": 80_000, "logs": []}}
        if method == "sendTransaction":
            tx = VersionedTransaction.from_bytes(base64.b64decode(params[0]))
            sig = str(tx.signatures[0])
            first = not params[1].get("skipPreflight")
            if first and self.landing == "preflight":
                raise RuntimeError("rpc sendTransaction: {'code': -32002, 'message': 'slippage'}")
            if first:
                self.sent.append(sig)
                self._land(sig, tx)
            return sig
        if method == "getSignatureStatuses":
            st = self.statuses.get(params[0][0])
            if st is None:
                self.height += 300  # time passes, the blockhash expires
            return {"value": [st]}
        if method == "getTransaction":
            return self.txs.get(params[0])
        raise AssertionError(method)

    def _land(self, sig, tx):
        if self.landing == "never":
            return
        if len(tx.message.instructions) and bytes(tx.message.instructions[0].data) == bytes([9]):
            self.closed += 1
            self.statuses[sig] = {"confirmationStatus": "confirmed", "err": None}
            return
        if self.landing == "error":
            self.statuses[sig] = {"confirmationStatus": "confirmed", "err": {"InstructionError": [2, "x"]}}
            return
        fee = 205_000
        if self.side == "sell":
            got = self.tokens // 1000
            pre_t, post_t, d_sol = self.tokens, 0, got - fee
        else:
            pre_t, post_t, d_sol = self.tokens, self.tokens + 990_000_000, -(100_000_000 + 2_039_280 + fee)
        pre_l = self.lamports
        self.lamports += d_sol
        self.tokens = post_t
        self.statuses[sig] = {"confirmationStatus": "confirmed", "err": None}
        self.txs[sig] = {"meta": {"err": None, "fee": fee, "preBalances": [pre_l], "postBalances": [self.lamports],
                                  "preTokenBalances": [{"owner": self.owner, "mint": MINT,
                                                        "uiTokenAmount": {"amount": str(pre_t)}}],
                                  "postTokenBalances": [{"owner": self.owner, "mint": MINT,
                                                         "uiTokenAmount": {"amount": str(post_t)}}]},
                         "transaction": {"message": {"accountKeys": [{"pubkey": self.owner}]}}}


def snap(price_sol=1e-4, age=0):
    return market.PairSnapshot(MINT, "MEME", "pool", "pumpswap", 1e-5, price_sol, 80_000, 1e6, 5000, 30000,
                               10, 2, 120, 1.0, 5.0, int(time.time() * 1000) - 3600_000, time.time() - age)


@pytest.fixture
def env(tmp_path, monkeypatch):
    s = Settings(db_path=tmp_path / "t.db")
    conn = ct_store.connect(s)
    store.init(conn)
    kp = Keypair()
    rpc = FakeRpc(str(kp.pubkey()))
    jup = FakeJup(kp)
    monkeypatch.setattr(chain, "token_decimals", lambda r, m: 6)
    monkeypatch.setattr(service, "send", lambda text: True)

    def make(c=None, dry=False, key=True):
        ex = GuardedLiveExecutor(s, rpc, c or cfg(), kp if key else None, str(kp.pubkey()), dry, conn=conn, jup=jup,
                                 sleep=lambda x: None)
        store.put(conn, "heartbeat", int(time.time()))
        return ex
    return s, conn, rpc, jup, make


def test_real_buy_books_landed_amounts(env):
    s, conn, rpc, jup, make = env
    ex = make()
    rpc.side = "buy"
    f = ex.buy(MINT, 0.5, snap())  # stage 1 caps at 0.1 SOL
    assert f.ok and f.source == "live"
    assert jup.quotes[0][2] == 100_000_000
    assert f.tokens == pytest.approx(990.0) and f.sol == pytest.approx(0.1 + 0.002039280 + 0.000205)
    o = store.recent(conn)[0]
    assert o["status"] == "landed" and o["realized_slippage"] == pytest.approx(0.01)
    assert o["priority_lamports"] == 200_000 and o["last_valid_height"] == 1000


def test_guard_limits(env):
    s, conn, rpc, jup, make = env
    c = cfg()
    store.put(conn, "heartbeat", int(time.time()))
    assert guard.check_buy(conn, c, "live", 1.0, 0.5) == (0.1, None)
    assert guard.check_buy(conn, c, "live", 0.1, 0.11)[0] == pytest.approx(0.08)  # reserve kept
    assert "Hauptvermögen" in guard.check_buy(conn, c, "live", 0.1, 50.0)[1]
    store.kill(conn, "test")
    assert "Not-Aus" in guard.check_buy(conn, c, "live", 0.1, 0.5)[1]
    store.unkill(conn)
    store.put(conn, "heartbeat", 0)
    assert "Wächter" in guard.check_buy(conn, c, "live", 0.1, 0.5)[1]
    assert guard.check_buy(conn, c, "dry", 0.1, 0.5)[1] is None  # dry run needs no watchdog
    store.put(conn, "heartbeat", int(time.time()))
    now = int(time.time())
    for i in range(2):
        conn.execute("INSERT INTO ct_positions(mode, strategy, mint, status, opened_at, entry_cost_sol, tokens, "
                     "last_price_sol) VALUES ('live','whale',?, 'open', ?, 0.1, 1000, 0.00005)", (f"m{i}", now))
    assert "offene" in guard.check_buy(conn, c, "live", 0.1, 0.5)[1]
    conn.execute("UPDATE ct_positions SET status='closed', pnl_sol=-0.2, closed_at=?", (now,))
    assert "Tagesverlust" in guard.check_buy(conn, c, "live", 0.1, 0.5)[1]  # -0.4 <= -0.3


def test_env_can_only_tighten():
    c = cfg(_pos=5.0, _open=1)
    assert c.max_position_sol == 0.1 and c.max_open == 1


def test_buy_rejected_by_impact_and_stale_price(env):
    s, conn, rpc, jup, make = env
    ex = make()
    jup.impact = 0.08
    f = ex.buy(MINT, 0.1, snap())
    assert not f.ok and "Preiseinfluss" in f.error and not rpc.sent
    jup.impact = 0.01
    f = ex.buy(MINT, 0.1, snap(price_sol=5e-4))  # quote 1e-4 vs market 5e-4: data is off
    assert not f.ok and "weicht" in f.error and not rpc.sent
    f = ex.buy(MINT, 0.1, snap(age=600))
    assert not f.ok and "alt" in f.error


def test_preflight_failure_sends_nothing_and_streak_kills(env):
    s, conn, rpc, jup, make = env
    ex = make()
    rpc.landing = "preflight"
    for _ in range(3):
        assert not ex.buy(MINT, 0.1, snap()).ok
    assert not rpc.sent
    f = ex.buy(MINT, 0.1, snap())
    assert "Fehlschläge" in f.error or "Not-Aus" in f.error
    assert store.killed(conn)


def test_expired_and_unknown(env):
    s, conn, rpc, jup, make = env
    ex = make()
    rpc.landing = "never"
    f = ex.buy(MINT, 0.1, snap())
    assert not f.ok and store.recent(conn)[0]["status"] == "expired"
    assert not store.pending(conn)


def test_dry_run_never_sends_and_books_dry(env):
    s, conn, rpc, jup, make = env
    ex = make(c=cfg(dry_run=True), dry=True)
    assert ex.mode == "dry"
    paper = PaperExecutor(s, rpc)
    st = strategy.Strategy(conn, s, {"paper": paper, "live": ex, "dry": ex})
    control.set_live(conn, "signal", True)
    sn = snap()
    p = control.preset(conn, "signal")
    pid = st.open("signal", "auto", MINT, 0.1, sn, p, tp=0.5, hold=60, reason="test")
    assert pid and "sendTransaction" not in rpc.calls and "simulateTransaction" in rpc.calls
    row = conn.execute("SELECT * FROM ct_positions WHERE id=?", (pid,)).fetchone()
    assert row["mode"] == "dry" and row["entry_tx"].startswith("dry-")
    assert ex.balance() == pytest.approx(1.0 - 0.1)
    assert st.close(row, sn, "target")
    row = conn.execute("SELECT * FROM ct_positions WHERE id=?", (pid,)).fetchone()
    assert row["status"] == "closed" and row["mode"] == "dry"
    assert [o["status"] for o in store.recent(conn)] == ["dry_built", "dry_ok"]
    assert "sendTransaction" not in rpc.calls


def test_real_roundtrip_sells_onchain_balance_and_closes_account(env):
    s, conn, rpc, jup, make = env
    ex = make()
    rpc.side = "buy"
    st = strategy.Strategy(conn, s, {"paper": PaperExecutor(s, rpc), "live": ex})
    control.set_live(conn, "signal", True)
    sn = snap()
    pid = st.open("signal", "auto", MINT, 0.1, sn, control.preset(conn, "signal"), tp=0.5, hold=60, reason="t")
    row = conn.execute("SELECT * FROM ct_positions WHERE id=?", (pid,)).fetchone()
    assert row["mode"] == "live" and row["tokens"] == pytest.approx(990.0)
    rpc.side = "sell"
    rpc.tokens = 985_000_000  # on chain: less than booked (transfer fee); we sell what is there
    assert st.close(row, sn, "target")
    sell = [o for o in store.recent(conn) if o["side"] == "sell"][0]
    assert sell["in_raw"] == 985_000_000
    assert rpc.closed == 1  # empty token account closed, rent back
    row = conn.execute("SELECT * FROM ct_positions WHERE id=?", (pid,)).fetchone()
    expected = (985_000 - 205_000 + 2_039_280 - 5000) / 1e9
    assert row["proceeds_sol"] == pytest.approx(expected)


def test_sell_retries_with_more_slippage_then_backs_off(env):
    s, conn, rpc, jup, make = env
    ex = make(c=cfg(sell_attempts=3))
    rpc.tokens = 1_000_000
    rpc.landing = "preflight"
    f = ex.sell(MINT, 1.0, 6, None, 0)
    assert not f.ok
    slips = [q[3] for q in jup.quotes]
    assert slips == [500, 1000, 2000]
    f = ex.sell(MINT, 1.0, 6, None, 0)
    assert "pausiert" in f.error and len(jup.quotes) == 3  # backoff: no new attempt right away


def test_late_landed_sell_is_booked_not_sold_twice(env):
    s, conn, rpc, jup, make = env
    ex = make()
    oid = store.new_order(conn, dry=False, side="sell", mint=MINT, in_raw=1_000_000, status="unknown", sig="S1",
                          last_valid_height=10)
    rpc.statuses["S1"] = {"confirmationStatus": "confirmed", "err": None}
    rpc.owner = ex.pubkey
    rpc.txs["S1"] = {"meta": {"err": None, "fee": 5000, "preBalances": [1], "postBalances": [1 + 50_000_000 - 5000],
                              "preTokenBalances": [{"owner": ex.pubkey, "mint": MINT,
                                                    "uiTokenAmount": {"amount": "1000000"}}],
                              "postTokenBalances": []},
                     "transaction": {"message": {"accountKeys": [{"pubkey": ex.pubkey}]}}}
    f = ex.sell(MINT, 1.0, 6, None, 0)
    assert f.ok and f.source == "live_late" and f.sol == pytest.approx(0.049995)
    assert not rpc.sent
    assert conn.execute("SELECT status FROM live_orders WHERE id=?", (oid,)).fetchone()[0] == "landed"


def test_parse_deltas_wsol_and_fee():
    tx = {"meta": {"err": None, "fee": 5000, "preBalances": [10], "postBalances": [10 - 2_039_280 - 5000],
                   "preTokenBalances": [{"owner": "A", "mint": SOL_MINT, "uiTokenAmount": {"amount": "100000000"}}],
                   "postTokenBalances": [{"owner": "A", "mint": SOL_MINT, "uiTokenAmount": {"amount": "0"}},
                                         {"owner": "A", "mint": MINT, "uiTokenAmount": {"amount": "777"}}]},
          "transaction": {"message": {"accountKeys": ["A"]}}}
    d = parse_deltas(tx, "A", MINT)
    assert d == {"token_raw": 777, "sol_lamports": -100_000_000 - 2_039_280 - 5000, "fee": 5000}


def test_telegram_commands(env, monkeypatch):
    s, conn, rpc, jup, make = env
    c = cfg()
    assert "Not-Aus" in service.handle_command(conn, c, "/notaus")
    assert store.killed(conn)
    now = int(time.time())
    conn.execute("INSERT INTO ct_positions(mode, strategy, mint, status, opened_at, entry_cost_sol, tokens) "
                 "VALUES ('live','whale','m1','open',?,0.1,10)", (now,))
    assert "1 Position" in service.handle_command(conn, c, "/alles_verkaufen")
    assert conn.execute("SELECT COUNT(*) FROM ct_commands WHERE action='sell' AND status='pending'").fetchone()[0] == 1
    service.handle_command(conn, c, "/weiter")
    assert not store.killed(conn)
    assert "/notaus" in service.handle_command(conn, c, "hallo")


def test_service_reconcile_and_data_outage(env, monkeypatch):
    s, conn, rpc, jup, make = env
    make()
    svc = service.Service(conn=conn, rpc=rpc, cfg=cfg())
    old = int(time.time()) - 600
    a = store.new_order(conn, dry=False, side="buy", mint=MINT, in_raw=1, status="unknown", sig="B1",
                        last_valid_height=950)
    b = store.new_order(conn, dry=False, side="sell", mint=MINT, in_raw=1, status="unknown", sig="S2",
                        last_valid_height=950)
    conn.execute("UPDATE live_orders SET ts=?", (old,))
    rpc.statuses["B1"] = {"confirmationStatus": "confirmed", "err": None}
    rpc.height = 2000
    svc.reconcile()
    st = dict(conn.execute("SELECT id, status FROM live_orders").fetchall())
    assert st[a] == "landed_unbooked" and st[b] == "expired"
    from live import jupiter

    monkeypatch.setattr(jupiter, "quote", lambda *a, **k: None)
    for _ in range(3):
        svc.data_check()
    assert "Datenausfall" in guard.check_buy(conn, cfg(), "live", 0.1, 0.5)[1]
    monkeypatch.setattr(jupiter, "quote", lambda *a, **k: {"outAmount": "15000000"})
    svc.data_check()
    assert guard.check_buy(conn, cfg(), "live", 0.1, 0.5)[1] is None


def test_gate_blocks_real_buys_until_paper_and_dry_run_pass(env, monkeypatch):
    s, conn, rpc, jup, make = env
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    ex = make(c=cfg(require_gate=True))
    guard._gate_cache.clear()
    control.set_live(conn, "whale", True)
    f = ex.buy(MINT, 0.1, snap())
    assert not f.ok and "Go-Live" in f.error and not rpc.sent
    now = int(time.time())
    for i in range(40):  # profitable paper week, both halves positive
        conn.execute("INSERT INTO ct_positions(mode, strategy, mint, status, opened_at, closed_at, pnl_sol) "
                     "VALUES ('paper','whale',?, 'closed', ?, ?, ?)",
                     (f"p{i}", now - i * 15000, now - i * 15000, 0.05 if i % 3 else -0.04))
        conn.execute("INSERT INTO ct_positions(mode, strategy, mint, status, opened_at, closed_at, pnl_sol) "
                     "VALUES ('dry','whale',?, 'closed', ?, ?, 0)", (f"d{i}", now - 100, now - 50)) if i < 3 else None
    for i in range(5):
        store.new_order(conn, dry=True, side="buy", mint=MINT, in_raw=1, status="dry_ok")
    for i in range(3):
        store.new_order(conn, dry=True, side="sell", mint=MINT, in_raw=1, status="dry_built")
    rep = readiness.report(conn, cfg(require_gate=True))
    assert rep["ready"], [c for c in rep["checks"] if not c["ok"]]
    guard._gate_cache.clear()
    rpc.side = "buy"
    assert ex.buy(MINT, 0.1, snap()).ok


def test_open_real_positions_keep_a_real_seller(env, monkeypatch, tmp_path):
    s, conn, rpc, jup, make = env
    import json as _json

    from live import executor as lx
    kp = Keypair()
    kf = tmp_path / "k.json"
    kf.write_text(_json.dumps(list(bytes(kp))))
    s.keypair_path = str(kf)
    monkeypatch.setenv("LIVE_TRADING", "false")
    assert lx.make_executor(s, rpc) is None  # nothing open, live off: no live executor at all
    conn.execute("INSERT INTO ct_positions(mode, strategy, mint, status, opened_at, entry_cost_sol, tokens) "
                 "VALUES ('live','whale','m1','open',?,0.1,10)", (int(time.time()),))
    ex = lx.make_executor(s, rpc)
    assert ex is not None and ex.mode == "live" and ex.exit_only
    assert not ex.buy(MINT, 0.1, snap()).ok
    monkeypatch.setenv("LIVE_TRADING", "true")
    monkeypatch.setenv("LIVE_DRY_RUN", "true")
    ex = lx.make_executor(s, rpc)
    assert ex.mode == "live" and "Dry-Run" in ex.exit_only


def test_exit_value_from_quote(env):
    s, conn, rpc, jup, make = env
    ex = make()
    v = ex.exit_value(MINT, 1000.0, 6)  # 1e9 raw -> 1e6 lamports in the fake
    assert v and v[0] == pytest.approx(0.001)
    jup.fail_quote = True
    assert ex.exit_value(MINT, 1000.0, 6) is None
