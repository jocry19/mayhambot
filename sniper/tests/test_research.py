"""Outcome correctness, restart safety, label fixes and the research module. Fixtures only, no network."""
import json
import os
import random
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pytest  # noqa: E402

from sniper import feed, research, store  # noqa: E402
from sniper.engine import Engine  # noqa: E402
from sniper.tests.test_sniper import T0, Curve, FakeEnricher, cfg, early_from, organic  # noqa: E402


@pytest.fixture
def conn():
    d = tempfile.mkdtemp()
    c = store.connect(os.path.join(d, "t.db"))
    yield c
    c.close()


def test_outcome_extremes_start_at_the_decision_and_path_is_stored(conn):
    """Regression: max_ret_60m counted the pump before the decision (ret_1m -6 %, max_ret_60m +148 %)."""
    c = cfg()
    eng = Engine(c, conn, enricher=FakeEnricher())
    cv = Curve("PUMPDUMP")
    eng.handle(cv.create(), T0)
    for i in range(12):                       # pump before the decision
        eng.handle(cv.buy(f"p{i}", 3.0), T0 + 1 + i)
    for i in range(12):                       # and dump, still before it
        eng.handle(cv.sell(f"p{i}", 20_000_000), T0 + 14 + i * 0.3)
    eng.tick(T0 + c.decide_sec + 1)
    st = eng.launches["PUMPDUMP"]
    assert st.decided_ts and st.max_mcap_sol == st.base_mcap_sol < st.peak_mcap_sol
    for m in (1, 5, 15, 60):
        eng.tick(T0 + c.decide_sec + 1 + m * 60 + 1)
    r = conn.execute("SELECT * FROM sniper_launches WHERE mint='PUMPDUMP'").fetchone()
    assert r["done"] == 1 and abs(r["max_ret_60m"]) < 0.01
    path = store.load_paths(conn)["PUMPDUMP"]
    assert path[0][0] == 0 and max(p[3] for p in path) > path[0][1] * 1.5   # the pre-decision pump is in the path


def _enter(conn, c):
    eng = Engine(c, conn, enricher=FakeEnricher())
    cv = Curve("RESTART")
    eng.handle(cv.create(), T0)
    organic(eng, cv, T0 + 1, n=30)
    st = eng.tick(T0 + c.decide_sec + 1)[0]
    enr = {"early": early_from(cv), "funders": {f"buyer{i:03d}": f"f{i}" for i in range(6)},
           "coin": {"twitter": "x"}, "creator_hist": {"n": 1}, "dev_coins": {"n": 1, "n24": 1, "n_complete": 0}}
    _a, reason, pos = eng.finalize(st, enr, now=T0 + 22)
    assert pos, reason
    for i in range(40):                        # a pump the stored high remembers (above tp1? no: below)
        eng.handle(cv.buy(f"x{i}", 0.3), T0 + 30 + i)
    eng.tick(T0 + 100 + 60)                    # 1 min outcome -> mcap_max stored
    return eng


def test_restart_does_not_sell_at_the_stored_high(conn):
    """Regression: after a restart the stub's current price was the stored high: fake tp1 on the first tick."""
    c = cfg()
    _enter(conn, c)
    conn.execute("UPDATE sniper_launches SET mcap_max_sol = mcap_decide_sol * 2.1, max_ret_60m = 1.1")
    conn.commit()
    eng2 = Engine(c, conn, enricher=FakeEnricher())
    st = eng2.launches["RESTART"]
    assert st.mcap_sol == 0.0 and st.current_mcap_sol() == 0.0
    assert eng2.book.check(conn, st, T0 + 200) is None
    eng2.tick(T0 + 200)
    p = conn.execute("SELECT status, tp1_done FROM sniper_positions").fetchone()
    assert p["status"] == "open" and p["tp1_done"] == 0


def test_safety_gate_blocks_when_the_check_fails(monkeypatch, conn):
    import safety
    from sniper.enrich import Budget, Enricher

    def boom(*a, **k):
        raise RuntimeError("rpc down")
    monkeypatch.setattr(safety, "assess", boom)
    e = Enricher(cfg(), Budget({"rpc": 10, "api": 10, "deep": 4}))
    res = e.safety_gate(conn, "MINT")
    assert res["blocked"] is True and res["level"] == "unchecked"


def test_feed_fdv_and_no_stablecoin_launches(monkeypatch):
    from core.models import STABLE_MINTS
    usdt = sorted(STABLE_MINTS)[0]
    pairs = [{"baseToken": {"address": "M1"}, "quoteToken": {"address": feed.SOL_MINT}, "priceNative": "0.0000001",
              "marketCap": 1000.0, "fdv": 200_000.0, "liquidity": {"usd": 5000}}]
    monkeypatch.setattr(feed.http, "get_json", lambda *a, **k: pairs)
    assert feed.dex_prices(["M1"], 200.0) == {"M1": 1000.0}          # fdv / sol_usd, not the circulating cap
    pools = {"data": [
        {"attributes": {"name": "USDT / SOL", "fdv_usd": "1e9", "pool_created_at": "2026-10-16T10:00:00Z"},
         "relationships": {"dex": {"data": {"id": "raydium"}}, "base_token": {"data": {"id": f"solana_{usdt}"}}}},
        {"attributes": {"name": "NEW / SOL", "fdv_usd": "50000", "market_cap_usd": "100",
                        "pool_created_at": "2026-10-16T10:00:00Z"},
         "relationships": {"dex": {"data": {"id": "raydium"}}, "base_token": {"data": {"id": "solana_NEWMINT"}}}}]}
    monkeypatch.setattr(feed.http, "get_json", lambda *a, **k: pools)
    out = feed.new_pools(200.0)
    assert [p["mint"] for p in out] == ["NEWMINT"] and out[0]["mcap_sol"] == 250.0


