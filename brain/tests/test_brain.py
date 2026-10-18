"""brain/: offline tests on a temporary DB (no network, no data/, no .env)."""
import json
import random
import time

import pytest

from brain import config, journal, learner, store
from brain.decide import choose
from brain.features import tokens
from brain.model import Brain
from brain.plans import BASE, BY_NAME, NAMES, PLANS, grid_index
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


def test_late_whale_buys_are_still_decided(conn, s):
    set_tier(conn, WHALE, "ultra")
    mk = Market()
    mk.snaps[MINT] = snap()
    t = make_trader(conn, s, trained(), mk)
    t.scan()
    add_whale_buy(conn, "late", ts=time.time() - 200)  # the tracker lagged behind (RPC rate limits)
    add_whale_buy(conn, "toolate", mint="Mint9" + "9" * 39, ts=time.time() - 400)
    t.scan()
    o = conn.execute("SELECT features FROM brain_opps WHERE ref='late'").fetchone()
    assert json.loads(o["features"])["delay"] == ">90s"
    assert conn.execute("SELECT 1 FROM brain_opps WHERE ref='toolate'").fetchone() is None and t.too_late == 1


def test_signal_v2_learns_apart_from_v1():
    t1 = tokens("signal", {"sig": "v1", "score": "70+", "mom": "0-10%"})
    t2 = tokens("signal", {"sig": "v2", "score": "70+", "mom": "0-10%"})
    assert "score=70+" in t1 and "score=70+" not in t2 and "v2:score=70+" in t2 and "sig=v2" in t2
    assert "mom=0-10%" in t2 and "signal_v2:mom=0-10%" in t2  # market context still generalizes


def test_honest_score_keeps_explorations_apart(conn, s):
    for i, (wanted, explore) in enumerate(((1, 0), (0, 1), (0, 0))):
        conn.execute("INSERT INTO brain_opps(ts, kind, ref, mint, features, wanted, explore, evaluated_at, "
                     "realized_plan, realized_base) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (int(time.time()), "whale", f"h{i}", MINT, "{}", wanted, explore, int(time.time()), 0.1 * i, 0.0))
    h = learner.honest(conn)
    assert (h["n_taken"], h["n_explored"], h["n_passed"]) == (1, 1, 1)


def test_no_exploration_in_segments_that_lose_at_entry(s):
    class Unsure:  # every plan around zero, wide: never convinced, a draw often says "could pay"
        def predict(self, toks):
            return [(0.0, 0.2)] * len(PLANS)

        def contributions(self, toks, i):
            return []
    rng = random.Random(1)
    fresh = [choose(Unsure(), "whale", {"delay": "5-15s", "liq": "10-50k"}, s, rng) for _ in range(40)]
    late = [choose(Unsure(), "whale", {"delay": "30-90s", "liq": "10-50k"}, s, rng) for _ in range(40)]
    curve = [choose(Unsure(), "whale", {"delay": "<5s", "liq": "curve"}, s, rng) for _ in range(40)]
    assert any(d.explore for d in fresh)
    assert not any(d.enter for d in late + curve)
    assert "keine Erkundung" in late[0].why_not and "curve" in curve[0].why_not


def test_honest_score_by_delay_and_curve(conn, s):
    for i, (f, base) in enumerate(((("<5s", "curve"), -0.2), (("30-90s", "10-50k"), -0.1), (("<5s", "10-50k"), 0.05))):
        conn.execute("INSERT INTO brain_opps(ts, kind, ref, mint, features, wanted, explore, evaluated_at, "
                     "realized_plan, realized_base) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (int(time.time()), "whale", f"d{i}", MINT, json.dumps({"delay": f[0], "liq": f[1]}), 0, 0,
                      int(time.time()), base, base))
    h = learner.honest(conn)
    assert h["by_delay"]["<5s"]["n"] == 2 and h["by_delay"]["30-90s"]["copy_all_mean"] == -0.1
    assert h["by_liq"]["curve"]["n"] == 1 and h["by_liq"]["pool"]["n"] == 2


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


# ---------------------------------------------------------------------- organic exits
from brain import exits  # noqa: E402


