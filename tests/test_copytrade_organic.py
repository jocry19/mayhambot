"""The brain's organic exit on the signal paper account: off (unchanged exits) until mature and live."""
import dataclasses
import types

from copytrade import control, organic, strategy
from test_copytrade import MINT, env  # noqa: F401  (fixture)

from brain import exits as real_exits


def _fake_exits(live=True, act="hold"):
    calls = []

    def advise(xm, toks, s, halved, up):
        calls.append(toks)
        return act, -0.08 if act == "sell" else 0.05, 0.2 if act == "sell" else 0.7

    ns = types.SimpleNamespace(**{k: getattr(real_exits, k) for k in ("Live", "tokens", "coin_swaps", "smart_flow",
                                                                       "FWD_MIN")})
    ns.mature = lambda xm: True
    ns.verdict = lambda conn, kind=None: kind == "signal" and {"n": 40, "organic": 0.12, "plan": 0.05, "live": live}
    ns.advise = advise
    ns.calls = calls
    return ns


def _signal_position(st, conn, state):
    p = control.preset(conn, "signal")
    pid = st.open("signal", "auto", MINT, 0.1, state["snap"], p, tp=p.take_profit, hold=p.max_hold_min, reason="test")
    assert pid
    return conn.execute("SELECT * FROM ct_positions WHERE id=?", (pid,)).fetchone()


def _price(state, p, gain):
    # paper exits measure from the market price at the buy (meta mid_sol), not from the fill
    state["snap"] = dataclasses.replace(state["snap"], price_sol=strategy.Strategy._ref_price(p) * (1 + gain))


def _row(conn, pid):
    return conn.execute("SELECT * FROM ct_positions WHERE id=?", (pid,)).fetchone()


def test_without_an_exit_model_the_signal_account_exits_exactly_as_before(env):
    s, conn, ex, state = env
    st = strategy.Strategy(conn, s, ex)
    p = _signal_position(st, conn, state)
    assert not st.organic_exit().active()  # no data/brain/exit_model.npz in the test folder
    _price(state, p, p["take_profit"] + 0.05)
    st.update()
    assert _row(conn, p["id"])["exit_reason"] == "target"


def test_not_live_verdict_keeps_the_account_exits(env):
    s, conn, ex, state = env
    st = strategy.Strategy(conn, s, ex)
    fx = _fake_exits(live=False, act="sell")
    st._organic = organic.OrganicExit(conn, s, exits_mod=fx, brain_settings=types.SimpleNamespace(half_life_h=120))
    p = _signal_position(st, conn, state)
    _price(state, p, 0.01)
    st.update()
    assert _row(conn, p["id"])["status"] == "open" and not fx.calls  # the model was not even asked
    _price(state, p, p["take_profit"] + 0.05)
    st.update()
    assert _row(conn, p["id"])["exit_reason"] == "target"


def test_live_brain_holds_past_the_target_sells_on_its_call_and_logs_why(env):
    s, conn, ex, state = env
    st = strategy.Strategy(conn, s, ex)
    fx = _fake_exits(live=True, act="hold")
    st._organic = org = organic.OrganicExit(conn, s, exits_mod=fx, brain_settings=types.SimpleNamespace(half_life_h=120))
    p = _signal_position(st, conn, state)
    _price(state, p, p["take_profit"] + 0.05)
    st.update()
    assert _row(conn, p["id"])["status"] == "open"  # the brain holds: no fixed target
    assert fx.calls and "kind=signal" in fx.calls[0]
    ev = [r[0] for r in conn.execute("SELECT message FROM ct_events WHERE kind='organic'")]
    assert any("an:" in m and "+12.0%" in m for m in ev)  # the gate switch is an event with its numbers
    fx.advise = lambda *a: ("sell", -0.08, 0.2)
    org.live.checked.clear()  # next minute
    st.update()
    r = _row(conn, p["id"])
    assert r["status"] == "closed" and r["exit_reason"] == "organic"
    ev = [r[0] for r in conn.execute("SELECT message FROM ct_events WHERE kind='organic'")]
    assert any(m.startswith("Hirn verkauft") and "P(steigt) 20%" in m for m in ev)
    assert strategy.EXIT_NAMES["organic"]


def test_live_brain_keeps_the_accounts_stop_and_max_hold(env):
    s, conn, ex, state = env
    st = strategy.Strategy(conn, s, ex)
    st._organic = organic.OrganicExit(conn, s, exits_mod=_fake_exits(live=True, act="hold"),
                                      brain_settings=types.SimpleNamespace(half_life_h=120))
    p = _signal_position(st, conn, state)
    _price(state, p, -p["stop_loss"] - 0.02)
    st.update()
    assert _row(conn, p["id"])["exit_reason"] == "stop"
    conn.execute("UPDATE ct_positions SET intent=NULL")
    _price(state, p, 0.0)
    p2 = _signal_position(st, conn, state)
    conn.execute("UPDATE ct_positions SET opened_at=opened_at-? WHERE id=?", (p2["max_hold_min"] * 60 + 5, p2["id"]))
    st.update()
    assert _row(conn, p2["id"])["exit_reason"] == "time"


def test_paper_stop_uses_the_live_curve_price_and_the_quote_can_hold_it(env, monkeypatch):
    from copytrade import market

    s, conn, ex, state = env
    st = strategy.Strategy(conn, s, ex)
    p = _signal_position(st, conn, state)
    ref = strategy.Strategy._ref_price(p)
    # the screener still shows the buy price, the curve is already below the stop: the stop fires on the curve
    _price(state, p, 0.0)
    monkeypatch.setattr(st, "_curve_price", lambda mint, screener, now: ref * (1 - p["stop_loss"] - 0.05))
    st.update()
    assert _row(conn, p["id"])["exit_reason"] == "stop"
    # a stop the sell quote contradicts (a sale would bring a profit) does not sell, and is not re-quoted at once
    p2 = _signal_position(st, conn, state)
    monkeypatch.setattr(st, "_curve_price", lambda mint, screener, now: None)
    _price(state, p2, -p2["stop_loss"] - 0.05)
    quotes = []
    out = int(p2["entry_cost_sol"] * 1.1 * 1e9)
    monkeypatch.setattr(market, "jupiter_quote", lambda *a, **k: quotes.append(1) or {"outAmount": out})
    st.update()
    assert _row(conn, p2["id"])["status"] == "open" and quotes == [1]
    st.update()
    assert _row(conn, p2["id"])["status"] == "open" and quotes == [1]


def test_curve_price_is_ignored_when_it_is_another_unit(monkeypatch):
    from sniper import store as sstore

    st = strategy.Strategy.__new__(strategy.Strategy)
    st._sconn = object()
    monkeypatch.setattr(sstore, "curve_now", lambda conn, mint, age, now=None: {"price_sol": 10.0, "complete": False})
    assert st._curve_price("m", 9.0, 0) == 10.0
    assert st._curve_price("m", 1.0, 0) is None  # 10x off the screener: a units error
    monkeypatch.setattr(sstore, "curve_now", lambda conn, mint, age, now=None: {"price_sol": 10.0, "complete": True})
    assert st._curve_price("m", 9.0, 0) is None  # migrated: the screener's pool price counts
