"""brain/: offline tests on a temporary DB (no network, no data/, no .env)."""
import json
import random
import time

import pytest

from brain import config, journal, learner, store
from brain.decide import choose
from brain.features import tokens
from brain.model import Brain
from brain.plans import BASE, NAMES, PLANS, grid_index
from brain.service import Service
from brain.trader import Trader
from copytrade import learn as ct_learn, sim, store as ct_store
from copytrade.execution import Fill
from copytrade.market import PairSnapshot

MINT = "Mint1111111111111111111111111111111111111111"
WHALE = "Wha1e11111111111111111111111111111111111111"


@pytest.fixture
def s(tmp_path, monkeypatch):
    monkeypatch.setenv("MAYHAM_DB_PATH", str(tmp_path / "t.db"))
    st = config.Settings()
    st.telegram = "off"
    return st


@pytest.fixture
def conn(s):
    c = store.connect(s.db_path)
    c.executescript(ct_store.SCHEMA)
    ct_learn.init(c)
    ct_store._winfo_cache.clear()
    return c


def snap(price=1e-6, liq=50_000, buys=20, sells=10, mint=MINT, symbol="TST", dex="raydium", fdv=200_000):
    return PairSnapshot(mint, symbol, "pair", dex, price * 150, price, liq, fdv, 1000, 5000, buys, sells,
                        100, 2.0, 5.0, int((time.time() - 3 * 3600) * 1000), time.time())


class Market:
    def __init__(self):
        self.snaps = {}

    def __call__(self, mints):
        return {m: self.snaps[m] for m in mints if m in self.snaps}


class Paper:
    def buy(self, mint, size, sn):
        return Fill(True, tokens=size / (sn.price_sol * 1.02), sol=size, price_sol=sn.price_sol * 1.02, decimals=6,
                    source="test")

    def sell(self, mint, tokens, decimals, sn, last):
        px = (sn.price_sol if sn else 0) * 0.98
        return Fill(True, tokens=tokens, sol=tokens * px, price_sol=px, source="test")


def ok_safety(conn, mint):
    return "low", False, True, []


def trained(kind="whale", good=("tier=ultra",), n=300, mean=0.2):
    """A model that learned: contexts with a 'good' token make +mean on every plan, the rest lose."""
    m = Brain(len(PLANS))
    rng = random.Random(1)
    for i in range(n):
        g = i % 2 == 0
        toks = [f"kind={kind}", *(good if g else ("tier=none",))]
        m.update(toks, {p: (mean if g else -0.15) + rng.gauss(0, 0.05) for p in range(len(PLANS))})
    return m


def make_trader(conn, s, model, market, **kw):
    kw.setdefault("safety_check", ok_safety)
    return Trader(conn, s, model, snapshots=market, executor=Paper(), rng=random.Random(7), **kw)


def add_whale_buy(conn, sig="sig1", wallet=WHALE, mint=MINT, sol=3.0, ts=None, price=1e-6):
    ts = int(ts or time.time())
    conn.execute("INSERT INTO ct_swaps VALUES (?,?,?,?,?,?,?,?,?)", (sig, wallet, mint, "buy", ts, 1e6, sol, price, ts))


def set_tier(conn, wallet, tier):
    conn.execute("INSERT OR REPLACE INTO wallets(address, tier) VALUES (?,?)", (wallet, tier))
    ct_store._winfo_cache.clear()


# ---------------------------------------------------------------------- model
def test_model_learns_context_and_uncertainty():
    m = trained()
    good = m.predict(["kind=whale", "tier=ultra"])
    bad = m.predict(["kind=whale", "tier=none"])
    assert all(g[0] > 0.1 for g in good) and all(b[0] < -0.1 for b in bad)
    unknown = m.predict(["kind=whale", "tier=ultra", "never=seen"])
    assert unknown[0][1] > good[0][1]  # an unseen token adds uncertainty instead of being ignored
    s = config.Settings()
    rng = random.Random(3)
    picks = [choose(m, "whale", {"tier": "ultra"}, s, rng) for _ in range(20)]
    skips = [choose(m, "whale", {"tier": "none"}, s, rng) for _ in range(20)]
    assert all(d.enter and not d.explore for d in picks)
    assert all(d.size_sol > s.min_size_sol for d in picks)  # sure of it: more than the minimum
    assert not any(d.enter for d in skips)


