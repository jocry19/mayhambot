"""Copy-trader logic tests, offline (market calls are stubbed). Run: python -m pytest tests/test_copytrade.py"""
import time

import pytest

from copytrade import chain, market, ranking, stats, store, strategy, tracker
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
    s = Settings(db_path=tmp_path / "t.db", explore_unranked=True, thompson=False)  # paper exploration path, gates
    conn = store.connect(s)
    snap = market.PairSnapshot(MINT, "MEME", "pool", "raydium", 0.0003, 3e-6, 80_000, 1e6, 5000, 30000,
                               10, 2, 120, 1.0, 5.0, int(time.time() * 1000) - 3600_000, time.time())
    state = {"snap": snap}
    monkeypatch.setattr(market, "snapshot", lambda m, s=None: state["snap"])
    monkeypatch.setattr(market, "snapshots", lambda ms, s=None: {MINT: state["snap"]})
    monkeypatch.setattr(market, "snapshots_checked", lambda ms, s=None: ({MINT: state["snap"]}, state.get("failed", set())))
    monkeypatch.setattr(market, "jupiter_quote", lambda *a, **k: None)
    monkeypatch.setattr(market, "jupiter_quote_status", lambda *a, **k: (None, False))  # Jupiter: no route
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
    gap = s.pump_window_min * 60 + 60  # the backtest only uses outcomes already known at the time
    t = int(time.time()) - 8 * gap - 3600
    for i in range(8):
        good = i % 4 != 0
        conn.execute("INSERT INTO ct_buy_outcomes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (f"s{i}", W, MINT, t + i * gap, 5, "pool", 1.0, 1.02, 0.8 if good else 0.05, 9, 0.9, 10, 9, 3,
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


def _strict(s):
    import dataclasses

    return dataclasses.replace(s, explore=False)  # strict account only


def test_signal_mode_auto_buys_and_respects_preset(env):
    s, conn, ex, state = env
    st = strategy.Strategy(conn, _strict(s), ex)
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
    st = strategy.Strategy(conn, _strict(s), ex)
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
    follow_signals(conn, st)  # model 62 % (A): buy, grade sizing is neutral (the top band loses most net)
    pos = conn.execute("SELECT * FROM ct_positions").fetchone()
    assert pos and pos["size_sol"] == pytest.approx(control.preset(conn, "signal").size_sol * strategy.GRADE_SIZE["A"])


def test_signal_gate_uses_the_net_model_when_it_has_one(env, monkeypatch):
    import model

    s, conn, ex, state = env
    st = strategy.Strategy(conn, _strict(s), ex)
    follow_signals(conn, st)
    h6 = {"p_pump": 0.6, "algo": "gbm", "p_win_net": 0.3, "exp_net_ret": -0.08}
    monkeypatch.setattr(model, "score_token", lambda mint, conn=None: {"score": 62.0, "grade": "A", "version": 11,
                                                                       "horizons": {"6h": h6}})
    _signal(conn, score=95)
    follow_signals(conn, st)  # pump score 62 but the net model expects -8 % after costs: no buy (v11 holdout)
    assert conn.execute("SELECT COUNT(*) FROM ct_positions").fetchone()[0] == 0
    assert any("nach Kosten in 6 h" in r[0] for r in conn.execute("SELECT message FROM ct_events WHERE kind='skip'"))
    h6["exp_net_ret"] = 0.04
    monkeypatch.setattr(model, "score_token", lambda mint, conn=None: {"score": 12.0, "grade": "E", "version": 11,
                                                                       "horizons": {"6h": h6}})
    _signal(conn, score=95)
    follow_signals(conn, st)  # low pump score, positive expected net: the net model decides
    assert conn.execute("SELECT COUNT(*) FROM ct_positions").fetchone()[0] == 1


def test_geckoterminal_uses_shared_limiter(monkeypatch):
    from copytrade import http as cthttp

    calls = []
    monkeypatch.setattr(cthttp, "throttle", lambda host, **k: calls.append(("throttle", host)))
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
    monkeypatch.setattr(cthttp, "throttle", lambda host, **k: hosts.append(host))

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


def test_wallet_polls_use_free_rpc_and_fall_back(monkeypatch):
    from copytrade import http as cthttp
    urls = []

    def fake_post(url, payload, **k):
        urls.append(url.split("?")[0])
        if "mainnet-beta" in url and payload["method"] == "getSignaturesForAddress" and len(urls) > 1:
            raise RuntimeError("429")
        return {"result": [{"signature": "x"}]}

    monkeypatch.setattr(chain, "_core_http", None)  # without core's endpoint pool
    monkeypatch.setattr(cthttp, "post_json", fake_post)
    rpc = chain.Rpc(Settings(rpc_url="https://mainnet.helius-rpc.com/?api-key=k", poll_rpc_url=""))
    assert rpc.poll_signatures(W) == [{"signature": "x"}]
    assert urls == ["https://api.mainnet-beta.solana.com"]  # no Helius credit for "anything new?"
    with pytest.raises(RuntimeError):  # bulk read fails on public: retried later, never on Helius credits
        rpc.poll_signatures(W)
    assert urls[-1] == "https://api.mainnet-beta.solana.com" and rpc.bulk_failures == 1
    rpc.poll_signatures(W, priority="live")  # time-critical: public fails -> main RPC
    assert urls[-1] == "https://mainnet.helius-rpc.com/" and rpc.poll_fallbacks == 1
    assert chain.Rpc(Settings(rpc_url="https://mainnet.helius-rpc.com/?api-key=k", poll_rpc_url="off")).poller is None


# ---------------------------------------------------------------- entry gates from the 10/2026 paper review
def _rank(conn, samples=10, score=0.5):
    conn.execute("INSERT OR REPLACE INTO ct_wallet_rank(address, samples, hit_rate, score, take_profit, max_hold_min, "
                 "updated_at) VALUES (?,?,?,?,?,?,?)", (W, samples, 0.5, score, 0.4, 60, int(time.time())))


def _evals(conn, gain, n=10, kind="whale_buy"):
    import json as _json

    from copytrade import sim
    for i in range(n):
        conn.execute("INSERT INTO ct_evals VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (kind, f"e{gain}{i}", "whale", W, MINT, int(time.time()) - 9000 + i, sim.GRID_VERSION,
                      _json.dumps([gain] * len(sim.GRID)), None, None, "{}", int(time.time())))


def test_unranked_whales_only_measured_without_exploration(env):
    s, conn, ex, state = env
    st = strategy.Strategy(conn, Settings(db_path=s.db_path, thompson=False), ex)
    assert st.on_whale_buy(whale_buy()) is None
    msgs = [r[0] for r in conn.execute("SELECT message FROM ct_events WHERE kind='skip'")]
    assert any("nur mitgemessen" in m for m in msgs)


def test_ranked_whale_needs_edge_after_costs(env):
    s, conn, ex, state = env
    st = strategy.Strategy(conn, Settings(db_path=s.db_path, thompson=False), ex)
    _rank(conn)
    _evals(conn, -0.05)
    assert st.on_whale_buy(whale_buy()) is None  # its past buys lost money with our exits
    assert any("Vorteil" in r[0] for r in conn.execute("SELECT message FROM ct_events WHERE kind='skip'"))
    conn.execute("DELETE FROM ct_evals")
    _evals(conn, 0.12)
    assert st.on_whale_buy(whale_buy())  # proven, clear edge: copied with the largest conviction size
    p = conn.execute("SELECT * FROM ct_positions").fetchone()
    assert p["size_sol"] == pytest.approx(s.position_size_sol * 1.5)
    assert "Vorteil" in conn.execute("SELECT reason FROM ct_alerts WHERE action='bought'").fetchone()[0]


