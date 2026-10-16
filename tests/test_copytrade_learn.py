"""Self-learning layer tests, offline: simulator, out-of-sample tuning, caps, filters, use in trading."""
import dataclasses
import json
import time

import pytest

from copytrade import control, learn, sim, strategy

from test_copytrade import MINT, W, env, whale_buy  # noqa: F401  (fixture)

T0 = 1_700_000_000


def flat(n=30, p=1.0, v=100.0, t0=T0):
    return [(t0 + i * 60, p, p, p, p, v) for i in range(n)]


def with_path(prices, t0=T0, vols=None):
    """Candles whose close follows prices; high/low = close ±0.5 %."""
    vols = vols or [100.0] * len(prices)
    return [(t0 + i * 60, p, p * 1.005, p * 0.995, p, vols[i]) for i, p in enumerate(prices)]


V = sim.Variant(15, 0.4, 0.2, 0.0, 0.0, 120, 0)


# ---------------------------------------------------------------------- simulator
def test_grid_shape():
    assert len(sim.GRID) == 3 * 6 * 3 * 3 * 4 * 2
    assert len(sim.EXIT_GRID) == len(sim.GRID) // 3
    assert all(v.delay_s == sim.DELAYS_S[0] for v in sim.EXIT_GRID)


def test_simulate_target_stop_time():
    up = with_path([1.0, 1.0] + [1.0 + 0.05 * i for i in range(1, 20)])
    g, mins, why = sim.simulate(up, T0, V, cost=0.03)
    assert why == "target" and g == pytest.approx(0.4 - 0.03)
    down = with_path([1.0, 1.0] + [1.0 - 0.03 * i for i in range(1, 20)])
    g, _, why = sim.simulate(down, T0, V, cost=0.03)
    assert why == "stop" and g == pytest.approx(-0.21 - 0.03)  # the candle opens at 0.79, below the 0.80 stop
    rug = with_path([1.0, 1.0, 0.95, 0.06, 0.05])
    g, _, why = sim.simulate(rug, T0, V, cost=0.03)
    assert why == "stop" and g == pytest.approx(-0.94 - 0.03)  # a rug is booked at its real loss, not at -20 %
    g, mins, why = sim.simulate(flat(200), T0, V, cost=0)
    assert why == "time" and mins == pytest.approx(120) and g == pytest.approx(0)
    assert sim.simulate(flat(1), T0, V) is None  # nothing after the entry


def test_simulate_stop_checked_before_target_in_one_candle():
    c = flat(3) + [(T0 + 180, 1, 1.6, 0.7, 1.0, 100)]
    assert sim.simulate(c, T0, V, cost=0)[2] == "stop"


def test_simulate_trailing_inflow_whale_and_delay():
    prices = [1.0, 1.0] + [1.0 + 0.04 * i for i in range(1, 9)] + [1.32 - 0.03 * i for i in range(1, 10)]
    tr = sim.Variant(15, 1.5, 0.3, 0.15, 0.08, 240, 0)
    g, _, why = sim.simulate(with_path(prices), T0, tr, cost=0)
    assert why == "trail" and 0.15 < g < 0.3
    vols = [100, 100] + [3000] * 6 + [200] * 20
    rise = with_path([1.0, 1.0] + [1.0 + 0.02 * i for i in range(1, 27)], vols=vols)
    g, _, why = sim.simulate(rise, T0, sim.Variant(15, 1.5, 0.3, 0, 0, 240, 1), cost=0)
    assert why == "inflow_peak" and g > 0.05
    g, mins, why = sim.simulate(flat(60), T0, V, exit_events=[T0 + 600], cost=0)
    assert why == "whale" and mins == pytest.approx(10)
    late = with_path([1.0, 1.0, 1.0, 2.0, 2.0, 2.0, 2.0, 2.0, 2.0])
    early, delayed = sim.Variant(15, 0.5, 0.2, 0, 0, 5, 0), sim.Variant(180, 0.5, 0.2, 0, 0, 5, 0)
    assert sim.simulate(late, T0, early, cost=0)[2] == "target"
    assert sim.simulate(late, T0, delayed, cost=0)[0] == pytest.approx(0)  # entered after the jump


# ---------------------------------------------------------------------- tuning
def _gains(fn, variants=sim.GRID):
    return [round(fn(v), 4) for v in variants]