def candles_pump_dump(conn, mint, lo, hi, allow_net=True):
    """Flat 20 min, +4 %/min with rising volume for 15 min, then the volume dries out and it bleeds -3 %/min."""
    out, px = [], 1.0
    for i, ts in enumerate(range(lo - lo % 60, hi, 60)):
        vol = 1000.0
        if 22 < i <= 37:
            px *= 1.04
            vol = 1000.0 + 400 * (i - 22)
        elif i > 37:
            px *= 0.97
            vol = 300.0
        out.append((ts, px, px * 1.005, px * 0.995, px, vol))
    return out


def test_exit_model_learns_when_a_run_is_over_and_beats_the_fixed_plan(s):
    xm = exits.new_model(120)
    t0 = 1_700_000_000
    for k in range(12):  # twelve coins with the same shape teach it: rising volume carries, dried volume ends it
        lo = t0 + k * 86400
        cs = candles_pump_dump(None, MINT, lo - 20 * 60, lo + 300 * 60)
        assert exits.learn_path(xm, cs, lo, "whale", "10-50k", None, [], [], 0, exits.MAX_HOLD_MIN) > 100
    assert exits.mature(xm)
    lo = t0 + 99 * 86400
    cs = candles_pump_dump(None, MINT, lo - 20 * 60, lo + 300 * 60)
    gain, held, why = exits.replay(xm, cs, lo, "whale", "10-50k", None, [], [], s, 0.03)
    plan = sim.simulate(cs, lo, BY_NAME["pump"].variant(0))[0]  # fixed +40 % target
    assert why == "organic" and gain > plan and gain > 0.3  # rode the run past +40 % and left when it turned


def test_trader_sells_half_then_the_rest_when_the_state_turns(conn, s):
    class Outlook:  # stands in for a trained exit model: P(next 10 min up) set by the test
        n = 10_000
        p = 0.45

        def predict(self, toks):
            from statistics import NormalDist
            return [(NormalDist().inv_cdf(self.p) * 0.1, 0.1)]
    s.organic_exit = "on"
    set_tier(conn, WHALE, "ultra")
    mk = Market()
    mk.snaps[MINT] = snap()
    clock = [time.time()]
    xm = Outlook()
    t = make_trader(conn, s, trained(), mk, clock=lambda: clock[0], exit_model=xm)
    t.scan()
    add_whale_buy(conn, "n", ts=clock[0])
    assert t.scan() == 1
    p0 = conn.execute("SELECT * FROM brain_positions").fetchone()
    bal0 = store.balance(conn, s.start_balance_sol)
    assert t.manage() == 0  # the buy minute: watched, never judged
    clock[0] += 180
    mk.snaps[MINT] = snap(price=p0["entry_price_sol"] * 1.5)  # +50 %, the outlook doubtful: half off the table
    assert t.manage() == 0
    p1 = conn.execute("SELECT * FROM brain_positions").fetchone()
    assert p1["tokens"] == pytest.approx(p0["tokens"] / 2) and json.loads(p1["meta"])["partial_sol"] > 0
    assert store.balance(conn, s.start_balance_sol) > bal0
    xm.p = 0.2  # the run is over
    clock[0] += 61
    assert t.manage() == 1
    p2 = conn.execute("SELECT * FROM brain_positions").fetchone()
    assert p2["exit_reason"] == "organic" and p2["pnl_pct"] > 0.3
    assert p2["proceeds_sol"] == pytest.approx(json.loads(p2["meta"])["partial_sol"] + p1["tokens"] * mk.snaps[MINT].price_sol * 0.98)
    h = learner.honest(conn)
    assert h["by_exit"]["organic"]["n"] == 1 and h["by_exit"]["organic"]["capture"] > 0.5


def test_trader_keeps_the_crash_stop_and_plans_while_organic_is_unproven(conn, s):
    set_tier(conn, WHALE, "ultra")
    mk = Market()
    mk.snaps[MINT] = snap()
    xm = exits.new_model(120)
    t = make_trader(conn, s, trained(), mk, exit_model=xm)  # auto mode, untrained: the plans decide
    assert not t.organic_on(time.time())
    t.scan()
    add_whale_buy(conn, "n")
    t.scan()
    p = conn.execute("SELECT * FROM brain_positions").fetchone()
    mk.snaps[MINT] = snap(price=p["entry_price_sol"] * (1 + p["take_profit"] + 0.05))
    assert t.manage() == 1
    assert conn.execute("SELECT exit_reason FROM brain_positions").fetchone()[0] == "target"