def test_model_save_load_and_forget(tmp_path):
    m = trained()
    p = tmp_path / "m.npz"
    m.save(p)
    m2 = Brain.load(p, len(PLANS), 120)
    assert m2.vocab == m.vocab
    assert m2.predict(["kind=whale", "tier=ultra"])[0][0] == pytest.approx(m.predict(["kind=whale", "tier=ultra"])[0][0])
    before = m2.predict(["kind=whale", "tier=ultra"])[0]
    m2.forget(m2.forgot_at + 10 * 120 * 3600)  # ten half-lives: almost back to the prior
    after = m2.predict(["kind=whale", "tier=ultra"])[0]
    assert abs(after[0]) < abs(before[0]) and after[1] > before[1]


def test_plans_are_grid_points():
    for p in PLANS:
        assert sim.GRID[grid_index(p)].take_profit == p.take_profit
        assert sim.EXIT_GRID[grid_index(p, sim.EXIT_GRID)].max_hold_min == p.max_hold_min


# ---------------------------------------------------------------------- trading
def test_first_start_skips_backlog_then_buys_good_whale(conn, s):
    add_whale_buy(conn, "old")  # before the first scan: backlog, never traded
    set_tier(conn, WHALE, "ultra")
    mk = Market()
    mk.snaps[MINT] = snap()
    t = make_trader(conn, s, trained(good=("tier=ultra",)), mk)
    assert t.scan() == 0
    assert conn.execute("SELECT COUNT(*) FROM brain_opps").fetchone()[0] == 0
    add_whale_buy(conn, "new")
    assert t.scan() == 1
    o = conn.execute("SELECT * FROM brain_opps").fetchone()
    assert o["decision"] == "enter" and o["wanted"] == 1 and o["position_id"]
    assert "Wal" in o["note"] and "Erwartung" in o["note"] and 2 <= len(o["note"].splitlines()) <= 4
    p = conn.execute("SELECT * FROM brain_positions").fetchone()
    assert p["status"] == "open" and p["plan"] in NAMES and p["whale_exit"] == 1
    assert store.balance(conn, s.start_balance_sol) == pytest.approx(s.start_balance_sol - p["size_sol"])
    add_whale_buy(conn, "again")  # same whale, same coin: no new opportunity
    t.scan()
    assert conn.execute("SELECT COUNT(*) FROM brain_opps").fetchone()[0] == 1


def test_bad_context_is_passed_and_noted(conn, s):
    set_tier(conn, WHALE, "none")
    mk = Market()
    mk.snaps[MINT] = snap()
    t = make_trader(conn, s, trained(good=("tier=ultra",)), mk)
    t.scan()
    add_whale_buy(conn, "x")
    assert t.scan() == 0
    o = conn.execute("SELECT * FROM brain_opps").fetchone()
    assert o["decision"] == "skip" and o["wanted"] == 0 and "Ausgelassen" in o["note"]
    assert conn.execute("SELECT COUNT(*) FROM brain_positions").fetchone()[0] == 0