STAR = sim.Variant(60, 0.4, 0.2, 0.15, 0.08, 45, 1)
NEAR = {sim.GRID[j] for j in sim.neighbors(sim.GRID, sim.GRID.index(STAR))}


def star(v, hi, lo):
    """STAR wins, its grid neighbours do half as well (a stable optimum, not a lone spike)."""
    return hi if v == STAR else hi / 2 if v in NEAR else lo


def _seed(conn, n, fn_by_i, features_by_i=lambda i: {"tier": "smart"}, wallet="", kind="whale_buy"):
    for i in range(n):
        learn._store_eval(conn, kind, f"r{wallet[-4:]}{i}", "whale", wallet, MINT, T0 + i * 600,
                          _gains(lambda v, i=i: fn_by_i(i, v)), {"strategy": "whale", **features_by_i(i)})


def test_tune_accepts_only_out_of_sample_improvement(env):
    s, conn, ex, state = env
    _seed(conn, 50, lambda i, v: star(v, 0.2, -0.05))
    res = learn.tune(conn, s)["whale"]
    assert res["accepted"] and res["params"]["take_profit"] == 0.4 and res["test_gain"] > res["baseline"]
    lp = learn.params_for(conn, "whale")
    assert lp["delay_s"] == 60 and lp["inflow_exit"] == 1 and lp["version"]
    v = learn.versions(conn, "whale")
    assert len(v) == 1 and int(v["active"].iloc[0]) == 1
    assert conn.execute("SELECT COUNT(*) FROM ct_events WHERE kind='learn'").fetchone()[0] == 1


def test_tune_rejects_overfit(env):
    s, conn, ex, state = env
    # STAR wins on the older 70 % only, loses on the newer 30 %
    _seed(conn, 50, lambda i, v: (0.3 if i < 35 else -0.2) if v == STAR else 0.0)
    res = learn.tune(conn, s)["whale"]
    assert res and not res["accepted"]
    assert learn.params_for(conn, "whale") is None  # bot keeps the preset
    row = conn.execute("SELECT accepted, active FROM ct_param_versions").fetchone()
    assert tuple(row) == (0, 0)  # recorded for the history, not used


def test_tune_needs_enough_entries(env):
    s, conn, ex, state = env
    _seed(conn, learn.MIN_MODE_N - 1, lambda i, v: star(v, 0.2, -0.05))
    assert learn.tune(conn, s).get("whale") is None
    assert learn.status(conn)["whale"]["needed_for_tuning"] == 1


def test_learning_never_widens_stop_or_hold(env):
    s, conn, ex, state = env
    wide = sim.Variant(15, 0.6, 0.3, 0, 0, 240, 0)  # best, but its stop is wider than the preset's 20 %
    ok = sim.Variant(15, 0.6, 0.2, 0, 0, 120, 0)
    near_ok = {sim.GRID[j] for j in sim.neighbors(sim.GRID, sim.GRID.index(ok))}
    _seed(conn, 50, lambda i, v: 0.4 if v == wide else 0.2 if v == ok else 0.1 if v in near_ok else -0.05)
    p = control.preset(conn, "whale")
    assert p.stop_loss == pytest.approx(0.2)
    res = learn.tune(conn, s)["whale"]
    assert res["accepted"] and res["params"]["stop_loss"] <= p.stop_loss
    # preset tightened later: the active version is capped again when used
    control.save_preset(conn, "eng", dataclasses.replace(p, stop_loss=0.1, max_hold_min=60))
    control.use_preset(conn, "whale", "eng")
    lp = learn.params_for(conn, "whale")
    assert lp["stop_loss"] == pytest.approx(0.1) and lp["max_hold_min"] == 60
    # size, max open, daily loss and live switch are untouched by learning
    pre = control.preset(conn, "whale")
    assert (pre.size_sol, pre.max_open, pre.max_daily_loss_sol) == (p.size_sol, p.max_open, p.max_daily_loss_sol)
    assert not control.live_wanted(conn, "whale")


def test_learned_filter_blocks_losing_bucket(env):
    s, conn, ex, state = env
    _seed(conn, 60, lambda i, v: star(v, 0.4 if i % 2 else -0.1, -0.05),
          features_by_i=lambda i: {"tier": "ultra" if i % 2 else "none"})
    assert learn.tune(conn, s)["whale"]["accepted"]
    assert learn.blocked(conn, "whale", {"tier": "none"}) == "tier=none"
    assert learn.blocked(conn, "whale", {"tier": "ultra"}) is None