def test_strict_copy_halves_size_after_a_5min_run(env, monkeypatch):
    from copytrade import learn
    s, conn, ex, state = env
    st = strategy.Strategy(conn, Settings(db_path=s.db_path, thompson=False), ex)
    _rank(conn)
    _evals(conn, 0.12)
    monkeypatch.setattr(learn, "intel_context", lambda *a: ({"run5": ">5%"}, {"px_ret_5m": 0.12}))
    assert st.on_whale_buy(whale_buy())
    p = conn.execute("SELECT * FROM ct_positions").fetchone()
    assert p["size_sol"] == pytest.approx(s.position_size_sol * 1.5 * strategy.RUN5_SIZE)
    assert "Kurs 5 min schon +12%" in conn.execute("SELECT reason FROM ct_alerts WHERE action='bought'").fetchone()[0]


def test_extra_cost_from_own_trades_lowers_the_edge(env):
    from copytrade import learn
    s, conn, ex, state = env
    _rank(conn)
    _evals(conn, 0.02)
    for i in range(12):  # paper fills 4 %-points worse than the simulation said
        conn.execute("INSERT INTO ct_evals(kind, ref, strategy, wallet, mint, ts, grid_version, gains, actual_gain, "
                     "sim_actual, features, created_at) VALUES ('trade',?,?,?,?,?,1,'[]',?,?,'{}',0)",
                     (str(i), "whale", "other", MINT, i, -0.07, -0.03))
    assert learn.extra_cost(conn, "whale") == pytest.approx(0.04)
    e = learn.wallet_edge(conn, W)
    assert e["n"] == 10 and e["mean"] == pytest.approx(0.02 - 0.04) and e["lower"] < 0 and e["size_factor"] == 0


def test_noisy_edge_is_not_proven_and_old_data_decays():
    from copytrade import learn
    now = time.time()
    assert learn.decay(now, now) == 1 and learn.decay(now - 72 * 3600, now) == pytest.approx(0.5)


def test_noisy_wallet_edge_needs_lower_bound(env):
    import json as _json

    from copytrade import learn, sim
    s, conn, ex, state = env
    for i, g in enumerate([0.6, -0.3, -0.3, 0.5, -0.3, -0.3, 0.4, 0.0]):  # mean +3.8 %, but pure noise
        conn.execute("INSERT INTO ct_evals VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     ("whale_buy", f"n{i}", "whale", W, MINT, int(time.time()) - 100 + i, sim.GRID_VERSION,
                      _json.dumps([g] * len(sim.GRID)), None, None, "{}", 0))
    e = learn.wallet_edge(conn, W)
    assert e["mean"] > 0 and e["lower"] < 0


def test_rollback_when_real_trades_miss_the_test(env):
    from copytrade import learn
    s, conn, ex, state = env
    conn.execute("INSERT INTO ct_param_versions(id, ts, strategy, scope, params, train_gain, test_gain, accepted, "
                 "active) VALUES (50, 0, 'whale', 'mode', '{}', 0.05, 0.04, 1, 1)")
    for i in range(10):
        conn.execute("INSERT INTO ct_positions(mode, strategy, mint, status, pnl_pct, param_version) "
                     "VALUES ('paper','whale',?, 'closed', -0.06, 50)", (f"m{i}",))
    assert learn.rollback(conn) == [50]
    assert conn.execute("SELECT active FROM ct_param_versions WHERE id=50").fetchone()[0] == 0


def test_wash_traded_coin_and_coin_limit(env, monkeypatch):
    s, conn, ex, state = env
    st = strategy.Strategy(conn, s, ex)
    monkeypatch.setattr(strategy, "wash_share", lambda c, m: 0.4)
    assert st.on_whale_buy(whale_buy()) is None
    assert any("Wash-Trading" in r[0] for r in conn.execute("SELECT message FROM ct_events WHERE kind='skip'"))
    monkeypatch.setattr(strategy, "wash_share", lambda c, m: 0.1)
    now = int(time.time())
    for i in range(s.max_copies_per_coin):
        conn.execute("INSERT INTO ct_positions(mode, strategy, wallet, mint, opened_at, status) "
                     "VALUES ('paper','whale',?,?,?,'closed')", (f"w{i}", MINT, now - 60))
    assert st.on_whale_buy(whale_buy()) is None
    assert any("in diesem Coin" in r[0] for r in conn.execute("SELECT message FROM ct_events WHERE kind='skip'"))


def test_learned_version_that_lost_in_training_is_ignored(env):
    import json as _json

    from copytrade import learn
    s, conn, ex, state = env
    conn.execute("INSERT INTO ct_param_versions(ts, strategy, scope, params, n_train, n_test, train_gain, test_gain, "
                 "baseline_test_gain, accepted, active) VALUES (0,'whale','mode',?,200,90,-0.008,0.003,-0.05,1,1)",
                 (_json.dumps({"delay_s": 15, "take_profit": 0.4, "stop_loss": 0.1, "trail_activate": 0.15,
                               "trail_drawdown": 0.08, "max_hold_min": 45, "inflow_exit": 0}),))
    assert learn.params_for(conn, "whale") is None


def _launch(conn, decided_ts=None, mcap_usd=1e6, entered_ts=None):
    conn.execute("CREATE TABLE IF NOT EXISTS sniper_launches (mint TEXT PRIMARY KEY, source TEXT, decided_ts INTEGER, "
                 "decision TEXT, score REAL, risk REAL, mcap_decide_usd REAL)")
    conn.execute("CREATE TABLE IF NOT EXISTS sniper_positions (mint TEXT, opened_ts INTEGER)")
    conn.execute("INSERT OR REPLACE INTO sniper_launches(mint, source, decided_ts, decision, score, risk, mcap_decide_usd) "
                 "VALUES (?,?,?,?,?,?,?)", (MINT, "pumpportal", decided_ts or int(time.time()), "enter", 55, 20, mcap_usd))
    conn.execute("DELETE FROM sniper_positions WHERE mint=?", (MINT,))
    conn.execute("INSERT INTO sniper_positions VALUES (?,?)", (MINT, entered_ts or decided_ts or int(time.time())))
    return conn.execute("SELECT * FROM sniper_launches WHERE mint=?", (MINT,)).fetchone()


def test_launch_mode_off_by_default_then_buys_sniper_entries(env):
    from copytrade import control
    from copytrade.engine import follow_launches
    s, conn, ex, state = env
    st = strategy.Strategy(conn, s, ex)
    assert control.mode(conn, "launch") == "off"
    _launch(conn)
    assert follow_launches(conn, st) == 0 and conn.execute("SELECT COUNT(*) FROM ct_positions").fetchone()[0] == 0
    control.set_mode(conn, "launch", "auto")
    assert follow_launches(conn, st) == 0  # first start only remembers where it is
    # stage 1 decided 90 s ago, the sniper entered at stage 2 just now: still handed over and bought
    _launch(conn, decided_ts=int(time.time()) - 90, entered_ts=int(time.time()) + 1)
    assert follow_launches(conn, st) == 1
    p = conn.execute("SELECT * FROM ct_positions WHERE strategy='launch'").fetchone()
    pre = control.preset(conn, "launch")
    ex_ = strategy.sniper_exits()
    assert p and p["mode"] == "paper" and p["size_sol"] == pytest.approx(pre.size_sol)
    assert p["stop_loss"] == pytest.approx(min(ex_["stop_loss"], pre.stop_loss))  # never wider than the preset
    assert p["trail_activate"] == pytest.approx(ex_["trail_activate"])


