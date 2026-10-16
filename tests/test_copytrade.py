"""Copy-trader logic tests, offline (market calls are stubbed). Run: python -m pytest tests/test_copytrade.py"""
import time

import pytest

from copytrade import chain, market, ranking, stats, store, strategy
from copytrade.config import SOL_MINT, Settings
from copytrade.execution import PaperExecutor

W = "Wha1e1111111111111111111111111111111111111"
MINT = "Meme1111111111111111111111111111111111pump"


def parsed_tx(sol_delta_lamports, token_pre, token_post, fee=5000, payer=W, err=None):
    return {
        "blockTime": int(time.time()) - 5,
        "meta": {
            "err": err, "fee": fee,
            "preBalances": [10_000_000_000, 1], "postBalances": [10_000_000_000 + sol_delta_lamports - fee, 1],
            "preTokenBalances": [{"owner": W, "mint": MINT, "uiTokenAmount": {"uiAmount": token_pre}}],
            "postTokenBalances": [{"owner": W, "mint": MINT, "uiTokenAmount": {"uiAmount": token_post}}],
        },
        "transaction": {"signatures": ["sig1"],
                        "message": {"accountKeys": [{"pubkey": payer}, {"pubkey": "pool"}]}},
    }


def test_extract_buy_and_sell():
    b = chain.extract_swap(parsed_tx(-3_000_000_000, 0, 1_000_000), W)
    assert b.side == "buy" and b.mint == MINT and b.sol_amount == pytest.approx(3.0)
    assert b.price_sol == pytest.approx(3e-6)
    s = chain.extract_swap(parsed_tx(4_000_000_000, 1_000_000, 0), W)
    assert s.side == "sell" and s.sol_amount == pytest.approx(4.0)
    assert chain.extract_swap(parsed_tx(-3_000_000_000, 0, 1, err={"x": 1}), W) is None


def test_extract_wsol_leg():
    tx = parsed_tx(-2_039_280, 0, 500)  # only ATA rent in native SOL
    tx["meta"]["preTokenBalances"].append({"owner": W, "mint": SOL_MINT, "uiTokenAmount": {"uiAmount": 5.0}})
    tx["meta"]["postTokenBalances"].append({"owner": W, "mint": SOL_MINT, "uiTokenAmount": {"uiAmount": 0.0}})
    sw = chain.extract_swap(tx, W)
    assert sw.side == "buy" and sw.sol_amount == pytest.approx(5.002, abs=1e-3)


def candles_pump(t0, base=1.0):
    """quiet 15 min, whale buys, volume spike after 3 min, top +80% at 10 min, then dump"""
    out = []
    for m in range(-15, 60):
        ts = t0 - t0 % 60 + m * 60
        if m <= 0:
            p, v = base, 100
        elif m <= 10:
            p, v = base * (1 + 0.08 * m), 100 if m < 3 else 2000
        else:
            p, v = base * 1.8 * (0.93 ** (m - 10)), 800
        out.append((ts, p, p * 1.01, p * 0.99, p, v))
    return out


def test_evaluate_buy_measures_pump_and_lead():
    s = Settings()
    t0 = 1_700_000_000
    r = ranking.evaluate_buy(candles_pump(t0), t0, s)
    assert r["pumped"] == 1
    assert 0.7 < r["first_pump_gain"] < 0.85
    assert 8 <= r["first_pump_min"] <= 11
    assert r["inflow_lead_min"] == pytest.approx(3, abs=1)
    assert r["achievable_gain"] > 0.3  # trailing stop catches most of the first pump


def test_simulate_exit_stop_first():
    c = [(0, 1, 1.0, 0.7, 0.75, 1)]
    g, _, why = ranking.simulate_exit(c, 1.0, take_profit=0.5, stop_loss=0.2, trail_activate=0.15,
                                      trail_drawdown=0.1, max_hold_min=60)
    assert why == "stop" and g == pytest.approx(-0.2)