def test_wallet_scope_and_autotune_switch(env):
    s, conn, ex, state = env
    _seed(conn, 30, lambda i, v: 0.3 if v.take_profit == 1.0 and v.delay_s == 15 and v.stop_loss == 0.1 else -0.02,
          wallet=W)
    res = learn.tune(conn, s)
    assert res[f"whale:{W[:16]}"]["accepted"]
    assert learn.params_for(conn, "whale", W)["take_profit"] == 1.0
    assert learn.params_for(conn, "whale", "other") is None  # no mode version yet (only 30 entries)
    control.set_value(conn, "autotune:whale", "0")
    assert learn.params_for(conn, "whale", W) is None


# ---------------------------------------------------------------------- use in trading
def test_strategy_uses_learned_exits_and_delay(env):
    s, conn, ex, state = env
    _seed(conn, 50, lambda i, v: star(v, 0.2, -0.05))
    learn.tune(conn, s)
    st = strategy.Strategy(conn, s, ex)
    assert st.on_whale_buy(whale_buy(age=5)) is None  # waits for the learned 60 s delay
    assert len(st._pending) == 1
    st._pending = [(time.time() - 1, st._pending[0][1])]
    assert st.process_pending() == 1
    p = conn.execute("SELECT * FROM ct_positions").fetchone()
    assert p["take_profit"] == pytest.approx(0.4) and p["max_hold_min"] == 45
    assert p["trail_activate"] == pytest.approx(0.15) and p["use_inflow_exit"] == 1
    assert p["param_version"] == learn.params_for(conn, "whale")["version"]
    assert json.loads(p["meta"])["param_version"] == p["param_version"]


def test_pending_entry_dropped_when_whale_sold(env):
    s, conn, ex, state = env
    _seed(conn, 50, lambda i, v: star(v, 0.2, -0.05))
    learn.tune(conn, s)
    st = strategy.Strategy(conn, s, ex)
    sw = whale_buy(age=5)
    st.on_whale_buy(sw)
    conn.execute("INSERT INTO ct_swaps(sig, wallet, ts, side, mint, token_amount, sol_amount, price_sol) "
                 "VALUES ('sell1', ?, ?, 'sell', ?, 1, 5, 3e-6)", (W, sw.block_time + 10, MINT))
    st._pending = [(time.time() - 1, sw)]
    assert st.process_pending() == 0
    assert conn.execute("SELECT COUNT(*) FROM ct_positions").fetchone()[0] == 0


def test_closed_trade_is_evaluated_against_alternatives(env, monkeypatch):
    s, conn, ex, state = env
    st = strategy.Strategy(conn, s, ex)
    pid = st.on_whale_buy(whale_buy())
    old = int(time.time()) - learn.HORIZON_S - 100
    conn.execute("UPDATE ct_positions SET status='closed', opened_at=?, whale_ts=?, pnl_pct=0.1, pnl_sol=0.02 "
                 "WHERE id=?", (old, old - 5, pid))
    prices = [1.0] * 25 + [1.0 + 0.05 * i for i in range(1, 40)]
    monkeypatch.setattr(learn, "candles", lambda *a, **k: with_path(prices, t0=old - 20 * 60))
    assert learn.evaluate_trades(conn, s) == 1
    r = conn.execute("SELECT * FROM ct_evals WHERE kind='trade'").fetchone()
    g = json.loads(r["gains"])
    assert len(g) == len(sim.GRID) and r["actual_gain"] == pytest.approx(0.1) and r["sim_actual"] is not None
    assert max(x for x in g if x is not None) > 1.0  # the 1.5 target would have caught the run
    cmp = learn.exit_rule_comparison(conn, "whale")
    assert "tatsächlich" in set(cmp["style"])


def test_grid_replay_is_fast_enough():
    c = with_path([1.0 + 0.01 * ((i * 7) % 13 - 6) for i in range(300)])
    t = time.time()
    sim.grid_gains(c, T0, sim.GRID)
    assert time.time() - t < 3.0


# ---------------------------------------------------------------------- tiers, clusters, polling
W2 = "Wha1e2222222222222222222222222222222222222"


def _wallet_row(conn, addr, tier, cluster=None, capture=None, score=None):
    conn.execute("INSERT OR REPLACE INTO wallets(address, tier, tier_source, model_score, cluster_id, exit_capture) "
                 "VALUES (?,?,?,?,?,?)", (addr, tier, "model", score, cluster, capture))
    from copytrade import store
    store._winfo_cache.clear()