def test_hard_gates_override_the_model(conn, s):
    set_tier(conn, WHALE, "ultra")
    mk = Market()
    mk.snaps[MINT] = snap(liq=1000)
    t = make_trader(conn, s, trained(), mk)
    t.scan()
    add_whale_buy(conn, "a")
    t.scan()
    o = conn.execute("SELECT * FROM brain_opps").fetchone()
    assert o["wanted"] == 1 and o["decision"] == "skip" and "Liquidität zu klein" in o["note"]
    mint2 = "Mint2222222222222222222222222222222222222222"
    mk.snaps[mint2] = snap(mint=mint2)
    t.safety_check = lambda c, m: ("critical", True, True, ["Mint-Authority aktiv"])
    add_whale_buy(conn, "b", mint=mint2)
    t.scan()
    o = conn.execute("SELECT * FROM brain_opps WHERE mint=?", (mint2,)).fetchone()
    assert o["decision"] == "skip" and "Rug-Schutz" in o["note"]


def test_paper_has_no_budget(conn, s):
    from brain import store as bstore

    set_tier(conn, WHALE, "ultra")
    mk = Market()
    t = make_trader(conn, s, trained(), mk)
    t.scan()
    bstore.put(conn, "balance_sol", -3.0)  # paper account deep in the red after a bad day
    conn.execute("INSERT INTO brain_positions(mint, status, closed_at, pnl_sol) VALUES ('old', 'closed', ?, -4.0)",
                 (int(time.time()),))
    for i in range(8):
        m = f"Coin{i}" + "1" * 39
        mk.snaps[m] = snap(mint=m)
        add_whale_buy(conn, f"b{i}", wallet=f"W{i}" + "1" * 42, mint=m)
        set_tier(conn, f"W{i}" + "1" * 42, "ultra")
    t.scan()
    assert conn.execute("SELECT COUNT(*) FROM brain_positions WHERE status='open'").fetchone()[0] == 8
    assert bstore.balance(conn, s.start_balance_sol) < -3.0


def test_exits_and_learning_from_the_closed_trade(conn, s):
    set_tier(conn, WHALE, "ultra")
    mk = Market()
    mk.snaps[MINT] = snap()
    model = trained()
    svc = Service(s, conn, snapshots=mk, executor=Paper(), safety_check=ok_safety, rng=random.Random(2),
                  notify=lambda t: None)
    svc.model = model
    svc.trader.model = model
    svc.trader.scan()
    add_whale_buy(conn, "n")
    assert svc.trader.scan() == 1
    p = conn.execute("SELECT * FROM brain_positions").fetchone()
    n0 = model.n
    mk.snaps[MINT] = snap(price=p["entry_price_sol"] * (1 + p["take_profit"] + 0.05))
    assert svc.trader.manage() == 1
    p = conn.execute("SELECT * FROM brain_positions").fetchone()
    assert p["status"] == "closed" and p["exit_reason"] == "target" and p["pnl_sol"] > 0
    assert conn.execute("SELECT learned_real FROM brain_opps").fetchone()[0] == 1
    assert model.n > n0 and svc.dirty


def test_whale_sell_and_rug_exits(conn, s):
    set_tier(conn, WHALE, "ultra")
    mk = Market()
    mk.snaps[MINT] = snap()
    t = make_trader(conn, s, trained(), mk)
    t.scan()
    add_whale_buy(conn, "n")
    t.scan()
    conn.execute("INSERT INTO ct_swaps VALUES (?,?,?,?,?,?,?,?,?)",
                 ("sell1", WHALE, MINT, "sell", int(time.time()), 1e6, 3, 1e-6, int(time.time())))
    t.manage()
    assert conn.execute("SELECT exit_reason FROM brain_positions").fetchone()[0] == "whale"


def sell(conn, sig, wallet=WHALE, mint=MINT, ts=None):
    ts = int(ts or time.time())
    conn.execute("INSERT INTO ct_swaps VALUES (?,?,?,?,?,?,?,?,?)", (sig, wallet, mint, "sell", ts, 1e6, 3, 1e-6, ts))