def test_launch_mode_skips_old_or_run_up_launches(env):
    from copytrade import control
    s, conn, ex, state = env
    control.set_mode(conn, "launch", "auto")
    st = strategy.Strategy(conn, s, ex)
    assert st.on_launch(_launch(conn, decided_ts=int(time.time()) - 600)) is None
    assert st.on_launch(_launch(conn, mcap_usd=0.5e6)) is None  # snapshot fdv 1e6 = +100 % since the decision
    msgs = [r[0] for r in conn.execute("SELECT message FROM ct_events WHERE kind='skip'")]
    assert any("zu alt" in m for m in msgs) and any("Sniper-Entscheidung schon" in m for m in msgs)


def test_dexscreener_outage_does_not_close_as_no_market(env):
    s, conn, ex, state = env
    st = strategy.Strategy(conn, s, ex)
    assert st.on_whale_buy(whale_buy())
    state["failed"], state["snap"] = {MINT}, None
    for _ in range(15):
        st.update()
    assert conn.execute("SELECT status FROM ct_positions").fetchone()[0] == "open"
    state["failed"] = set()  # the coin is really gone: closes after 12 rounds
    for _ in range(12):
        st.update()
    assert conn.execute("SELECT exit_reason FROM ct_positions").fetchone()[0] == "no_market"


def test_close_twice_sells_once(env):
    s, conn, ex, state = env
    st = strategy.Strategy(conn, s, ex)
    assert st.on_whale_buy(whale_buy())
    p = conn.execute("SELECT * FROM ct_positions").fetchone()
    bal = ex.balance(conn)
    assert st.close(p, state["snap"], "whale") is True
    assert st.close(p, state["snap"], "stop") is False  # second thread holding the same stale row
    assert conn.execute("SELECT COUNT(*) FROM ct_positions WHERE status='closed'").fetchone()[0] == 1
    assert ex.balance(conn) > bal and conn.execute("SELECT exit_reason FROM ct_positions").fetchone()[0] == "whale"


def test_launch_bot_wallet_is_not_copied(env):
    s, conn, ex, state = env
    strategy._lb_cache.clear()
    conn.execute("CREATE TABLE IF NOT EXISTS tokens (mint TEXT, pair_created_at INTEGER)")
    t = int(time.time()) - 3600
    for i in range(4):
        conn.execute("INSERT INTO tokens(mint, pair_created_at) VALUES (?,?)", (f"L{i}", (t + i) * 1000))  # ms
        conn.execute("INSERT INTO ct_swaps VALUES (?,?,?,?,?,?,?,?,?)", (f"lb{i}", W, f"L{i}", "buy", t + i + 1, 1, 1, 1, t))
    st = strategy.Strategy(conn, s, ex)
    assert st.on_whale_buy(whale_buy()) is None
    assert any("Launch-/Dev-Bot" in r[0] for r in conn.execute("SELECT message FROM ct_events WHERE kind='skip'"))


def test_paper_buy_needs_a_safety_result(env, monkeypatch):
    import safety
    s, conn, ex, state = env
    st = strategy.Strategy(conn, s, ex)
    monkeypatch.setattr(safety, "assess", lambda *a, **k: safety.Assessment(MINT, 0, 0.0, "unchecked", False, 0.0))
    assert st.on_whale_buy(whale_buy()) is None

    def down(*a, **k):
        raise RuntimeError("rpc down")
    monkeypatch.setattr(safety, "assess", down)
    assert st.on_whale_buy(whale_buy()) is None
    assert conn.execute("SELECT COUNT(*) FROM ct_positions").fetchone()[0] == 0
    msgs = [r[0] for r in conn.execute("SELECT message FROM ct_events WHERE kind='skip'")]
    assert sum("kein Kauf ohne Prüfung" in m for m in msgs) == 2


def test_model_view_base_rate_model_carries_no_signal(monkeypatch):
    import model as _model
    out = {"score": 12.0, "grade": "A", "version": 3, "tiers": {},
           "horizons": {"1h": {"p_pump": 0.12, "algo": "base", "model": "logistic"}}}
    monkeypatch.setattr(_model, "score_token", lambda mint, conn=None: out)
    v = strategy.model_view({"mint": MINT})
    assert v["informative"] is False and v["size_factor"] == 1.0 and v["p_win_net_1h"] is None
    out["horizons"]["1h"].update(algo="gbm", p_win_net=0.4)
    v = strategy.model_view({"mint": MINT})
    assert v["informative"] and v["size_factor"] == strategy.GRADE_SIZE["A"] and v["p_win_net_1h"] == 0.4


def test_retried_entry_never_buys_twice(env):
    s, conn, ex, state = env
    st = strategy.Strategy(conn, s, ex)
    calls = []
    real = ex.buy
    ex.buy = lambda *a, **k: calls.append(1) or real(*a, **k)
    sw = whale_buy()
    pid = st.on_whale_buy(sw)
    assert pid and len(calls) == 1
    # e.g. 'database is locked' after the swap and the caller runs again: same intent, no second swap
    st._coin_copies = lambda mint, explore=False: 0
    assert st.on_whale_buy(sw) in (pid, None) and len(calls) == 1
    assert conn.execute("SELECT COUNT(*) FROM ct_positions WHERE intent=?", (f"whale:{sw.signature}",)).fetchone()[0] == 1


def test_failed_paper_fill_leaves_no_row_and_old_buy_clicks_expire(env):
    from copytrade import control
    s, conn, ex, state = env
    st = strategy.Strategy(conn, s, ex)
    from copytrade.execution import Fill
    ex.buy = lambda *a, **k: Fill(False, error="no price")
    assert st.on_whale_buy(whale_buy()) is None
    assert conn.execute("SELECT COUNT(*) FROM ct_positions").fetchone()[0] == 0
    cid = control.request_buy(conn, MINT)
    conn.execute("UPDATE ct_commands SET ts=ts-600 WHERE id=?", (cid,))
    assert control.pending_commands(conn) == []
    assert conn.execute("SELECT status FROM ct_commands WHERE id=?", (cid,)).fetchone()[0] == "expired"


def test_second_strategy_does_not_buy_a_coin_already_held_or_being_bought(env):
    s, conn, ex, state = env
    st = strategy.Strategy(conn, s, ex)
    from copytrade import control
    pre = control.preset(conn, "signal")
    conn.execute("INSERT INTO ct_positions(mode, strategy, mint, status, opened_at, tokens, entry_cost_sol) "
                 "VALUES ('paper','whale',?,'opening',?,0,0)", (MINT, int(time.time())))
    assert st.open("signal", "auto", MINT, 0.1, state["snap"], pre, tp=0.5, hold=60, reason="t", intent="signal:9") is None
    conn.execute("UPDATE ct_positions SET status='open'")
    assert st.open("signal", "auto", MINT, 0.1, state["snap"], pre, tp=0.5, hold=60, reason="t", intent="signal:9") is None
    msgs = [r[0] for r in conn.execute("SELECT message FROM ct_events WHERE kind='skip'")]
    assert any("Kauf läuft schon" in m for m in msgs) and any("schon gehalten" in m for m in msgs)