def test_create_slot_unknown_with_a_full_page_is_not_guessed():
    from sniper.enrich import Enricher, Budget
    from sniper.state import LaunchState

    class F:
        def rpc(self, method, params):
            return [{"signature": f"s{i}", "slot": 5000 + i, "err": None} for i in range(1000)]
    st = LaunchState(mint="M", created_ts=T0)
    st.create_sig = "not-in-page"
    enr = {"errors": []}
    Enricher(cfg(), Budget({"rpc": 10, "api": 10, "deep": 4}))._rpc_early(F(), st, enr)
    assert "create_slot" not in enr and "create_slot_unknown" in enr["errors"]


# ------------------------------------------------------------------ research
RULE = (60, 0.5, 25, 30, 12, 60)


def test_replay_knows_which_came_first():
    side, tx = 0.0, 0.0
    stop_first = [[20, 100, 100, 100], [25, 65, 65, 66], [30, 200, 180, 200]]
    tp_first = [[20, 100, 100, 100], [25, 170, 165, 170], [30, 60, 60, 60]]
    r1, why1 = research.replay(stop_first, 20, 100, RULE, side, tx)
    r2, why2 = research.replay(tp_first, 20, 100, RULE, side, tx)
    assert why1 == "stop" and r1 < -0.3
    assert why2 == "trail" and r2 > 0     # half sold at +60 %, rest trailed out
    flat = [[20, 100, 100, 100], [20 + 13 * 60, 101, 99, 101]]
    assert research.replay(flat, 20, 100, RULE, side, tx)[1] == "time"
    net, _ = research.replay(flat, 20, 100, RULE, 0.05, 0.01)
    assert -0.13 < net < -0.10            # costs: two sides of 5 % plus two transactions of 1 %


def _launch_rows(conn, n, edge, seed=1):
    rnd = random.Random(seed)
    for i in range(n):
        f = rnd.random()
        good = edge and f > 0.7
        ret = rnd.gauss(0.6 if good else -0.2, 0.15)
        base = 30.0
        conn.execute(
            "INSERT INTO sniper_launches(mint, source, pool, created_ts, decided_ts, stage, decision, n_trades, "
            "n_buyers, n_sellers, buy_sol, sell_sol, net_sol, momentum, top10_pct, unseen_pct, dev_buy_pct, "
            "curve_progress, mcap_decide_sol, ret_1m, ret_5m, ret_15m, ret_60m, max_ret_60m, min_ret_60m, rugged, done) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
            (f"M{i}", "pumpportal", "pump", T0 + i * 60, T0 + i * 60 + 20, 1, "skip", 10, 5, 2, 2.0, 0.5, 1.5,
             f * 100, 20.0, 5.0, 3.0, 0.1, base, ret / 2, ret, ret, ret, max(ret, 0), min(ret, 0), 0))
    conn.commit()


def test_research_accepts_a_real_edge_on_unseen_data(conn):
    _launch_rows(conn, 600, edge=True)
    rep = research.run(conn, cfg(), save=True, now=T0 + 600 * 60)
    acc = rep["rules"]["accepted"]
    assert acc and any(c[0] == "momentum" and c[1] == ">" and c[2] >= 55 for c in acc["conditions"])
    assert acc["test"]["ci_low"] > 0
    rule = research.learned_rule(conn, now=T0 + 600 * 60)
    assert rule and research.matches(rule, {"momentum": 90}) and not research.matches(rule, {"momentum": 10})
    assert "ANGENOMMEN" in research.summary(rep)


def test_research_rejects_noise(conn):
    _launch_rows(conn, 600, edge=False)
    rep = research.run(conn, cfg(), save=True, now=T0 + 600 * 60)
    assert rep["rules"]["accepted"] is None and research.learned_rule(conn) is None
    assert rep["features"] and rep["exits"] and "Keine Regel" in research.summary(rep)


def test_learned_rule_buys_in_paper_with_its_own_exits(conn):
    c = cfg()
    eng = Engine(c, conn, enricher=FakeEnricher())
    eng.learned = {"conditions": [["n_buyers", ">", 1]], "accepted": True,
                   "exit": {"tp1_pct": 20, "tp1_frac": 1.0, "trail_pct": 15, "stop_pct": 15, "time_stop_min": 3,
                            "max_hold_min": 60}}
    cv = Curve("LEARN")
    eng.handle(cv.create(), T0)
    organic(eng, cv, T0 + 1, n=3, sol=0.1)          # far too little for the momentum filter
    cands = eng.tick(T0 + c.decide_sec + 1)
    assert [s.mint for s in cands] == ["LEARN"] and cands[0].learned
    enr = {"early": early_from(cv), "funders": {}, "coin": {"twitter": "x"}, "creator_hist": {"n": 1},
           "dev_coins": {"n": 1, "n24": 1, "n_complete": 0}}
    _a, reason, pos = eng.finalize(cands[0], enr, now=T0 + 22)
    assert pos and reason is None, reason
    assert json.loads(conn.execute("SELECT params_json FROM sniper_positions").fetchone()[0])["tp1_pct"] == 20
    tt = T0 + 30
    while conn.execute("SELECT status FROM sniper_positions").fetchone()[0] == "open":
        eng.handle(cv.buy(f"late{tt}", 0.5), tt)
        tt += 1
        assert tt < T0 + 400
    assert conn.execute("SELECT exit_reason FROM sniper_positions").fetchone()[0] == "tp1"