@pytest.fixture
def env(tmp_path, monkeypatch):
    s = Settings(db_path=tmp_path / "t.db")
    conn = store.connect(s)
    snap = market.PairSnapshot(MINT, "MEME", "pool", "raydium", 0.0003, 3e-6, 80_000, 1e6, 5000, 30000,
                               10, 2, 120, 1.0, 5.0, int(time.time() * 1000) - 3600_000, time.time())
    state = {"snap": snap}
    monkeypatch.setattr(market, "snapshot", lambda m, s=None: state["snap"])
    monkeypatch.setattr(market, "snapshots", lambda ms, s=None: {MINT: state["snap"]})
    monkeypatch.setattr(market, "jupiter_quote", lambda *a, **k: None)
    monkeypatch.setattr(market, "sol_usd", lambda s=None: 100.0)
    monkeypatch.setattr(chain, "token_decimals", lambda rpc, m: 6)
    ex = PaperExecutor(s, rpc=None)
    return s, conn, ex, state


def whale_buy(sol=5.0, age=5):
    return chain.Swap("sigW", W, int(time.time()) - age, "buy", MINT, sol / 3e-6, sol, 3e-6)


def test_paper_roundtrip_target(env):
    s, conn, ex, state = env
    st = strategy.Strategy(conn, s, ex)
    pid = st.on_whale_buy(whale_buy())  # unranked -> exploration at half size
    assert pid
    p = conn.execute("SELECT * FROM ct_positions WHERE id=?", (pid,)).fetchone()
    assert p["size_sol"] == pytest.approx(s.position_size_sol / 2)
    assert p["entry_price_sol"] > 3e-6  # impact, fees and slippage make our fill worse
    assert ex.balance(conn) == pytest.approx(s.paper_start_balance_sol - p["entry_cost_sol"])
    import dataclasses
    state["snap"] = dataclasses.replace(state["snap"], price_sol=p["entry_price_sol"] * (1 + p["take_profit"] + 0.05))
    st.update()
    p = conn.execute("SELECT * FROM ct_positions WHERE id=?", (pid,)).fetchone()
    assert p["status"] == "closed" and p["exit_reason"] == "target" and p["pnl_sol"] > 0
    sm = stats.summary(stats.positions(conn, "paper"))
    assert sm["trades"] == 1 and sm["win_rate"] == 1.0


def test_skips(env):
    s, conn, ex, state = env
    st = strategy.Strategy(conn, s, ex)
    assert st.on_whale_buy(whale_buy(age=600)) is None  # too late
    assert st.on_whale_buy(whale_buy(sol=0.1)) is None  # not a whale
    import dataclasses
    state["snap"] = dataclasses.replace(state["snap"], price_sol=3e-6 * 1.5)
    assert st.on_whale_buy(whale_buy()) is None  # already pumped since the whale
    kinds = [r[0] for r in conn.execute("SELECT message FROM ct_events WHERE kind='skip'")]
    assert any("zu spät" in k for k in kinds) and any("seit Wal" in k for k in kinds)


def test_rank_and_backtest(env):
    s, conn, ex, state = env
    t = int(time.time()) - 86400
    for i in range(8):
        good = i % 4 != 0
        conn.execute("INSERT INTO ct_buy_outcomes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (f"s{i}", W, MINT, t + i * 600, 5, "pool", 1.0, 1.02, 0.8 if good else 0.05, 9, 0.9, 10, 9, 3,
                      0.45 if good else -0.2, int(good), 60, t))
    conn.commit()
    assert ranking.rank_wallets(conn, s) == 1
    r = ranking.wallet_rank(conn, W)
    assert r["samples"] == 8 and r["score"] > 0 and s.tp_min <= r["take_profit"] <= s.tp_max
    bt = stats.backtest(conn, s)
    assert len(bt) == 8 - s.min_wallet_samples and bt.pnl_sol.sum() > 0


# ---------------------------------------------------------------- modes, presets, mode 2
from copytrade import control  # noqa: E402
from copytrade.engine import follow_signals  # noqa: E402