def test_no_copy_when_the_whale_is_already_out_or_flips(conn, s, caplog):
    caplog.set_level("INFO", logger="brain")
    set_tier(conn, WHALE, "ultra")
    mk = Market()
    mk.snaps[MINT] = snap()
    t = make_trader(conn, s, trained(), mk)
    t.scan()
    now = int(time.time())
    add_whale_buy(conn, "early", ts=now - 20)
    sell(conn, "out", ts=now - 5)  # sold again before we could decide
    t.scan()
    o = conn.execute("SELECT decision, note FROM brain_opps").fetchone()
    assert o["decision"] == "skip" and "schon wieder verkauft" in o["note"]
    assert any(r.message.startswith("PASS whale") and "schon wieder verkauft" in r.message for r in caplog.records)
    # a wallet that sells most coins within two minutes: no copy, and its habit is a token the model learns from
    w2 = "Flip111111111111111111111111111111111111111"
    set_tier(conn, w2, "ultra")
    for i in range(6):
        m = f"Old{i}" + "1" * 40
        add_whale_buy(conn, f"fb{i}", wallet=w2, mint=m, ts=now - 3600 * (i + 1))
        sell(conn, f"fs{i}", wallet=w2, mint=m, ts=now - 3600 * (i + 1) + 40)
    t.scan()  # cursor past the history
    add_whale_buy(conn, "flipbuy", wallet=w2)
    t.scan()
    o = conn.execute("SELECT decision, note, features FROM brain_opps WHERE ref='flipbuy'").fetchone()
    assert o["decision"] == "skip" and "sofort wieder" in o["note"]
    assert json.loads(o["features"])["flip"] == ">50%"
    assert conn.execute("SELECT COUNT(*) FROM brain_positions").fetchone()[0] == 0


def test_no_copy_when_the_coin_already_ran(conn, s, monkeypatch):
    from copytrade import learn

    real = learn.intel_context

    def ran(conn, mint, wallet, now):
        ib, iraw = real(conn, mint, wallet, now)
        return {**ib, "run5": ">5%"}, {**iraw, "px_ret_5m": 0.08}

    monkeypatch.setattr(learn, "intel_context", ran)
    set_tier(conn, WHALE, "ultra")
    mk = Market()
    mk.snaps[MINT] = snap()
    t = make_trader(conn, s, trained(), mk)
    t.scan()
    add_whale_buy(conn, "n")
    t.scan()
    o = conn.execute("SELECT decision, note, features FROM brain_opps").fetchone()
    assert o["decision"] == "skip" and "schon gelaufen (+8% in 5 min" in o["note"]
    assert json.loads(o["features"])["run5"] == ">5%"


def test_fresh_coin_run_and_rug_verdict_reach_the_brain(conn, s):
    set_tier(conn, WHALE, "ultra")
    mk = Market()
    sn = snap()
    sn.change_m5 = 9.0  # DexScreener: +9 % in 5 min, intel has no price history for this coin yet
    mk.snaps[MINT] = sn
    t = make_trader(conn, s, trained(), mk, safety_check=lambda c, m: ("medium", False, True, []))
    t.scan()
    add_whale_buy(conn, "n")
    t.scan()
    o = conn.execute("SELECT decision, note, features FROM brain_opps").fetchone()
    assert o["decision"] == "skip" and "schon gelaufen (+9% in 5 min" in o["note"]
    assert json.loads(o["features"])["safety"] == "medium"