def test_replays_compare_organic_with_the_plan_out_of_sample(conn, s):
    model = Brain(len(PLANS))
    xm = exits.new_model(120)
    for k in range(12):
        lo = 1_700_000_000 + k * 86400
        exits.learn_path(xm, candles_pump_dump(None, MINT, lo - 1200, lo + 18000), lo, "signal", None, None, [], [],
                         0, exits.MAX_HOLD_MIN)
    old = time.time() - learner.horizon_s() - learner.CACHE_GRACE_S - 600
    conn.execute("INSERT INTO brain_opps(ts, decided_at, kind, ref, mint, wallet, entity, src_ts, features, decision,"
                 " wanted, plan) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 (int(old), old, "signal", "s1", MINT, "", "signal", int(old), json.dumps({"score": "70+"}), "skip", 1,
                  "pump"))
    n0 = xm.n
    assert learner.evaluate(conn, model, s, candles_pump_dump, limit=5, xm=xm) == 1
    e = conn.execute("SELECT * FROM brain_exit_evals").fetchone()
    assert e["organic"] is not None and e["plan_gain"] is not None and e["max_up"] > 0.5 and e["hold_1h"] is not None
    assert e["organic"] > e["plan_gain"] and xm.n > n0
    xr = learner.honest(conn)["exit_replays"]
    assert xr["n"] == 1 and xr["capture_organic"] > xr["capture_plan"]
    assert exits.verdict(conn)["n"] == 1 and not exits.verdict(conn)["live"]  # one coin proves nothing yet
    assert exits.verdict(conn, "signal")["n"] == 1 and exits.verdict(conn, "whale")["n"] == 0
    text = journal.write(conn, model, s)
    assert "Ausstieg im Vergleich" in text


def test_exit_backfill_learns_finished_opportunities_once(conn, s):
    old = time.time() - 2 * 86400
    conn.execute("INSERT INTO brain_opps(ts, decided_at, kind, ref, mint, features, plan, evaluated_at, gains) "
                 "VALUES (?,?,?,?,?,?,?,?,?)", (int(old), old, "signal", "s1", MINT, "{}", "pump", int(old) + 20000,
                                                 json.dumps([0.1] * len(PLANS))))
    xm = exits.new_model(120)
    assert learner.backfill_exits(conn, xm, s, candles_pump_dump) == 1 and xm.n > 0
    assert learner.backfill_exits(conn, xm, s, candles_pump_dump) == 0


def test_diary_shows_the_daily_multiplier(conn, s):
    d0 = store.day_start(time.time())
    now = d0 + 7200
    for ts, eq in ((d0 - 86400 + 60, 5.0), (d0 - 120, 4.5), (d0 + 60, 4.5), (d0 + 3600, 9.0)):
        conn.execute("INSERT OR REPLACE INTO brain_equity VALUES (?,?,?,?)", (int(ts), eq, 0, eq))
    m = journal.multipliers(conn, now)
    assert [x for _, x in m] == [0.9, 2.0]
    assert "Tagesfaktor" in journal.write(conn, Brain(len(PLANS)), s, now) and "×2.00" in journal.write(conn, Brain(len(PLANS)), s, now)


def test_regime_shift_fades_old_evidence_once(conn, s):
    model, xm = trained(), exits.new_model(120)
    xm.update(["kind=whale"], {0: 0.1})
    now = time.time()
    rng = random.Random(5)
    for i in range(40):  # predicted +10 %, the market now delivers -10 %
        conn.execute("INSERT INTO brain_opps(ts, kind, ref, mint, features, pred_mean, evaluated_at, realized_plan) "
                     "VALUES (?,?,?,?,?,?,?,?)", (int(now) - 9000, "whale", f"r{i}", MINT, "{}", 0.1, int(now) - 60,
                                                   -0.1 + rng.gauss(0, 0.05)))
    n0, x0 = model.n, xm.n
    shift = learner.regime_check(conn, model, xm, now)
    assert shift and shift["err"] < -0.15 and model.n == pytest.approx(n0 / 2) and xm.n == pytest.approx(x0 / 2)
    assert learner.regime_check(conn, model, xm, now + 60) is None  # cooldown: once, not every round
    assert "Regime gewechselt" in journal.write(conn, model, s, now)