def test_live_sell_runs_in_a_worker_and_stops_use_the_exit_quote(env):
    s, conn, ex, state = env
    st = strategy.Strategy(conn, s, ex)
    pid = st.on_whale_buy(whale_buy())
    conn.execute("UPDATE ct_positions SET mode='live' WHERE id=?", (pid,))
    conn.commit()
    p = conn.execute("SELECT * FROM ct_positions WHERE id=?", (pid,)).fetchone()
    st.exs["live"] = ex
    ex.exit_value = lambda mint, tokens, dec: (p["entry_cost_sol"] * 0.5, 0)  # a sell now brings -50 %
    st.update()  # DexScreener price is unchanged, the quote says stop
    for _ in range(50):
        if conn.execute("SELECT status FROM ct_positions WHERE id=?", (pid,)).fetchone()[0] == "closed":
            break
        time.sleep(0.05)
    r = conn.execute("SELECT status, exit_reason FROM ct_positions WHERE id=?", (pid,)).fetchone()
    assert tuple(r) == ("closed", "stop")


def test_exploration_account_trades_what_the_strict_rules_skip(env, monkeypatch):
    import dataclasses

    s, conn, ex, state = env
    s = Settings(db_path=s.db_path, thompson=False)  # defaults: exploration on, unranked whales not copied by the strict account
    tiers = {"t": "smart"}
    monkeypatch.setattr(store, "wallet_info", lambda c, w: {"tier": tiers["t"]})
    st = strategy.Strategy(conn, s, ex)
    assert "explore" in st.exs
    pid = st.on_whale_buy(whale_buy(sol=1.0))  # unranked and under the 2 SOL minimum: strict skips, exploration buys
    p = conn.execute("SELECT * FROM ct_positions WHERE id=?", (pid,)).fetchone()
    assert p["mode"] == "explore" and p["size_sol"] == pytest.approx(s.explore_size_sol)
    assert p["intent"].startswith("explore:whale:")
    assert st.exs["explore"].balance(conn) == pytest.approx(s.explore_start_balance_sol - p["entry_cost_sol"])
    assert ex.balance(conn) == pytest.approx(s.paper_start_balance_sol)  # strict account untouched
    assert any("nur Erkundung" in r[0] for r in conn.execute("SELECT message FROM ct_events WHERE kind='skip'"))
    st.on_whale_buy(whale_buy(sol=1.0))  # the same whale buy again: no second exploration buy
    assert conn.execute("SELECT COUNT(*) FROM ct_positions").fetchone()[0] == 1
    # exits work the same; PnL lands on the exploration balance
    state["snap"] = dataclasses.replace(state["snap"], price_sol=p["entry_price_sol"] * (1 + p["take_profit"] + 0.05))
    st.update()
    p = conn.execute("SELECT * FROM ct_positions WHERE id=?", (pid,)).fetchone()
    assert p["status"] == "closed" and p["pnl_sol"] > 0
    assert st.exs["explore"].balance(conn) > s.explore_start_balance_sol
    assert ex.balance(conn) == pytest.approx(s.paper_start_balance_sol)


def test_exploration_only_for_chosen_tiers_and_beside_strict_copies(env, monkeypatch):
    s, conn, ex, state = env
    s = Settings(db_path=s.db_path, thompson=False)
    tiers = {"t": "good"}
    monkeypatch.setattr(store, "wallet_info", lambda c, w: {"tier": tiers["t"]})
    st = strategy.Strategy(conn, s, ex)
    assert st.on_whale_buy(whale_buy(sol=1.0)) is None  # tier 'good' is not explored
    assert st.on_whale_buy(whale_buy(sol=0.3)) is None
    assert not conn.execute("SELECT COUNT(*) FROM ct_positions").fetchone()[0]
    tiers["t"] = "ultra"
    _rank(conn)
    _evals(conn, 0.12)
    pid = st.on_whale_buy(whale_buy())  # proven edge: the strict account copies, exploration copies beside it
    rows = {r["mode"]: r for r in conn.execute("SELECT * FROM ct_positions WHERE status='open'")}
    assert set(rows) == {"paper", "explore"} and rows["paper"]["id"] == pid
    assert rows["explore"]["size_sol"] == pytest.approx(s.explore_size_sol)
    control.set_mode(conn, "whale", "manual")  # manual mode only alerts, also no exploration buy
    conn.execute("DELETE FROM ct_positions")
    assert st.on_whale_buy(chain.Swap("sigX", W, int(time.time()) - 5, "buy", MINT, 1e6, 5.0, 3e-6)) is None
    assert not conn.execute("SELECT COUNT(*) FROM ct_positions").fetchone()[0]


def test_exploration_can_be_turned_off_and_its_positions_still_sell_on_their_own_account(env, monkeypatch):
    s, conn, ex, state = env
    monkeypatch.setattr(store, "wallet_info", lambda c, w: {"tier": "smart"})
    st = strategy.Strategy(conn, Settings(db_path=s.db_path, thompson=False), ex)
    pid = st.on_whale_buy(whale_buy(sol=1.0))
    off = strategy.Strategy(conn, Settings(db_path=s.db_path, thompson=False, explore=False), ex)
    assert off.on_whale_buy(chain.Swap("sigZ", W, int(time.time()) - 5, "buy", "OTHER", 1e6, 1.0, 3e-6)) is None
    paper_before = ex.balance(conn)
    p = conn.execute("SELECT * FROM ct_positions WHERE id=?", (pid,)).fetchone()
    assert off.close(p, state["snap"], "manual")
    assert ex.balance(conn) == pytest.approx(paper_before)  # proceeds went to the exploration balance
    assert off.exs["explore"].balance(conn) > s.explore_start_balance_sol - 0.1


def test_exploration_buys_signals_the_strict_gates_turn_down(env):
    import dataclasses

    s, conn, ex, state = env
    st = strategy.Strategy(conn, s, ex)
    follow_signals(conn, st)
    state["snap"] = dataclasses.replace(state["snap"], liquidity_usd=8000)  # under the preset's $15k
    _signal(conn, score=85)
    follow_signals(conn, st)
    rows = conn.execute("SELECT * FROM ct_positions").fetchall()
    assert [r["mode"] for r in rows] == ["explore"] and rows[0]["size_sol"] == pytest.approx(s.explore_size_sol)
    assert rows[0]["take_profit"] == pytest.approx(control.preset(conn, "signal").take_profit)  # same exits
    conn.execute("DELETE FROM ct_positions")
    state["snap"] = dataclasses.replace(state["snap"], liquidity_usd=3000)  # too thin even for exploration
    _signal(conn, score=85)
    follow_signals(conn, st)
    assert not conn.execute("SELECT COUNT(*) FROM ct_positions").fetchone()[0]
    state["snap"] = dataclasses.replace(state["snap"], liquidity_usd=80_000)
    _signal(conn, score=85)
    follow_signals(conn, st)  # every gate passes: strict buys, exploration buys beside it
    assert sorted(r["mode"] for r in conn.execute("SELECT mode FROM ct_positions")) == ["explore", "paper"]


def test_exploration_whale_copy_from_lower_liquidity(env, monkeypatch):
    import dataclasses

    s, conn, ex, state = env
    s = Settings(db_path=s.db_path, thompson=False)
    monkeypatch.setattr(store, "wallet_info", lambda c, w: {"tier": "smart"})
    st = strategy.Strategy(conn, s, ex)
    state["snap"] = dataclasses.replace(state["snap"], liquidity_usd=8000)
    assert st.on_whale_buy(whale_buy(sol=1.0))
    assert [r[0] for r in conn.execute("SELECT mode FROM ct_positions")] == ["explore"]
    conn.execute("DELETE FROM ct_positions")
    state["snap"] = dataclasses.replace(state["snap"], liquidity_usd=2000)
    assert st.on_whale_buy(chain.Swap("sigY", W, int(time.time()) - 5, "buy", MINT, 1e6, 1.0, 3e-6)) is None