def test_honest_score_keeps_explorations_apart(conn, s):
    for i, (wanted, explore) in enumerate(((1, 0), (0, 1), (0, 0))):
        conn.execute("INSERT INTO brain_opps(ts, kind, ref, mint, features, wanted, explore, evaluated_at, "
                     "realized_plan, realized_base) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (int(time.time()), "whale", f"h{i}", MINT, "{}", wanted, explore, int(time.time()), 0.1 * i, 0.0))
    h = learner.honest(conn)
    assert (h["n_taken"], h["n_explored"], h["n_passed"]) == (1, 1, 1)


def test_no_rebuy_of_a_coin_just_traded(conn, s):
    set_tier(conn, WHALE, "ultra")
    w2 = "Whale22222222222222222222222222222222222222"
    set_tier(conn, w2, "ultra")
    mk = Market()
    mk.snaps[MINT] = snap()
    t = make_trader(conn, s, trained(), mk)
    t.scan()
    add_whale_buy(conn, "a")
    t.scan()
    sell(conn, "a_out")
    t.manage()
    assert conn.execute("SELECT status FROM brain_positions").fetchone()[0] == "closed"
    add_whale_buy(conn, "b", wallet=w2)  # another whale, same coin, minutes later
    t.scan()
    o = conn.execute("SELECT decision, note FROM brain_opps WHERE ref='b'").fetchone()
    assert o["decision"] == "skip" and "6 h schon gehandelt" in o["note"]
    assert conn.execute("SELECT COUNT(*) FROM brain_positions").fetchone()[0] == 1


def test_bonding_curve_coins_and_late_pairs_are_tradable(conn, s):
    set_tier(conn, WHALE, "ultra")
    mk = Market()
    clock = [time.time()]
    t = make_trader(conn, s, trained(), mk, clock=lambda: clock[0])
    t.scan()
    add_whale_buy(conn, "n", ts=clock[0])
    t.scan()  # no DexScreener pair yet: the opportunity waits instead of being passed as "kein Marktpreis"
    assert conn.execute("SELECT COUNT(*) FROM brain_opps").fetchone()[0] == 0
    mk.snaps[MINT] = snap(liq=0, dex="pumpfun", fdv=20_000)  # still on the pump.fun curve: no pool liquidity
    clock[0] += 5
    assert t.scan() == 1
    o = conn.execute("SELECT features FROM brain_opps").fetchone()
    assert json.loads(o["features"])["liq"] == "curve"
    # a curve that is still tiny is passed; a coin that never gets a pair is decided after the retry window
    m2 = "Mint2222222222222222222222222222222222222222"
    m3 = "Mint3333333333333333333333333333333333333333"
    w2, w3 = "W2" + "2" * 41, "W3" + "3" * 41
    set_tier(conn, w2, "ultra")
    set_tier(conn, w3, "ultra")
    mk.snaps[m2] = snap(liq=0, dex="pumpfun", fdv=4_000, mint=m2)
    add_whale_buy(conn, "small", wallet=w2, mint=m2, ts=clock[0])
    add_whale_buy(conn, "nopair", wallet=w3, mint=m3, ts=clock[0])
    t.scan()
    assert "Bonding Curve noch zu klein" in conn.execute("SELECT note FROM brain_opps WHERE ref='small'").fetchone()[0]
    assert conn.execute("SELECT 1 FROM brain_opps WHERE ref='nopair'").fetchone() is None
    clock[0] += 31
    t.scan()
    assert "kein Marktpreis" in conn.execute("SELECT note FROM brain_opps WHERE ref='nopair'").fetchone()[0]


def test_trail_waits_a_minute_and_compares_market_prices(conn, s):
    set_tier(conn, WHALE, "ultra")
    mk = Market()
    mk.snaps[MINT] = snap()
    clock = [time.time()]
    t = make_trader(conn, s, trained(), mk, clock=lambda: clock[0])
    t.scan()
    add_whale_buy(conn, "n", ts=clock[0])
    assert t.scan() == 1
    p = conn.execute("SELECT * FROM brain_positions").fetchone()
    conn.execute("UPDATE brain_positions SET plan='pump', take_profit=0.4, stop_loss=0.2, trail_activate=0.15, "
                 "trail_drawdown=0.08")
    mk.snaps[MINT] = snap(price=1.2e-6)  # +20 % on the market two seconds in: a spike, not a run to trail yet
    clock[0] += 2
    t.manage()
    mk.snaps[MINT] = snap(price=1.08e-6)
    clock[0] += 2
    t.manage()
    assert conn.execute("SELECT status FROM brain_positions").fetchone()[0] == "open"
    # after a minute the trail arms on the market price at the buy (1e-6), not on the fill with slippage
    mk.snaps[MINT] = snap(price=1.2e-6)
    clock[0] += 70
    t.manage()
    mk.snaps[MINT] = snap(price=1.09e-6)
    clock[0] += 5
    t.manage()
    p = conn.execute("SELECT * FROM brain_positions").fetchone()
    assert p["status"] == "closed" and p["exit_reason"] == "trail"


def test_signal_and_launch_are_opportunities(conn, s):
    mk = Market()
    m2 = "Mint3333333333333333333333333333333333333333"
    mk.snaps[MINT] = snap()
    mk.snaps[m2] = snap(mint=m2)
    conn.execute("CREATE TABLE IF NOT EXISTS sniper_launches (mint TEXT PRIMARY KEY, symbol TEXT, decided_ts INTEGER, "
                 "decision TEXT, score REAL, risk REAL, bundle_flag INTEGER, insider_flag INTEGER, dev_launches INTEGER,"
                 " top10_pct REAL, has_socials INTEGER, n_ultra INTEGER, n_smart INTEGER, model_p REAL, "
                 "reasons_json TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS sniper_positions (id INTEGER PRIMARY KEY, mint TEXT, opened_ts INTEGER)")
    t = make_trader(conn, s, Brain(len(PLANS)), mk)
    t.scan()
    now = int(time.time())
    conn.execute("INSERT INTO signals(ts, mint, symbol, score, net_usd, smart_buyers, price_usd, mcap_usd, ev, call_no)"
                 " VALUES (?,?,?,?,?,?,?,?,?,?)", (now, MINT, "TST", 70, 900, 3, 1.5e-4, 2e5, 0.03, 2))
    conn.execute("INSERT INTO sniper_launches VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (m2, "LNC", now, "enter", 72, 30, 0, 0, 1, 25, 1, 0, 1, 0.2, '["Dev neu"]'))
    conn.execute("INSERT INTO sniper_positions(mint, opened_ts) VALUES (?,?)", (m2, now + 1))
    t.scan()
    kinds = {r["kind"]: r for r in conn.execute("SELECT * FROM brain_opps").fetchall()}
    assert set(kinds) == {"signal", "launch"}
    f = json.loads(kinds["signal"]["features"])
    assert f["call"] == "2" and f["ev"] == "0-5%"
    assert "Signal" in kinds["signal"]["note"] and "Launch" in kinds["launch"]["note"]
    # an untrained brain explores at most the daily budget, always with the smallest size
    for o in conn.execute("SELECT * FROM brain_opps WHERE decision='enter'"):
        assert o["explore"] == 1 and o["size_sol"] == s.min_size_sol


# ---------------------------------------------------------------------- learning
def candles_up(conn, mint, lo, hi, allow_net=True):
    """Minute candles: flat for 20 min, then +60 % within 30 min."""
    out, px = [], 1.0
    for i, ts in enumerate(range(lo - lo % 60, hi, 60)):
        if i > 22:
            px *= 1.02
        out.append((ts, px, px * 1.005, px * 0.995, px, 1000.0))
    return out


def test_counterfactual_replay_scores_and_teaches(conn, s):
    model = Brain(len(PLANS))
    old = time.time() - learner.horizon_s() - learner.CACHE_GRACE_S - 600
    for i, wanted in enumerate((1, 0)):
        conn.execute("INSERT INTO brain_opps(ts, decided_at, kind, ref, mint, wallet, entity, src_ts, features, decision,"
                     " wanted, plan, pred_mean) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (int(old), old, "signal", f"s{i}", MINT, "", "signal", int(old),
                      json.dumps({"score": "70+", "ev": "0-5%"}), "skip", wanted, "runner", 0.01))
    n = learner.evaluate(conn, model, s, candles_up, limit=10)
    assert n == 2
    rows = conn.execute("SELECT * FROM brain_opps").fetchall()
    assert all(r["evaluated_at"] and r["realized_plan"] is not None for r in rows)
    assert rows[0]["realized_plan"] > 0  # the runner plan caught the climb, after costs
    assert model.n == 2 and model.predict(tokens("signal", {"score": "70+"}))[NAMES.index("runner")][0] > 0
    h = learner.honest(conn)
    assert h["n"] == 2 and h["n_taken"] == 1 and h["n_passed"] == 1 and h["copy_all_mean"] is not None


def test_short_plans_are_learned_after_an_hour(conn, s):
    model = Brain(len(PLANS))
    t0 = time.time() - learner.short_horizon_s() - 300
    conn.execute("INSERT INTO brain_opps(ts, decided_at, kind, ref, mint, wallet, entity, src_ts, features, decision,"
                 " wanted, plan) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 (int(t0), t0, "signal", "s1", MINT, "", "signal", int(t0), json.dumps({"score": "70+"}), "skip", 0,
                  "pump"))
    assert learner.evaluate(conn, model, s, candles_up, limit=5) == 1
    o = conn.execute("SELECT * FROM brain_opps").fetchone()
    g = json.loads(o["gains"])
    assert o["evaluated_at"] is None and all(g[i] is not None for i in learner.SHORT)
    assert all(g[i] is None for i in range(len(PLANS)) if i not in learner.SHORT)
    assert learner.evaluate(conn, model, s, candles_up, limit=5) == 0  # nothing new until the long plans are over
    later = t0 + learner.horizon_s() + learner.CACHE_GRACE_S + 60
    assert learner.evaluate(conn, model, s, candles_up, now=later, limit=5) == 1
    o = conn.execute("SELECT * FROM brain_opps").fetchone()
    assert o["evaluated_at"] and all(x is not None for x in json.loads(o["gains"]))
    assert o["realized_plan"] is not None and model.n > 1.9  # two steps (forgetting trims n a little)