def test_no_regime_shift_when_predictions_hold(conn, s):
    model = trained()
    now = time.time()
    rng = random.Random(6)
    for i in range(40):
        conn.execute("INSERT INTO brain_opps(ts, kind, ref, mint, features, pred_mean, evaluated_at, realized_plan) "
                     "VALUES (?,?,?,?,?,?,?,?)", (int(now) - 9000, "whale", f"q{i}", MINT, "{}", 0.0, int(now) - 60,
                                                   rng.gauss(0, 0.2)))
    assert learner.regime_check(conn, model, None, now) is None


# ---------------------------------------------------------------------- side source: market coins
def add_shadow(conn, i, mint=MINT, ts=None, reason="no_smart"):
    conn.execute("""CREATE TABLE IF NOT EXISTS signal_shadow (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER,
                    mint TEXT, kind TEXT, reason TEXT, entities INTEGER, net_usd REAL, score REAL, liquidity_usd REAL,
                    price_usd REAL, dex TEXT, mcap_usd REAL, age_min REAL, pc_m5 REAL, pc_h1 REAL, txns_m5 INTEGER,
                    txns_h1 INTEGER, volume_h1 REAL, ret_15m_before REAL)""")
    conn.execute("INSERT INTO signal_shadow(ts, mint, kind, reason, entities, dex, mcap_usd, age_min, pc_m5, pc_h1, "
                 "txns_m5, volume_h1, ret_15m_before) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (int(ts or time.time()), mint, "v1", reason, 0, "pumpswap", 80_000, 90, 12.0, 40.0, 150, 60_000, -0.32))


def test_market_coins_are_watched_not_traded_until_their_replays_earn_it(conn, s):
    s.challenge = "off"
    mk = Market()
    mk.snaps[MINT] = snap(dex="pumpswap")
    good = trained(kind="market", good=("dex=pumpswap",))
    checks = []
    t = make_trader(conn, s, good, mk, safety_check=lambda c, m: checks.append(m) or ("low", False, True, []))
    add_shadow(conn, 1)
    t.scan()  # first start: the backlog is skipped
    add_shadow(conn, 2)
    assert t.scan() == 0
    o = conn.execute("SELECT * FROM brain_opps").fetchone()
    f = json.loads(o["features"])
    assert o["kind"] == "market" and o["wanted"] == 1 and o["decision"] == "skip"
    assert f["dex"] == "pumpswap" and f["dip15"] == "<-30%" and f["smart"] == "0"
    assert "Markt-Coin" in o["note"] and "Nebenquelle" in o["note"] and not checks  # no RPC spent while watching
    now = time.time()
    for i in range(30):  # 30 out-of-sample replays of what it wanted from the source, net positive
        conn.execute("INSERT INTO brain_opps(ts, kind, ref, mint, features, wanted, evaluated_at, realized_plan, "
                     "realized_base) VALUES (?,?,?,?,?,?,?,?,?)", (int(now) - 20000, "market", f"m{i}", f"M{i}", "{}",
                                                                   1, int(now) - 100, 0.05, -0.02))
    t._market_at = (-1e18, False)
    assert t.market_live()
    mint2 = "Mint2222222222222222222222222222222222222222"
    mk.snaps[mint2] = snap(dex="pumpswap", mint=mint2)
    add_shadow(conn, 3, mint=mint2)
    assert t.scan() == 1 and checks == [mint2]
    assert "Nebenquelle Markt-Coins" in journal.write(conn, good, s)


def test_momentum_coins_from_the_tokens_table_are_a_side_source(conn, s):
    s.challenge = "off"
    conn.execute("""CREATE TABLE IF NOT EXISTS tokens (mint TEXT PRIMARY KEY, symbol TEXT, dex TEXT, mcap_usd REAL,
                    liquidity_usd REAL, pair_created_at INTEGER, price_change_m5 REAL, price_change_h1 REAL,
                    volume_h1 REAL, txns_m5 INTEGER, last_updated INTEGER)""")
    mk = Market()
    mk.snaps[MINT] = snap(dex="pumpswap")
    t = make_trader(conn, s, trained(kind="market"), mk)
    t.scan()  # first start: backlog skipped
    now = int(time.time()) + 1
    cols = "(mint, symbol, dex, mcap_usd, liquidity_usd, pair_created_at, price_change_m5, price_change_h1, volume_h1, " \
           "txns_m5, last_updated)"
    conn.execute(f"INSERT INTO tokens{cols} VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 (MINT, "TST", "pumpswap", 90_000, 40_000, (now - 3600) * 1000, 18.0, 60.0, 80_000, 220, now))
    conn.execute(f"INSERT INTO tokens{cols} VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 ("Flat1111111111111111111111111111111111111111", "FL", "pumpswap", 90_000, 40_000, 0, 1.0, 2.0,
                  5_000, 220, now))
    conn.execute("INSERT INTO tokens(mint, symbol, dex, mcap_usd, liquidity_usd, pair_created_at, price_change_m5, "
                 "price_change_h1, volume_h1, txns_m5, last_updated) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 ("Futr1111111111111111111111111111111111111111", "FU", "pumpswap", 90_000, 40_000, 0, 20.0, 2.0,
                  5_000, 220, now + 3000))  # stamped ahead of the clock: must not hide the others
    mk.snaps["Futr1111111111111111111111111111111111111111"] = snap(dex="pumpswap",
                                                                  mint="Futr1111111111111111111111111111111111111111")
    t._momentum_at = -1e18
    t.scan()
    rows = conn.execute("SELECT * FROM brain_opps").fetchall()
    assert {r["mint"] for r in rows} == {MINT, "Futr1111111111111111111111111111111111111111"}
    rows = [r for r in rows if r["mint"] == MINT]
    assert rows[0]["kind"] == "market"
    f = json.loads(rows[0]["features"])
    assert f["why"] == "momentum" and f["tx5"] == "100-300" and f["pc1h"] == "20..100%"
    assert rows[0]["decision"] == "skip"  # watched until its replays earn it