def test_bonding_curve_coins_only_explored_and_late_copies_skipped(env, monkeypatch):
    import dataclasses

    s, conn, ex, state = env
    st = strategy.Strategy(conn, Settings(db_path=s.db_path, thompson=False), ex)
    _rank(conn)
    _evals(conn, 0.12)
    state["snap"] = dataclasses.replace(state["snap"], dex_id="pumpfun")
    assert st.on_whale_buy(whale_buy()) is None  # strict account: curve coins lost -17 % net in the research data
    assert any("Bonding Curve" in r[0] for r in conn.execute("SELECT message FROM ct_events WHERE kind='skip'"))
    monkeypatch.setattr(store, "wallet_info", lambda c, w: {"tier": "smart"})
    assert st.on_whale_buy(whale_buy(sol=1.0)) is None  # exploration neither: 22 curve copies -18.7 %, 9 % winners
    state["snap"] = dataclasses.replace(state["snap"], dex_id="pumpswap")
    assert st.on_whale_buy(chain.Swap("sigSwap", W, int(time.time()) - 5, "buy", MINT, 1e6, 1.0, 3e-6))
    assert [r[0] for r in conn.execute("SELECT mode FROM ct_positions")] == ["explore"]
    late = chain.Swap("sigLate", W, int(time.time()) - strategy.MAX_ENTRY_AGE_S - 5, "buy", MINT, 1e6, 1.0, 3e-6)
    assert st.on_whale_buy(late) is None


def test_curve_depth_from_price_lets_exploration_buy_a_curve_coin(env, monkeypatch):
    import dataclasses

    # at launch a pump.fun curve holds 30 virtual SOL: depth 2 x 30 SOL
    assert market.curve_liquidity_usd("pumpfun", 30 / 1.073e9, 100.0) == pytest.approx(6000)
    assert market.curve_liquidity_usd("pumpswap", 30 / 1.073e9, 100.0) == 0
    s, conn, ex, state = env
    monkeypatch.setattr(strategy, "EXPLORE_CURVE", True)  # the curve path itself (block-0 copies use it)
    st = strategy.Strategy(conn, Settings(db_path=s.db_path, thompson=False), ex)
    monkeypatch.setattr(store, "wallet_info", lambda c, w: {"tier": "smart"})
    state["snap"] = dataclasses.replace(state["snap"], dex_id="pumpfun", liquidity_usd=0.0)
    assert st.on_whale_buy(whale_buy(sol=1.0)) is None  # DexScreener's $0 and no curve depth: no buy
    assert any("($0)" in r[0] for r in conn.execute("SELECT message FROM ct_events WHERE kind='skip'"))
    state["snap"] = dataclasses.replace(state["snap"], curve_liq_usd=9000.0)
    assert st.on_whale_buy(whale_buy(sol=1.0))
    assert [r[0] for r in conn.execute("SELECT mode FROM ct_positions")] == ["explore"]


def test_buys_under_the_copy_minimum_leave_a_summary_line(env, monkeypatch):
    s, conn, ex, state = env
    st = strategy.Strategy(conn, Settings(db_path=s.db_path, thompson=False), ex)
    assert st.on_whale_buy(whale_buy(sol=0.3)) is None
    assert not conn.execute("SELECT 1 FROM ct_events WHERE kind='small'").fetchone()
    st._small[2] -= strategy.SMALL_SUMMARY_S
    assert st.on_whale_buy(whale_buy(sol=0.5)) is None
    row = conn.execute("SELECT message FROM ct_events WHERE kind='small'").fetchone()
    assert row and "2 Wal-Käufe" in row[0] and st._small[0] == 0


def test_paper_target_needs_a_sellable_price_and_slippage_survives_a_restart(env, monkeypatch):
    import dataclasses
    import json as _json

    from copytrade import execution

    s, conn, ex, state = env
    store.set_state(conn, "paper_adverse", _json.dumps({"adverse": 0.0075}))
    monkeypatch.delenv("CT_PAPER_ADVERSE_SLIPPAGE", raising=False)
    monkeypatch.setattr(execution, "_adverse", 0.02)
    st = strategy.Strategy(conn, s, ex)
    assert execution.paper_adverse() == pytest.approx(0.0075)  # measured value, not the 2 % default
    pid = st.on_whale_buy(whale_buy())
    p = conn.execute("SELECT * FROM ct_positions WHERE id=?", (pid,)).fetchone()
    assert _json.loads(p["meta"])["adverse"] == pytest.approx(0.0075)
    state["snap"] = dataclasses.replace(state["snap"], price_sol=p["entry_price_sol"] * (1 + p["take_profit"] + 0.05))
    quote = {"outAmount": str(int(p["entry_cost_sol"] * 0.95 * 1e9))}  # mid says target, a sell would lose 5 %
    monkeypatch.setattr(market, "jupiter_quote", lambda *a, **k: quote)
    monkeypatch.setattr(market, "jupiter_quote_status", lambda *a, **k: (quote, False))
    st.update()
    assert conn.execute("SELECT status FROM ct_positions WHERE id=?", (pid,)).fetchone()[0] == "open"
    quote["outAmount"] = str(int(p["entry_cost_sol"] * (1 + p["take_profit"]) * 1e9))
    st.update()
    p = conn.execute("SELECT * FROM ct_positions WHERE id=?", (pid,)).fetchone()
    assert p["status"] == "closed" and p["exit_reason"] == "target" and p["pnl_sol"] > 0


def test_rug_without_route_books_zero_and_liquidity_pull_exits(env):
    import dataclasses

    s, conn, ex, state = env
    st = strategy.Strategy(conn, s, ex)
    pid = st.on_whale_buy(whale_buy())
    snap0 = state["snap"]
    state["snap"] = dataclasses.replace(snap0, liquidity_usd=snap0.liquidity_usd * 0.1)  # LP pulled, price unchanged
    st.update()
    p = conn.execute("SELECT * FROM ct_positions WHERE id=?", (pid,)).fetchone()
    assert p["status"] == "closed" and p["exit_reason"] == "rug"
    # no Jupiter route and no market left: the tokens are worth nothing, not last price x 0.9
    fill = ex.sell(MINT, 1000.0, 6, None, 3e-6)
    assert fill.ok and fill.sol == 0 and fill.source == "no_route"
    thin = dataclasses.replace(snap0, liquidity_usd=200)
    assert ex.sell(MINT, 1000.0, 6, thin, 3e-6).sol == 0


def test_sale_is_credited_once_even_if_booking_fails_midway(env, monkeypatch):
    s, conn, ex, state = env
    st = strategy.Strategy(conn, s, ex)
    pid = st.on_whale_buy(whale_buy())
    bal = ex.balance(conn)
    p = conn.execute("SELECT * FROM ct_positions WHERE id=?", (pid,)).fetchone()
    real = ex.adjust_balance

    def boom(conn_, delta, commit=True):
        real(conn_, delta, commit=False)
        raise RuntimeError("killed between credit and close")

    monkeypatch.setattr(ex, "adjust_balance", boom)
    with pytest.raises(RuntimeError):
        st.close(p, state["snap"], "manual")
    assert ex.balance(conn) == pytest.approx(bal)  # rolled back with the status change: nothing credited
    assert conn.execute("SELECT status FROM ct_positions WHERE id=?", (pid,)).fetchone()[0] == "closing"
    monkeypatch.setattr(ex, "adjust_balance", real)
    conn.execute("UPDATE ct_positions SET status='open' WHERE id=?", (pid,))  # what the start-up recovery does
    p = conn.execute("SELECT * FROM ct_positions WHERE id=?", (pid,)).fetchone()
    assert st.close(p, state["snap"], "manual")
    sold = conn.execute("SELECT proceeds_sol FROM ct_positions WHERE id=?", (pid,)).fetchone()[0]
    assert ex.balance(conn) == pytest.approx(bal + sold)  # credited exactly once