def test_no_candles_retries_then_gives_up(conn, s):
    model = Brain(len(PLANS))
    old = time.time() - 3 * 86400
    conn.execute("INSERT INTO brain_opps(ts, decided_at, kind, ref, mint, features, plan) VALUES (?,?,?,?,?,?,?)",
                 (int(old), old, "launch", "x", MINT, "{}", "pump"))
    assert learner.evaluate(conn, model, s, lambda *a: [], limit=5) == 0
    assert conn.execute("SELECT gains FROM brain_opps").fetchone()[0] == "[]"


def test_warm_start_from_copytrade_replays(conn, s):
    started = int(time.time())
    store.put(conn, "started_at", started)
    gains = [None] * len(sim.GRID)
    bad = [None] * len(sim.GRID)
    for p in PLANS:
        for dl in sim.DELAYS_S:
            gains[grid_index(p, delay_s=dl)] = 0.25
            bad[grid_index(p, delay_s=dl)] = -0.2
    for i in range(40):
        g, tier = (gains, "ultra") if i % 2 else (bad, "none")
        conn.execute("INSERT INTO ct_evals VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     ("whale_buy", f"w{i}", "whale", WHALE, MINT, started - 3600 * (i + 1), sim.GRID_VERSION,
                      json.dumps(g), None, None, json.dumps({"strategy": "whale", "tier": tier}), started - 100 + i))
    model = Brain(len(PLANS))
    assert learner.warm_start(conn, model, s, now=started + 10) == 40
    assert learner.warm_start(conn, model, s, now=started + 20) == 0  # never twice
    assert learner.warm_start(conn, Brain(len(PLANS)), s, now=started + 30) == 40  # model file lost: relearn
    good = model.predict(tokens("whale", {"tier": "ultra"}))[NAMES.index(BASE)][0]
    bad_ = model.predict(tokens("whale", {"tier": "none"}))[NAMES.index(BASE)][0]
    assert good > 0 > bad_
    chk = store.get(conn, "prior_check")
    assert chk["n"] == 40 and chk.get("n_base") == 40