def test_tiers_come_from_wallets_table(env):
    from copytrade import stats, store, tracker
    s, conn, ex, state = env
    _wallet_row(conn, W, "ultra", cluster="c7", capture=0.85, score=91)
    _wallet_row(conn, W2, "good")
    assert learn.wallet_tier(conn, W) == "ultra"
    assert learn.wallet_features(conn, W) == {"tier": "ultra", "capture": ">80%"}
    assert store.entity(conn, W) == "cluster:c7" and store.entity(conn, W2) == W2
    tracker.load_core_smart(conn)
    src = dict(conn.execute("SELECT address, source FROM ct_wallets").fetchall())
    assert src == {W: "model_ultra"}  # 'good' is not copied automatically
    wt = stats.wallet_table(conn)
    row = wt[wt.address == W].iloc[0]
    assert row["tier"] == "ultra" and row["entity"] == "cluster:c7" and row["model_score"] == 91


def test_one_cluster_is_copied_once(env):
    from test_copytrade import whale_buy as wb
    s, conn, ex, state = env
    _wallet_row(conn, W, "smart", cluster="c1")
    _wallet_row(conn, W2, "smart", cluster="c1")
    st = strategy.Strategy(conn, s, ex)
    pid = st.on_whale_buy(wb())
    conn.execute("UPDATE ct_positions SET status='closed' WHERE id=?", (pid,))  # closed, so "schon im Coin" is off
    sw2 = dataclasses.replace(wb(), wallet=W2, signature="sigW2")
    assert st.on_whale_buy(sw2) is None
    assert any("Cluster" in r[0] for r in conn.execute("SELECT message FROM ct_events WHERE kind='skip'"))
    meta = json.loads(conn.execute("SELECT meta FROM ct_positions").fetchone()[0])
    assert meta["cluster"] == "cluster:c1" and meta["model_tier"] == "smart"


def test_cluster_wallets_are_tuned_together(env):
    s, conn, ex, state = env
    _wallet_row(conn, W, "smart", cluster="c9")
    _wallet_row(conn, W2, "smart", cluster="c9")
    good = lambda i, v: 0.3 if v.take_profit == 1.0 and v.delay_s == 15 and v.stop_loss == 0.1 else -0.02  # noqa: E731
    _seed(conn, 15, good, wallet=W)
    _seed(conn, 15, lambda i, v: good(i + 15, v), wallet=W2)  # 15 + 15: only together enough
    res = learn.tune(conn, s)
    assert res["whale:cluster:c9"]["accepted"]
    assert learn.params_for(conn, "whale", W2)["take_profit"] == 1.0


def test_quiet_wallets_are_polled_less_often(env):
    from copytrade import store, tracker
    s, conn, ex, state = env
    now = time.time()
    hot, warm, cold = "Hot1" + "1" * 40, "Warm" + "1" * 40, "Cold" + "1" * 40
    store.add_wallets(conn, [hot, warm, cold], "auto")
    for w, age in ((hot, 3600), (warm, 30 * 3600)):
        conn.execute("INSERT INTO ct_swaps(sig, wallet, mint, side, ts) VALUES (?,?,?,?,?)",
                     ("s" + w[:4], w, MINT, "buy", int(now - age)))
    conn.execute("UPDATE ct_wallets SET last_polled=?", (int(now - 30),))
    assert [r["address"] for r in tracker.due_wallets(conn, s, now)] == [hot]
    conn.execute("UPDATE ct_wallets SET last_polled=?", (int(now - 120),))
    assert {r["address"] for r in tracker.due_wallets(conn, s, now)} == {hot, warm}
    conn.execute("UPDATE ct_wallets SET last_polled=?", (int(now - 400),))
    assert len(tracker.due_wallets(conn, s, now)) == 3


def test_lone_spike_in_the_grid_is_not_accepted(env):
    s, conn, ex, state = env
    _seed(conn, 50, lambda i, v: 0.2 if v == STAR else -0.05)  # only STAR wins, every neighbour loses
    res = learn.tune(conn, s)["whale"]
    assert res is None or not res["accepted"]