def test_paper_has_no_budget(env):
    # user, 10/2026: paper buys never stop on balance, open positions or the daily loss; the balance may go negative
    s, conn, ex, state = env
    st = strategy.Strategy(conn, s, ex)
    store.set_state(conn, "paper_balance_sol", 0.6)
    conn.execute("INSERT INTO ct_positions(mode, strategy, managed, mint, symbol, opened_at, size_sol, tokens, "
                 "entry_cost_sol, status) VALUES ('paper','signal','auto','OTHER','X',?,0.5,0,0,'opening')",
                 (int(time.time()),))
    conn.execute("INSERT INTO ct_positions(mode, strategy, managed, mint, symbol, opened_at, closed_at, size_sol, "
                 "tokens, entry_cost_sol, status, pnl_sol) VALUES ('paper','whale','auto','LOSS','L',?,?,1,0,0,"
                 "'closed',-50)", (int(time.time()), int(time.time())))
    p = control.preset(conn, "whale")
    assert st._risk_block("whale", p, 5.0, MINT) is None
    assert st.open("whale", "auto", MINT, 0.5, state["snap"], p, tp=0.5, hold=60, reason="t") is not None


def test_metrics_count_rugs_drawdowns_and_each_whale_buy_once(env):
    import pandas as pd

    from copytrade import learn
    s, conn, ex, state = env
    # drawdown from the start: -1, -1, +0.5 is a 2 SOL drawdown, not 1
    df = pd.DataFrame({"status": ["closed"] * 3, "pnl_sol": [-1.0, -1.0, 0.5], "pnl_pct": [-0.5, -0.5, 0.25],
                       "closed_at": [1, 2, 3], "hold_min": [1, 1, 1], "entry_delay_s": [1, 1, 1]})
    assert stats.summary(df)["max_drawdown_sol"] == pytest.approx(-2.0)
    # a rug replays at -100 %, not at the old -60 % floor
    assert learn.GAIN_CLIP[0] == -1.0
    # the same whale buy as whale_buy replay and as a trade row of two accounts counts once
    _rank(conn)
    _evals(conn, 0.12, n=10)
    one = learn.wallet_edge(conn, W)
    for kind in ("trade",):
        for i in range(10):
            row = conn.execute("SELECT * FROM ct_evals WHERE ref=?", (f"e0.12{i}",)).fetchone()
            for acc in ("p", "x"):
                conn.execute("INSERT INTO ct_evals VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                             (kind, f"{acc}{i}", "whale", W, MINT, row["ts"], row["grid_version"], row["gains"],
                              None, None, "{}", int(time.time())))
    assert learn.wallet_edge(conn, W)["n"] == one["n"] == 10


def test_signal_backtest_books_the_stop_when_the_dip_came_first(env):
    s, conn, ex, state = env
    for col in ("dd_before_peak_1h REAL", "outcome_flag TEXT"):
        try:
            conn.execute(f"ALTER TABLE signals ADD COLUMN {col}")
        except Exception:
            pass
    p = control.preset(conn, "signal")
    sig = _signal(conn, score=90)
    conn.execute("UPDATE signals SET ret_1h=-0.1, max_ret_1h=1.0, dd_before_peak_1h=-0.4 WHERE id=?", (sig["id"],))
    sig2 = _signal(conn, score=90)
    conn.execute("UPDATE signals SET ret_1h=-0.95, max_ret_1h=0.02, outcome_flag='dead' WHERE id=?", (sig2["id"],))
    bt = stats.signal_backtest(conn, p)
    gains = sorted(bt["gain"].round(3))
    assert gains == sorted([round(-0.95 - stats.ROUNDTRIP_COST, 3), round(-p.stop_loss - stats.ROUNDTRIP_COST, 3)])


def test_commands_are_claimed_once_and_unconfirmed_live_buys_stay_opening(env):
    from copytrade.execution import Fill

    s, conn, ex, state = env
    st = strategy.Strategy(conn, s, ex)
    conn.execute("INSERT INTO ct_commands(ts, action, mint, strategy, status) VALUES (?,?,?,?,?)",
                 (int(time.time()), "buy", MINT, "signal", "pending"))
    st2 = strategy.Strategy(conn, s, ex)
    assert st.process_commands() == 1 and st2.process_commands() == 0  # the second process finds it claimed
    assert conn.execute("SELECT COUNT(*) FROM ct_positions").fetchone()[0] == 1

    class Unsure:
        mode = "live"

        def buy(self, mint, size, snap):
            return Fill(False, error=f"{strategy.UNCONFIRMED} Kauf abc… wird geprüft")

        def balance(self, conn):
            return 10.0

    st3 = strategy.Strategy(conn, s, {"paper": ex, "live": Unsure()})
    st3.exs["live"] = Unsure()
    p = control.preset(conn, "signal")
    assert st3.open("signal", "auto", "LIVEMINT", 0.1, state["snap"], p, tp=0.5, hold=60, reason="t",
                    account="live") is None
    assert conn.execute("SELECT status FROM ct_positions WHERE mint='LIVEMINT'").fetchone()[0] == "opening"


def test_sell_without_any_answer_stays_open_instead_of_booking_a_rug(env, monkeypatch):
    s, conn, ex, state = env
    monkeypatch.setattr(market, "jupiter_quote_status", lambda *a, **k: (None, True))   # network down / 429
    f = ex.sell(MINT, 1000.0, 6, None, 3e-6)
    assert not f.ok and "pausiert" in f.error
    monkeypatch.setattr(market, "jupiter_quote_status", lambda *a, **k: (None, False))  # Jupiter: no route
    f = ex.sell(MINT, 1000.0, 6, None, 3e-6)
    assert f.ok and f.source == "no_route" and f.sol == 0


def test_jupiter_client_error_is_final_a_429_is_not():
    assert market._transient(RuntimeError("GET x failed: 400 Client Error: Bad Request for url")) is False
    assert market._transient(RuntimeError("GET x failed: 429 Too Many Requests")) is True
    assert market._transient(RuntimeError("GET x failed: HTTPSConnectionPool timed out")) is True


def test_ranking_evaluates_buys_from_the_exploration_minimum(env, monkeypatch):
    s, conn, ex, state = env
    old = int(time.time()) - s.pump_window_min * 60 - 600
    store.save_swaps(conn, [chain.Swap(f"sig{x}", W, old, "buy", MINT, 1e6, x, 3e-6) for x in (0.3, 0.6, 3.0)])
    monkeypatch.setattr(market, "best_pool", lambda m: "pool")
    monkeypatch.setattr(market, "minute_candles", lambda *a, **k: [])
    assert ranking.evaluate_pending(conn, s) == 2  # 0.6 and 3 SOL: a wallet's 0.5-2 SOL buys count as samples
    assert {r[0] for r in conn.execute("SELECT sig FROM ct_buy_outcomes")} == {"sig0.6", "sig3.0"}