def _signal(conn, score=80, buyers=4, age=10, mint=MINT):
    import json as _json
    conn.execute("""INSERT INTO signals(ts, mint, symbol, score, net_usd, buy_usd, smart_buyers, avg_quality,
                    price_usd, mcap_usd, wallets_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                 (int(time.time()) - age, mint, "MEME", score, 9000, 10000, buyers, 70, 0.0003, 1e6,
                  _json.dumps([{"wallet": W, "buy_usd": 5000}])))
    conn.commit()
    return conn.execute("SELECT * FROM signals ORDER BY id DESC LIMIT 1").fetchone()


def test_signal_mode_auto_buys_and_respects_preset(env):
    s, conn, ex, state = env
    st = strategy.Strategy(conn, s, ex)
    assert follow_signals(conn, st) == 0  # first call only sets the cursor
    _signal(conn, score=40)
    _signal(conn, score=85, mint=MINT)
    assert follow_signals(conn, st) == 2
    pos = conn.execute("SELECT * FROM ct_positions WHERE strategy='signal'").fetchall()
    assert len(pos) == 1 and pos[0]["managed"] == "auto" and pos[0]["signal_id"]
    assert pos[0]["size_sol"] == control.preset(conn, "signal").size_sol
    assert conn.execute("SELECT COUNT(*) FROM ct_alerts WHERE action='bought'").fetchone()[0] == 1


def test_manual_mode_only_alerts(env):
    s, conn, ex, state = env
    control.set_mode(conn, "signal", "manual")
    control.set_mode(conn, "whale", "manual")
    st = strategy.Strategy(conn, s, ex)
    follow_signals(conn, st)
    _signal(conn)
    follow_signals(conn, st)
    st.on_whale_buy(whale_buy())
    assert conn.execute("SELECT COUNT(*) FROM ct_positions").fetchone()[0] == 0
    acts = [r[0] for r in conn.execute("SELECT strategy FROM ct_alerts WHERE action='signal'")]
    assert sorted(acts) == ["signal", "whale"]


def test_semi_mode_hint_then_user_sells(env):
    import dataclasses
    s, conn, ex, state = env
    control.set_mode(conn, "whale", "semi")
    st = strategy.Strategy(conn, s, ex)
    pid = st.on_whale_buy(whale_buy())
    p = conn.execute("SELECT * FROM ct_positions WHERE id=?", (pid,)).fetchone()
    state["snap"] = dataclasses.replace(state["snap"], price_sol=p["entry_price_sol"] * 0.5)
    st.update()
    p = conn.execute("SELECT * FROM ct_positions WHERE id=?", (pid,)).fetchone()
    assert p["status"] == "open" and p["exit_hint"] == "stop"  # bot does not sell in semi mode
    control.request_sell(conn, pid)
    assert st.process_commands() == 1
    p = conn.execute("SELECT * FROM ct_positions WHERE id=?", (pid,)).fetchone()
    assert p["status"] == "closed" and p["exit_reason"] == "manual"


def test_manual_buy_command_and_pause(env):
    s, conn, ex, state = env
    st = strategy.Strategy(conn, s, ex)
    control.set_paused(conn, True)
    control.request_buy(conn, MINT, 0.2)
    st.process_commands()
    assert conn.execute("SELECT status, result FROM ct_commands").fetchone()[0] == "failed"
    control.set_paused(conn, False)
    control.request_buy(conn, MINT, 0.2)
    st.process_commands()
    p = conn.execute("SELECT * FROM ct_positions").fetchone()
    assert p["managed"] == "semi" and p["size_sol"] == pytest.approx(0.2)


def test_presets_and_live_switch(env):
    s, conn, ex, state = env
    assert {"vorsichtig", "standard", "aggressiv"} <= set(control.presets(conn))
    control.save_preset(conn, "mein", control.Preset(size_sol=0.05, stop_loss=0.1))
    control.use_preset(conn, "signal", "mein")
    assert control.preset(conn, "signal").size_sol == 0.05
    control.set_live(conn, "signal", True)
    st = strategy.Strategy(conn, s, {"paper": ex, "live": None})
    assert st.executor("signal").mode == "paper"  # no live executor without .env master switch


# ---------------------------------------------------------------- secrets never reach logs
def test_failing_request_never_logs_api_key(monkeypatch, caplog, tmp_path):
    import logging

    import requests

    from copytrade import http as cthttp

    secret = "SuperSecretKey123"
    url = f"https://mainnet.helius-rpc.com/?api-key={secret}"

    def boom(*a, **k):
        raise requests.ConnectionError(f"HTTPSConnectionPool: Max retries exceeded with url: /?api-key={secret}")

    monkeypatch.setattr(cthttp._session, "request", boom)
    monkeypatch.setattr(cthttp.time, "sleep", lambda s: None)
    caplog.set_level(logging.DEBUG)
    with pytest.raises(RuntimeError) as ei:
        cthttp.post_json(url, {"x": 1}, retries=2)
    assert secret not in str(ei.value)
    logging.getLogger("copytrade.chain").warning("rpc failed: %s at %s", ei.value, url)
    assert secret not in caplog.text and "api-key=***" in caplog.text

    s = Settings(db_path=tmp_path / "t.db", rpc_url=url)
    conn = store.connect(s)
    store.event(conn, "error", f"call {url} failed", data={"url": url})
    row = conn.execute("SELECT message, data FROM ct_events").fetchone()
    assert secret not in row[0] and secret not in row[1]


def test_signal_mode_uses_model_when_trained(env, monkeypatch):
    import model
    from model.predict import ModelNotTrained

    s, conn, ex, state = env
    st = strategy.Strategy(conn, s, ex)
    follow_signals(conn, st)

    def untrained(*a, **k):
        raise ModelNotTrained("no model yet")

    monkeypatch.setattr(model, "score_token", untrained)
    p = control.preset(conn, "signal")
    control.save_preset(conn, "nurmodell", control.Preset(**{**p.__dict__, "require_model": True}))
    control.use_preset(conn, "signal", "nurmodell")
    _signal(conn, score=95)
    follow_signals(conn, st)  # untrained + require_model: no buy, no crash
    assert conn.execute("SELECT COUNT(*) FROM ct_positions").fetchone()[0] == 0

    control.use_preset(conn, "signal", "standard")
    monkeypatch.setattr(model, "score_token", lambda mint, conn=None: {"score": 20.0, "grade": "D", "version": 3})
    _signal(conn, score=95)
    follow_signals(conn, st)  # model says 20 % < 30 %: skip even though the signal score is 95
    assert conn.execute("SELECT COUNT(*) FROM ct_positions").fetchone()[0] == 0

    monkeypatch.setattr(model, "score_token", lambda mint, conn=None: {"score": 62.0, "grade": "A", "version": 3})
    _signal(conn, score=40)
    follow_signals(conn, st)  # model 62 % (A): buy at 1.5x size
    pos = conn.execute("SELECT * FROM ct_positions").fetchone()
    assert pos and pos["size_sol"] == pytest.approx(control.preset(conn, "signal").size_sol * 1.5)


def test_geckoterminal_uses_shared_limiter(monkeypatch):
    from copytrade import http as cthttp

    calls = []
    monkeypatch.setattr(cthttp, "throttle", lambda host: calls.append(("throttle", host)))
    monkeypatch.setattr(cthttp, "penalize", lambda host, s: calls.append(("penalize", host, s)))
    monkeypatch.setattr(cthttp.time, "sleep", lambda s: calls.append(("sleep", s)))

    class R:
        def __init__(self, code):
            self.status_code, self.text, self.headers = code, "", {"Retry-After": "40"}

        def raise_for_status(self):
            pass

        def json(self):
            return {"ok": 1}

    answers = [R(429), R(200)]
    monkeypatch.setattr(cthttp._session, "request", lambda *a, **k: answers.pop(0))
    assert cthttp.get_json("https://api.geckoterminal.com/api/v2/x", limiter_name="geckoterminal") == {"ok": 1}
    assert calls == [("throttle", "api.geckoterminal.com"), ("penalize", "api.geckoterminal.com", 40.0),
                     ("throttle", "api.geckoterminal.com")]  # no own limiter, no own sleep


def test_db_writes_never_hold_the_lock(tmp_path):
    import sqlite3
    import threading

    s = Settings(db_path=tmp_path / "t.db")
    conn = store.connect(s)
    conn.execute("UPDATE ct_state SET value='1' WHERE key='x'")
    store.event(conn, "x", "y")
    assert not conn.in_transaction  # autocommit: nothing stays locked across network calls
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"

    other = sqlite3.connect(str(s.db_path), timeout=0.1, check_same_thread=False)
    other.execute("BEGIN IMMEDIATE")  # another service holds the write lock for a moment
    threading.Timer(0.5, other.rollback).start()
    store.event(conn, "x", "waited for the lock")  # busy_timeout waits instead of failing
    assert conn.execute("SELECT COUNT(*) FROM ct_events").fetchone()[0] == 2

    calls = []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        return "ok"

    import copytrade.store as st_mod
    orig = st_mod.time.sleep
    st_mod.time.sleep = lambda x: None
    try:
        assert store.retry_locked(flaky) == "ok" and len(calls) == 2
    finally:
        st_mod.time.sleep = orig


def test_helius_rpc_uses_shared_limiter(monkeypatch):
    from copytrade import http as cthttp

    hosts = []
    monkeypatch.setattr(cthttp, "throttle", lambda host: hosts.append(host))

    class R:
        status_code, text, headers = 200, "", {}

        def raise_for_status(self):
            pass

        def json(self):
            return {"result": 7}

    monkeypatch.setattr(cthttp._session, "request", lambda *a, **k: R())
    rpc = chain.Rpc(Settings(rpc_url="https://mainnet.helius-rpc.com/?api-key=k"))
    assert rpc.shared and rpc.call("getSlot", []) == 7 and hosts == ["mainnet.helius-rpc.com"]


def test_rug_protection_blocks_buys(env, monkeypatch):
    import json as _json

    import safety

    from copytrade import control
    s, conn, ex, state = env
    st = strategy.Strategy(conn, s, ex)
    safety.block(conn, MINT, "Test-Rug")
    assert st.on_whale_buy(whale_buy()) is None
    assert st.on_signal(_signal(conn)) is None
    control.request_buy(conn, MINT)
    st.process_commands()
    assert conn.execute("SELECT COUNT(*) FROM ct_positions").fetchone()[0] == 0
    msgs = [r[0] for r in conn.execute("SELECT message FROM ct_events WHERE kind='skip'")]
    assert sum("Rug-Schutz" in m for m in msgs) == 2
    assert "Rug-Schutz" in conn.execute("SELECT result FROM ct_commands").fetchone()[0]
    safety.allow(conn, MINT, "geprüft")  # your manual allow wins
    pid = st.on_whale_buy(whale_buy())
    p = conn.execute("SELECT * FROM ct_positions WHERE id=?", (pid,)).fetchone()
    assert p["safety_level"] and _json.loads(p["meta"])["safety"]["blocked"] is False


def test_live_buy_requires_fresh_safety_check(env, monkeypatch):
    import safety

    from copytrade import control
    s, conn, ex, state = env
    live = PaperExecutor(s, rpc=None)
    live.mode = "live"
    st = strategy.Strategy(conn, s, {"paper": ex, "live": live})
    control.set_live(conn, "signal", True)
    calls = []

    def assess_fail(mint, conn=None, deep=True, **k):
        calls.append(deep)
        raise RuntimeError("rpc down")

    monkeypatch.setattr(safety, "assess", assess_fail)
    assert st.on_signal(_signal(conn)) is None and calls == [False]
    assert any("Rug-Prüfung nicht möglich" in r[0] for r in conn.execute("SELECT message FROM ct_events"))
    ok = safety.Assessment(MINT, int(time.time()), 10.0, "low", False, 1.0)
    monkeypatch.setattr(safety, "assess", lambda mint, conn=None, deep=True, **k: ok)
    pid = st.on_signal(_signal(conn, mint=MINT))
    assert pid and conn.execute("SELECT mode, safety_level FROM ct_positions").fetchone()[:] == ("live", "low")