def test_convinced_buys_stake_a_kelly_share_of_the_account(s):
    m = trained()
    rng = random.Random(3)
    small = choose(m, "whale", {"tier": "ultra"}, s, rng, equity=1.0)
    big = choose(m, "whale", {"tier": "ultra"}, s, rng, equity=10.0)
    assert small.enter and big.enter and not big.explore
    assert big.size_sol == pytest.approx(s.max_stake * 10.0) and big.size_sol > small.size_sol  # grows with the account
    s.kelly_frac = 0
    assert choose(m, "whale", {"tier": "ultra"}, s, rng, equity=10.0).size_sol <= s.max_size_sol


def test_diary_shows_the_hour_factor(conn, s):
    h0 = int(time.time()) // 3600 * 3600 - 7200
    for ts, eq in ((h0 + 60, 5.0), (h0 + 3500, 10.0), (h0 + 3660, 10.0), (h0 + 7100, 11.0)):
        conn.execute("INSERT OR REPLACE INTO brain_equity VALUES (?,?,?,?)", (ts, eq, 0, eq))
    assert [x for _, x in journal.hour_factors(conn, h0 + 7300)] == [2.0, 1.1]
    assert "×2 erreicht in 1 von 2 Stunden" in journal.write(conn, Brain(len(PLANS)), s, h0 + 7300)