def test_lucky_test_set_fails_the_monte_carlo(env):
    s, conn, ex, state = env
    # STAR wins big on one test entry and loses on all others: positive mean, but luck
    _seed(conn, 50, lambda i, v: star(v, (2.0 if i == 40 else -0.1) if i >= 35 else 0.2, -0.05))
    rows = learn._rows(conn, "whale")
    test = rows[int(len(rows) * learn.TRAIN_SHARE):]
    mte = learn._means(test, len(sim.GRID))
    b = sim.GRID.index(STAR)
    rob = learn.robustness(test, b, 0, sim.GRID, mte)
    assert mte[b] > 0 and rob["p_win"] < learn.BOOTSTRAP_MIN_WIN


def test_intel_context_buckets_and_thesis(monkeypatch):
    import sys, types
    from copytrade import learn

    fake = types.SimpleNamespace(features_at=lambda kind, key, ts, conn: {"features": (
        {"vol_roundtrip_60m": 0.05, "vol_entities_60m": 120.0, "liq_chg_60m": -0.3, "exec_rt_0_25": 0.04}
        if kind == "coin" else {"w_scalp_share": 0.8, "w_hold_median_min": 12.0})})
    monkeypatch.setitem(sys.modules, "intel", fake)
    b, raw = learn.intel_context(None, "MINTxyz", "WAL1", 1000)
    assert b == {"wash": "<10%", "persons": ">80", "liq60": "<-20%", "scalper": ">70%", "focus": "?"}
    t = learn.thesis(raw)
    assert "120 Personen/h" in t and "Wash 5%" in t and "Liquidität 1 h -30%" in t and "Scalps 80%" in t

    monkeypatch.setitem(sys.modules, "intel", types.SimpleNamespace(features_at=lambda *a: 1 / 0))
    assert learn.intel_context(None, "MINTxyz", "WAL1", 1000) == ({}, {})
    assert learn.thesis({}) == ""


def test_tracker_gap_marks_unknown_whale_exits():
    import sqlite3
    from copytrade import learn, store

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(store.SCHEMA)
    t = 1_000_000
    for i, seen in enumerate([t, t + 600, t + 1200, t + 1200 + 90 * 60, t + 1200 + 95 * 60]):
        conn.execute("INSERT INTO ct_swaps VALUES (?,?,?,?,?,?,?,?,?)", (f"s{i}", "X", "M", "buy", seen, 1, 1, 1, seen))
    gaps = learn.tracker_gaps(conn, t - 3600, t + 4 * 3600)
    assert gaps == [(t + 1200, t + 1200 + 90 * 60)]
    assert learn._sell_unknown(gaps, t, [])                    # holding into the outage, no sell on record
    assert not learn._sell_unknown(gaps, t, [t + 900])         # sold before the outage: every exit is known
    assert learn._sell_unknown(gaps, t, [t + 3 * 3600])        # first sell only after it: one may be missing
    assert not learn._sell_unknown(gaps, t + 6 * 3600, [])     # horizon starts after the outage


def test_landing_drift_sets_paper_slippage_from_the_tape():
    import sqlite3
    from copytrade import execution, learn, store

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(store.SCHEMA)
    conn.execute("CREATE TABLE trades (sig TEXT, wallet TEXT, mint TEXT, side TEXT, ts INTEGER, token_amount REAL, "
                 "sol_amount REAL, usd_amount REAL, price_usd REAL, source TEXT)")
    assert learn.landing_drift(conn)["adverse"] == learn.LAND_MAX  # nothing measured: keep the cautious 2 %
    for i in range(40):
        t = 10_000 * (i + 1)
        conn.execute("INSERT INTO ct_positions(mode, mint, opened_at, closed_at) VALUES ('paper', ?, ?, ?)",
                     (f"M{i}", t, t + 600))
        for ts, px in ((t - 5, 1.0), (t + 2, 1.003), (t + 595, 2.0), (t + 602, 1.99)):
            conn.execute("INSERT INTO trades VALUES ('s','w',?,'buy',?,1,1,1,?,'gt')", (f"M{i}", ts, px))
    d = learn.landing_drift(conn)
    assert d["n_buys"] == d["n_sells"] == 40 and abs(d["buy"] - 0.003) < 1e-6 and abs(d["sell"] - 0.005) < 1e-6
    assert abs(d["adverse"] - 0.01) < 1e-6  # worse side 0.5 % + 0.5 % buffer
    old = execution.paper_adverse()
    try:
        learn.calibrate_fills(conn)
        assert abs(execution.paper_adverse() - 0.01) < 1e-6
    finally:
        execution.set_paper_adverse(old)