def test_ledger_check_and_measured_costs(env):
    s, conn, ex, state = env
    st = strategy.Strategy(conn, s, ex)
    pid = st.on_whale_buy(whale_buy())
    p = conn.execute("SELECT * FROM ct_positions WHERE id=?", (pid,)).fetchone()
    assert st.close(p, state["snap"], "manual")
    d = stats.ledger(conn, s)["paper"]
    assert abs(d["diff"]) < 1e-9 and d["positions"] == 1
    ex.adjust_balance(conn, 0.25)  # a sale credited twice
    assert stats.ledger(conn, s)["paper"]["diff"] == pytest.approx(0.25)
    assert "DIFF +0.2500" in stats.bookkeeping_line(conn, s)
    c = stats.costs(conn)["paper/whale"]
    assert c["n_entry"] == 1 and c["entry"] > 0 and c["n_exit"] == 1 and c["exit"] > 0


def test_block0_group_is_explored_at_any_size_and_tagged(env, monkeypatch):
    s, conn, ex, state = env
    st = strategy.Strategy(conn, Settings(db_path=s.db_path, thompson=False), ex)
    monkeypatch.setattr(strategy, "launch_bot_share", lambda c, w: (20, 20))  # looks like a launch bot
    assert st.on_whale_buy(whale_buy(sol=0.005)) is None  # not in the group: under every minimum
    monkeypatch.setattr(tracker, "BLOCK0", {W})
    pid = st.on_whale_buy(whale_buy(sol=0.005))
    import json as _json

    rows = conn.execute("SELECT mode, meta, size_sol FROM ct_positions").fetchall()
    assert pid and [r[0] for r in rows] == ["explore"] and _json.loads(rows[0][1])["group"] == "block0"
    assert rows[0][2] == pytest.approx(s.explore_size_sol * strategy.BLOCK0_SIZE)


def test_learning_universe_wallets_are_explored_never_copied_strictly(env, monkeypatch):
    s, conn, ex, state = env
    st = strategy.Strategy(conn, Settings(db_path=s.db_path, thompson=False, explore_unranked=True), ex)
    monkeypatch.setattr(tracker, "WATCH", {W})
    st.on_whale_buy(whale_buy())
    assert {r[0] for r in conn.execute("SELECT mode FROM ct_positions")} <= {"explore"}
    assert conn.execute("SELECT 1 FROM ct_events WHERE message LIKE '%Lern-Universum%'").fetchone()


def test_ledger_fix_runs_once_and_keeps_the_old_value(env):
    s, conn, ex, state = env
    ex.balance(conn)
    ex.adjust_balance(conn, 0.22)
    rec = stats.fix_ledger_once(conn, s)
    assert rec["applied"] and rec["old"] == pytest.approx(s.paper_start_balance_sol + 0.22)
    assert abs(stats.ledger(conn, s)["paper"]["diff"]) < 1e-9
    ex.adjust_balance(conn, 0.1)
    assert stats.fix_ledger_once(conn, s) is None  # once only: later differences are reported, not corrected
    assert conn.execute("SELECT 1 FROM ct_events WHERE kind='verify'").fetchone()


def test_thompson_draw_tries_unknown_wallets_and_rarely_proven_losers(env, monkeypatch):
    from copytrade import learn

    s, conn, ex, state = env
    monkeypatch.setattr(learn, "population_edge", lambda c: {"n": 500, "mean": -0.05, "sd": 0.35})
    unknown = learn.thompson(conn, {"n": 0, "n_eff": 0, "mean": 0.0}, "a")
    loser = learn.thompson(conn, {"n": 100, "n_eff": 100, "mean": -0.05}, "a")
    winner = learn.thompson(conn, {"n": 40, "n_eff": 40, "mean": 0.12}, "a")
    assert 0.25 < unknown["p_pos"] < 0.45 and loser["p_pos"] < 0.1 and winner["p_pos"] > 0.95
    assert learn.thompson(conn, {"n": 0, "n_eff": 0, "mean": 0.0}, "a")["draw"] == unknown["draw"]  # same seed
    tried = sum(learn.thompson(conn, {"n": 0, "n_eff": 0, "mean": 0.0}, f"sig{i}")["draw"] > 0 for i in range(400))
    assert 100 < tried < 180


def test_thompson_prior_weight_is_measured_from_the_wallet_spread(env, monkeypatch):
    import random

    from copytrade import learn

    s, conn, ex, state = env
    rnd = random.Random(1)
    # 40 wallets whose true edges spread 0.05 around -0.05, each copy +- 0.17: k = 0.17^2 / 0.05^2 = 11.6
    by = {}
    for w in range(40):
        theta = rnd.gauss(-0.05, 0.05)
        by[w] = [rnd.gauss(theta, 0.17) for _ in range(30)]
    sp = learn._wallet_spread(by)
    assert sp["k_wallets"] == 40 and 6 < sp["k"] < 25 and 0.15 < sp["sd_within"] < 0.19
    assert learn._wallet_spread({w: v for w, v in list(by.items())[:5]}) == {}  # too few wallets: keep the default
    monkeypatch.setattr(learn, "population_edge", lambda c: {"n": 1200, "mean": -0.066, "sd": 0.18, **sp})
    unknown = learn.thompson(conn, {"n": 0, "n_eff": 0, "mean": 0.0}, "a")
    assert unknown["k"] == pytest.approx(sp["k"], abs=0.05) and unknown["sd"] == pytest.approx(
        sp["sd_within"] / sp["k"] ** 0.5)
    assert unknown["p_pos"] < 0.15  # tried less often than with the fixed weight 6 (about 0.19)
    probes = [learn.thompson(conn, {"n": 0, "n_eff": 0, "mean": 0.0}, f"p{i}") for i in range(300)]
    drawn = [d for d in probes if d["draw"] > 0]
    assert drawn and all(d["probe"] and d["size_factor"] == learn.THOMPSON_PROBE_SIZE for d in drawn)
    winner = learn.thompson(conn, {"n": 40, "n_eff": 40, "mean": 0.12}, "w")
    assert winner["mean"] > 0 and not winner["probe"] and winner["size_factor"] > learn.THOMPSON_PROBE_SIZE


def test_strict_copy_follows_the_thompson_draw(env, monkeypatch):
    from copytrade import learn

    s, conn, ex, state = env
    st = strategy.Strategy(conn, Settings(db_path=s.db_path, explore=False), ex)
    monkeypatch.setattr(learn, "wallet_edge", lambda c, w: {"n": 3, "n_eff": 3.0, "mean": 0.02, "lower": -0.1,
                                                             "size_factor": 0.0})
    draw = {"draw": -0.01, "mean": -0.02, "sd": 0.1, "p_pos": 0.4, "prior": -0.05, "size_factor": 0.0}
    monkeypatch.setattr(learn, "thompson", lambda c, e, seed: draw)
    assert st.on_whale_buy(whale_buy(sol=1.0)) is None  # 1 SOL: under the old 2 SOL minimum, the draw decides
    assert any("gezogener Vorteil" in r[0] for r in conn.execute("SELECT message FROM ct_events WHERE kind='skip'"))
    draw.update(draw=0.045, size_factor=1.5)
    pid = st.on_whale_buy(chain.Swap("sigT2", W, int(time.time()) - 5, "buy", MINT, 1.0 / 3e-6, 1.0, 3e-6))
    p = conn.execute("SELECT mode, size_sol FROM ct_positions WHERE id=?", (pid,)).fetchone()
    assert p["mode"] == "paper" and p["size_sol"] == pytest.approx(control.preset(conn, "whale").size_sol * 1.5)