def test_whale_replays_use_the_brains_own_delay(conn, s):
    from brain.plans import nearest_delay

    assert nearest_delay(10) == 15 and nearest_delay(75) == 60 and nearest_delay(150) == 180
    g = [0.0] * len(sim.GRID)
    for p in PLANS:
        g[grid_index(p, delay_s=15)] = 0.5  # copytrade's fast entry caught the pump
        g[grid_index(p, delay_s=60)] = -0.1  # a minute later it was gone
    conn.execute("INSERT INTO ct_evals VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 ("whale_buy", "sig9", "whale", WHALE, MINT, 1000, sim.GRID_VERSION, json.dumps(g), None, None, "{}",
                  2000))
    conn.execute("INSERT INTO brain_opps(ts, decided_at, kind, ref, mint, wallet, src_ts, features) "
                 "VALUES (1070, 1070, 'whale', 'sig9', ?, ?, 1000, '{}')", (MINT, WHALE))
    o = conn.execute("SELECT * FROM brain_opps WHERE ref='sig9'").fetchone()
    assert set(learner._ct_gains(conn, o)) == {-0.1}


def test_empty_model_relearns_own_outcomes(conn, s):
    gains = [0.3] * len(PLANS)
    for i in range(6):
        conn.execute("INSERT INTO brain_opps(ts, kind, ref, mint, features, evaluated_at, gains) VALUES (?,?,?,?,?,?,?)",
                     (int(time.time()) - 3600, "whale", f"r{i}", MINT, json.dumps({"tier": "ultra"}),
                      int(time.time()), json.dumps(gains)))
    store.put(conn, "prior_cursor", 123)  # the brain ran before; its model file is gone or damaged
    model = Brain(len(PLANS))
    learner.warm_start(conn, model, s)
    assert model.n > 5 and model.predict(tokens("whale", {"tier": "ultra"}))[0][0] > 0