def test_challenge_explores_side_sources_with_account_stakes_and_pauses_after_losses(conn, s):
    mk = Market()
    mk.snaps[MINT] = snap(dex="pumpswap")
    unsure = Brain(len(PLANS))  # knows nothing: exploration, not conviction
    t = Trader(conn, s, unsure, snapshots=mk, executor=Paper(), rng=random.Random(11), safety_check=ok_safety)
    add_shadow(conn, 1)
    t.scan()
    bought = 0
    for i in range(2, 12):
        m = f"Mint{i:040d}"[:44]
        mk.snaps[m] = snap(dex="pumpswap", mint=m)
        add_shadow(conn, i, mint=m)
        bought += t.scan()
    assert bought >= 1
    o = conn.execute("SELECT * FROM brain_opps WHERE decision='enter'").fetchone()
    assert o["kind"] == "market" and o["explore"] == 1
    assert o["size_sol"] == pytest.approx(max(s.min_size_sol, s.challenge_explore_share * 5.0), abs=0.02)
    # three losing hours: the challenge pauses and the side source is only watched again
    h0 = int(time.time()) // 3600 * 3600
    for k, eq in ((3, 5.0), (2, 4.5), (1, 4.0)):
        conn.execute("INSERT OR REPLACE INTO brain_equity VALUES (?,?,?,?)", (h0 - k * 3600 + 10, eq, 0, eq))
        conn.execute("INSERT OR REPLACE INTO brain_equity VALUES (?,?,?,?)", (h0 - k * 3600 + 3500, eq * 0.9, 0, eq * 0.9))
    t._challenge_at = (-1e18, False)
    assert not t.challenge_on(time.time())
    assert t.settings(time.time(), "market").kelly_frac == s.kelly_frac  # paused: even a released pocket stakes normally
    assert "Challenge pausiert" in journal.write(conn, unsure, s)


def test_heartbeat_reads_the_hour_factor_from_the_db(conn, s):
    svc = Service(s, conn, snapshots=Market(), executor=Paper(), safety_check=ok_safety, notify=lambda t: None)
    h0 = int(time.time()) // 3600 * 3600 - 3600
    conn.execute("INSERT OR REPLACE INTO brain_equity VALUES (?,?,?,?)", (h0 + 10, 5.0, 0, 5.0))
    conn.execute("INSERT OR REPLACE INTO brain_equity VALUES (?,?,?,?)", (h0 + 3500, 6.0, 0, 6.0))
    assert "hour factor x1.200" in svc.heartbeat(h0)


def test_organic_exit_keeps_a_late_trail_on_a_doubled_coin(conn, s):
    class Bullish:
        n = 10_000

        def predict(self, toks):
            return [(0.1, 0.05)]
    s.organic_exit = "on"
    set_tier(conn, WHALE, "ultra")
    mk = Market()
    mk.snaps[MINT] = snap()
    clock = [time.time()]
    t = make_trader(conn, s, trained(), mk, clock=lambda: clock[0], exit_model=Bullish())
    t.scan()
    add_whale_buy(conn, "n", ts=clock[0])
    t.scan()
    p = conn.execute("SELECT * FROM brain_positions").fetchone()
    ref = json.loads(p["meta"])["ref_price"]
    mk.snaps[MINT] = snap(price=ref * 2.5)
    clock[0] += 120
    assert t.manage() == 0  # +150 % and the model still bullish: it runs
    mk.snaps[MINT] = snap(price=ref * 1.7)  # 32 % off the high
    clock[0] += 5
    assert t.manage() == 1
    assert conn.execute("SELECT exit_reason FROM brain_positions").fetchone()[0] == "trail"


def test_organic_exit_never_judges_a_fresh_buy_or_a_coin_it_has_not_watched(conn, s):
    """18.10.: the model sold fresh buys after 0-2 s at -6..-8 % (the spread) and, after a restart, ten coins at once."""
    class Bearish:
        n = 10_000

        def predict(self, toks):
            return [(-0.1, 0.05)]
    s.organic_exit = "on"
    set_tier(conn, WHALE, "ultra")
    mk = Market()
    mk.snaps[MINT] = snap()
    clock = [time.time()]
    t = make_trader(conn, s, trained(), mk, clock=lambda: clock[0], exit_model=Bearish())
    t.scan()
    add_whale_buy(conn, "n", ts=clock[0])
    t.scan()
    clock[0] += 1
    assert t.manage() == 0  # 1 s after the buy: no verdict however bearish
    clock[0] += 120
    assert t.manage() == 0  # still inside the grace minutes
    t2 = make_trader(conn, s, trained(), mk, clock=lambda: clock[0], exit_model=Bearish())  # a restart
    clock[0] += 120
    assert t2.manage() == 0  # past the grace, but the new process has not watched the coin yet
    clock[0] += 61
    assert t2.manage() == 1
    assert conn.execute("SELECT exit_reason FROM brain_positions").fetchone()[0] == "organic"