def test_staged_pump_exits_once_the_high_is_past(env, monkeypatch):
    import dataclasses

    import safety

    s, conn, ex, state = env
    st = strategy.Strategy(conn, s, ex)
    pid = st.on_whale_buy(whale_buy())
    ok = safety.Assessment(MINT, int(time.time()), 10.0, "low", False, 1.0)
    monkeypatch.setattr(safety, "assess", lambda mint, conn=None, deep=True, **k: ok)
    p0 = conn.execute("SELECT * FROM ct_positions WHERE id=?", (pid,)).fetchone()
    state["snap"] = dataclasses.replace(state["snap"], price_sol=p0["entry_price_sol"] * 1.2)
    st.update()  # no pattern: the position rides on
    assert conn.execute("SELECT status FROM ct_positions WHERE id=?", (pid,)).fetchone()[0] == "open"
    ok.checks = {"staged": {"data": {"pattern": True}}}
    st._staged.clear()
    st.update()  # pattern, at the high: still open
    assert conn.execute("SELECT status FROM ct_positions WHERE id=?", (pid,)).fetchone()[0] == "open"
    st._high_ts[pid] -= strategy.STAGED_AFTER_HIGH_S
    st.update()  # 15 min past the high: out before the drain
    p = conn.execute("SELECT status, exit_reason FROM ct_positions WHERE id=?", (pid,)).fetchone()
    assert tuple(p) == ("closed", "staged")


def test_whale_decisions_go_to_the_opportunity_table_with_our_result(env):
    s, conn, ex, state = env
    st = strategy.Strategy(conn, s, ex)
    assert st.on_whale_buy(whale_buy(sol=0.1)) is None  # under every minimum: still an opportunity
    pid = st.on_whale_buy(chain.Swap("sigOpp", W, int(time.time()) - 5, "buy", MINT, 5 / 3e-6, 5.0, 3e-6))
    rows = {r["ref"]: r for r in conn.execute("SELECT * FROM opportunities WHERE source='strategy'")}
    assert rows["sigW"]["source_action"] == "passed" and rows["sigW"]["source_reason"] == "unter der Kopier-Grenze"
    assert rows["sigOpp"]["source_action"] == "took" and rows["sigOpp"]["liq_usd"] == 80_000
    p = conn.execute("SELECT * FROM ct_positions WHERE id=?", (pid,)).fetchone()
    assert st.close(p, state["snap"], "manual")
    r = conn.execute("SELECT source_result FROM opportunities WHERE ref='sigOpp'").fetchone()
    assert r[0] is not None and r[0] < 0  # sold at once: the round trip's cost


def test_half_the_pool_gone_exits_before_the_price_stop(env):
    import dataclasses

    s, conn, ex, state = env
    st = strategy.Strategy(conn, s, ex)
    pid = st.on_whale_buy(whale_buy())
    state["snap"] = dataclasses.replace(state["snap"], liquidity_usd=80_000 * 0.6)
    st.update()  # 40 % less pool: still open
    assert conn.execute("SELECT status FROM ct_positions WHERE id=?", (pid,)).fetchone()[0] == "open"
    state["snap"] = dataclasses.replace(state["snap"], liquidity_usd=80_000 * 0.45)
    st.update()  # more than half pulled, price not moved yet: out
    assert tuple(conn.execute("SELECT status, exit_reason FROM ct_positions WHERE id=?", (pid,)).fetchone()) == \
        ("closed", "rug")


def test_first_minute_curve_coin_priced_from_the_sniper_curve(env, monkeypatch):
    from sniper import store as sstore

    s, conn, ex, state = env
    monkeypatch.setattr(strategy, "EXPLORE_CURVE", True)  # the curve path itself (block-0 copies use it)
    st = strategy.Strategy(conn, Settings(db_path=s.db_path, thompson=False), ex)
    monkeypatch.setattr(store, "wallet_info", lambda c, w: {"tier": "smart"})
    monkeypatch.setattr(market, "snapshot", lambda m, s=None: None)  # DexScreener: no pair yet
    assert st.on_whale_buy(whale_buy(sol=1.0)) is None  # and no curve state either: no price
    sc = sstore.connect(s.db_path)
    sstore.save_curve_live(sc, [{"mint": MINT, "ts": time.time(), "pool": "pump", "price_sol": 3e-6, "v_sol": 40.0,
                                 "mcap_sol": 3000.0, "n_trades": 25, "complete": False}])
    pid = st.on_whale_buy(chain.Swap("sigCurve", W, int(time.time()) - 5, "buy", MINT, 1.0 / 3e-6, 1.0, 3e-6))
    import json as _json

    p = conn.execute("SELECT mode, meta FROM ct_positions WHERE id=?", (pid,)).fetchone()
    assert p["mode"] == "explore" and _json.loads(p["meta"])["dex"] == "pumpfun"


def test_a_rug_blocked_coin_is_not_checked_again_on_every_repeat_buy(env, monkeypatch):
    import safety

    s, conn, ex, state = env
    st = strategy.Strategy(conn, Settings(db_path=s.db_path, thompson=False), ex)
    monkeypatch.setattr(store, "wallet_info", lambda c, w: {"tier": "smart"})
    real, calls = safety.assess, []
    monkeypatch.setattr(safety, "assess", lambda *a, **k: calls.append(1) or real(*a, **k))
    safety.block(conn, MINT, "Test-Rug")
    for i in range(5):  # one bot buying the same coin again and again (10/18: 437 buys of GHOSTI)
        assert st.on_whale_buy(chain.Swap(f"sigR{i}", W, int(time.time()) - 5, "buy", MINT, 1e6, 1.0, 3e-6)) is None
    assert len(calls) == 1
    safety.allow(conn, MINT, "geprüft")  # your manual allow still wins at once
    assert st.on_whale_buy(chain.Swap("sigR9", W, int(time.time()) - 5, "buy", MINT, 1e6, 1.0, 3e-6))


def test_a_coin_only_the_swaps_know_is_priced_from_them(env, monkeypatch):
    from copytrade import tracker

    s, conn, ex, state = env
    st = strategy.Strategy(conn, Settings(db_path=s.db_path, thompson=False), ex)
    monkeypatch.setattr(market, "snapshot", lambda m, s=None: None)  # DexScreener: no pair, sniper: no curve state
    pump = "Pump1111111111111111111111111111111111pump"
    monkeypatch.setattr(tracker, "BLOCK0", {W})  # block-0 copies may buy curve coins
    swap = chain.Swap("sigP", W, int(time.time()) - 3, "buy", pump, 1.0 / 3e-6, 1.0, 3e-6)
    assert st.on_whale_buy(swap) is None  # no swap of the coin stored yet: no price
    now = int(time.time())
    conn.executemany("INSERT INTO ct_swaps(sig, wallet, mint, side, ts, token_amount, sol_amount, price_sol) "
                     "VALUES (?,?,?,?,?,?,?,?)", [(f"s{i}", "X" * 44, pump, "buy", now - 10 + i, 1e5, 0.3, 3e-6 * (1 + i / 100))
                                                  for i in range(3)])
    snap = st._swaps_snapshot(pump)
    assert snap and snap.dex_id == strategy.CURVE_DEX and snap.price_sol == pytest.approx(3.03e-6)
    assert snap.depth_usd > 0 and snap.age_hours < 1
    pid = st.on_whale_buy(chain.Swap("sigP2", W, int(time.time()) - 3, "buy", pump, 1.0 / 3e-6, 1.0, 3e-6))
    assert conn.execute("SELECT mode FROM ct_positions WHERE id=?", (pid,)).fetchone()["mode"] == "explore"
    assert st._swaps_snapshot("Other111111111111111111111111111111111111") is None