def test_journal_reads_like_a_diary(conn, s):
    model = trained()
    conn.execute("INSERT INTO brain_opps(ts, kind, ref, mint, features, decision, wanted, plan) VALUES (?,?,?,?,?,?,?,?)",
                 (int(time.time()), "whale", "j1", MINT, json.dumps({"tier": "ultra"}), "skip", 0, "pump"))
    text = journal.write(conn, model, s)
    assert "Lerntagebuch" in text and "Echtgeld bleibt aus" in text and "Gesehen: 1 Gelegenheiten" in text
    s.diary_hour_utc = 0
    assert journal.daily(conn, model, s) is not None
    assert journal.daily(conn, model, s) is None  # once a day


def test_status_api(conn, s):
    import brain

    st = brain.status(conn)
    assert st["balance_sol"] == s.start_balance_sol and st["honest"]["n"] == 0
    assert brain.decisions(conn) == [] and brain.diary(conn) == []


def test_launcher_knows_the_service():
    import mayham

    assert mayham.SERVICES["brain"][-2:] == ["brain", "run"]


def test_service_starts_without_model_and_keeps_running(s, caplog):
    """The auto-updater counts any exit within 3 min of a deploy as a crash: no model file must not end the service."""
    import logging
    import threading

    assert not s.model_path.exists()
    stop = threading.Event()
    svc = Service(s, snapshots=Market(), executor=Paper(), safety_check=ok_safety, notify=lambda t: None)
    s.scan_every_s = 0.05
    with caplog.at_level(logging.INFO, logger="brain"):
        th = threading.Thread(target=svc.run, args=(stop,), daemon=True)
        th.start()
        time.sleep(0.6)
        assert th.is_alive()
        stop.set()
        th.join(5)
    assert not th.is_alive()
    text = caplog.text
    assert "brain started" in text and "no saved model yet" in text and "keeps running" in text