def test_replay_gives_no_organic_verdict_in_the_grace_minutes():
    class Bearish:
        def predict(self, toks):
            return [(-0.1, 0.05)]
    t0 = 1_700_000_000
    cs = [(t0 + 60 * i, 1.0, 1.0, 1.0, 1.0, 10.0) for i in range(30)]
    s = config.Settings()
    gain, mins, why = exits.replay(Bearish(), cs, t0 + 60, "market", None, None, [], [], s, 0.0)
    assert why == "organic" and mins >= exits.GRACE_MIN


def _opps(conn, kind, feats, results, t0):
    for i, r in enumerate(results):
        conn.execute("INSERT INTO brain_opps(ts, kind, ref, features, realized_plan, evaluated_at) VALUES (?,?,?,?,?,?)",
                     (t0 + i, kind, f"{kind}{i}{random.random()}", json.dumps(feats), r, t0 + i))


def test_pockets_release_only_a_pattern_beyond_chance_in_both_halves_and_fall_back(conn):
    from brain import pockets
    now = time.time()
    rng = random.Random(5)
    _opps(conn, "market", {"dex": "pumpswap", "why": "momentum"}, [0.08 + rng.gauss(0, 0.1) for _ in range(120)], now - 5000)
    _opps(conn, "market", {"dex": "raydium", "why": "momentum"}, [0.15 + rng.gauss(0, 0.1) for _ in range(60)]
          + [-0.1 + rng.gauss(0, 0.1) for _ in range(60)], now - 3000)  # paid once, then stopped
    _opps(conn, "whale", {"tier": "ultra"}, [0.3 + rng.gauss(0, 0.1) for _ in range(40)], now - 1000)  # too few
    res = pockets.scan(conn, now)
    assert res["market|dex=pumpswap"]["released"]
    assert not res["market|dex=raydium"]["released"] and "neuere Hälfte" in res["market|dex=raydium"]["reason"]
    assert "whale|tier=ultra" not in res
    b = pockets.Book(conn, lambda: now)
    assert b.match("market", {"dex": "pumpswap", "why": "x"}) == "market|dex=pumpswap"
    assert b.match("market", {"dex": "raydium"}) is None
    assert "market|dex=pumpswap" in store.get(conn, "pockets")["live"]
    assert "1 von" in journal.write(conn, Brain(len(PLANS)), config.Settings(), now)
    tab = pockets.table(conn)
    row = next(r for r in tab["pockets"] if r["key"] == "market|dex=pumpswap")
    assert tab["rule"]["min_n"] == 100 and row["released_at"] and row["label"].startswith("Markt-Coins")


def test_kelly_on_the_lower_bound_outside_released_pockets(s):
    import dataclasses
    m = trained()
    s = dataclasses.replace(s, max_stake=100.0, kelly_frac=0.01)  # no cap: the edge alone decides
    lb = choose(m, "whale", {"tier": "ultra"}, dataclasses.replace(s, kelly_edge="lb"), random.Random(3), equity=10.0)
    mean = choose(m, "whale", {"tier": "ultra"}, dataclasses.replace(s, kelly_edge="mean"), random.Random(3), equity=10.0)
    assert lb.enter and mean.enter and lb.size_sol < mean.size_sol


def test_challenge_stakes_run_only_in_a_released_pocket(conn, s):
    t = make_trader(conn, s, trained(), Market())
    now = time.time()
    assert t.settings(now).kelly_edge == "lb" and t.settings(now).kelly_frac == s.kelly_frac
    big = t.settings(now, "market|dex=pumpswap")
    assert big.kelly_edge == "mean" and big.kelly_frac == s.challenge_kelly_frac and big.max_stake == s.challenge_max_stake
