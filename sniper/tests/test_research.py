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
from sniper.paper import PaperBook  # noqa: E402
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


def test_rug_model_blocks_and_is_stored(conn):
    from safety import store as sstore
    sstore.ensure_schema(conn)
    c = cfg()
    eng = Engine(c, conn, enricher=FakeEnricher())
    eng._rug_model = (9e18, {"stub": True}, lambda model, F: 0.8)     # a fresh "model" that calls every launch a rug
    cv = Curve("RUGGY")
    eng.handle(cv.create(), T0)
    organic(eng, cv, T0 + 1, n=30)
    st = eng.tick(T0 + c.decide_sec + 1)[0]
    assert conn.execute("SELECT rug_p FROM sniper_launches WHERE mint='RUGGY'").fetchone()[0] == 0.8
    enr = {"early": early_from(cv), "funders": {f"buyer{i:03d}": f"f{i}" for i in range(6)},
           "coin": {"twitter": "x"}, "creator_hist": {"n": 1}, "dev_coins": {"n": 1, "n24": 1, "n_complete": 0}}
    _a, reason, pos = eng.finalize(st, enr, now=T0 + 22)
    assert reason == "rug_p" and pos is None and eng.enricher.gates == 0


def test_research_rejects_outlier_rules_and_broken_bases(conn):
    """Regression (Mac, 20:28): rules on untraded launches with a broken base were accepted with test returns of
    +4,500 % and lost about 10 % on every real paper trade."""
    rnd = random.Random(3)
    for i in range(700):
        f = rnd.random()
        broken = f > 0.8
        base = 0.5 if broken and i % 2 else 30.0
        ret = (50.0 if (broken and rnd.random() < 0.15) else -0.12) if broken else rnd.gauss(-0.2, 0.1)
        conn.execute(
            "INSERT INTO sniper_launches(mint, source, pool, created_ts, decided_ts, stage, decision, n_trades, "
            "n_buyers, n_sellers, buy_sol, sell_sol, net_sol, momentum, mcap_decide_sol, ret_1m, ret_5m, ret_15m, "
            "ret_60m, rugged, done) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
            (f"B{i}", "pumpportal", "pump", T0 + i * 60, T0 + i * 60 + 20, 1, "skip", 3, 2, 1, 0.3, 0.1, 0.2,
             f * 100, base, ret, ret, ret, ret, 0))
    conn.commit()
    rep = research.run(conn, cfg(), save=True, now=T0 + 700 * 60)
    assert all(r["mcap_decide_sol"] >= research.MIN_BASE_MCAP for r in research.load(conn))
    assert rep["rules"]["accepted"] is None, rep["rules"]["candidates"][:1]


def test_broken_base_is_not_bought(conn):
    c = cfg()
    eng = Engine(c, conn, enricher=FakeEnricher())
    eng.learned = {"conditions": [["n_buyers", ">", -1]], "accepted": True, "exit": {}}
    cv = Curve("ZERO")
    eng.handle(cv.create(), T0)
    st = eng.launches["ZERO"]
    st.mcap_sol, st.v_tok = 0.0, 0.0
    assert eng.tick(T0 + c.decide_sec + 1) == []
    assert conn.execute("SELECT skip_reason FROM sniper_launches WHERE mint='ZERO'").fetchone()[0] == "no_price"


def test_old_rug_labels_are_fixed_once(conn):
    """Rug labels for rug models: a clean crash to -90 % is a rug, a broken base gets no label."""
    store.set_state(conn, "outcome_fix_ts", "100")
    store.set_state(conn, "rug_label_version", "1")
    rows = [("CRASH", 200, 30.0, -0.95, 0), ("OLDCRASH", 50, 30.0, -0.95, 0), ("BROKEN", 200, 0.5, 0.0, 0),
            ("FINE", 200, 30.0, -0.3, 0)]
    for mint, ts, base, mn, rug in rows:
        store.save_launch(conn, {"mint": mint, "pool": "pump", "decided_ts": ts, "mcap_decide_sol": base,
                                 "min_ret_60m": mn, "rugged": rug, "done": 1})
    assert store.relabel_rugs(conn, research.MIN_BASE_MCAP) == 2
    got = {r["mint"]: r["rugged"] for r in conn.execute("SELECT mint, rugged FROM sniper_launches")}
    assert got == {"CRASH": 1, "OLDCRASH": 0, "BROKEN": None, "FINE": 0}   # before the outcome fix min is unclean
    assert store.relabel_rugs(conn, research.MIN_BASE_MCAP) == 0         # once per label version


def _bal(c):
    return float(c.execute("SELECT value FROM sniper_state WHERE key='paper_balance_sol'").fetchone()[0])


def test_sale_is_credited_once_even_if_killed_between_credit_and_close(monkeypatch, conn):
    """Audit: the balance was committed before status='closed'; a lock or kill in between sold it twice."""
    c = cfg()
    eng = _enter(conn, c)
    path = conn.execute("PRAGMA database_list").fetchone()["file"]
    p = eng.book.open["RESTART"]
    before = _bal(conn)
    st = eng.launches["RESTART"]

    def boom(_conn, _delta):
        raise RuntimeError("killed")
    monkeypatch.setattr(PaperBook, "_credit", staticmethod(boom))
    with pytest.raises(RuntimeError):
        eng.book.sell(conn, st, p, 1.0, "time", T0 + 300)
    conn.rollback()
    monkeypatch.undo()
    other = store.connect(path)                         # what a restarted process sees
    assert _bal(other) == before
    assert other.execute("SELECT status FROM sniper_positions").fetchone()["status"] == "open"
    assert "RESTART" in eng.book.open and p["remaining"] > 0      # memory did not run ahead of the database
    sol = eng.book.sell(conn, st, p, 1.0, "time", T0 + 301)
    assert sol > 0 and abs(_bal(other) - (before + sol)) < 1e-9
    other.close()


def test_dead_curve_position_closes_after_a_restart(conn):
    """Audit: a restarted curve position without trades never closed (no price, max_hold never checked)."""
    c = cfg()
    _enter(conn, c)
    eng2 = Engine(c, conn, enricher=FakeEnricher())
    assert "RESTART" in eng2.due_external(T0 + 200)          # no live price: DexScreener/Jupiter are asked
    eng2.apply_external({}, now=T0 + 200)
    assert "RESTART" in eng2.book.open                        # not yet: no price is no reason to sell
    eng2.apply_external({}, now=T0 + 100 + c.max_hold_min * 60 + 1)
    r = conn.execute("SELECT status, exit_reason, events_json FROM sniper_positions").fetchone()
    assert r["status"] == "closed" and r["exit_reason"] in ("time", "max_hold")
    assert json.loads(r["events_json"])[-1]["src"] == "no_market"     # booked at 0, not at a guessed price


def test_position_without_a_launch_row_is_still_watched(conn):
    c = cfg()
    _enter(conn, c)
    conn.execute("DELETE FROM sniper_launches")
    conn.commit()
    eng2 = Engine(c, conn, enricher=FakeEnricher())
    assert "RESTART" in eng2.due_external(T0 + 200)


def test_migrated_coin_with_a_frozen_price_exits_as_no_market(conn):
    """Audit: after migration a vanished pair kept its last price; the stop never fired, time exit sold at 95 %."""
    c = cfg()
    eng = _enter(conn, c)
    st = eng.launches["RESTART"]
    st.migrated = True
    p = eng.book.open["RESTART"]
    p["exit"] = {"time_stop_min": 999, "max_hold_min": 999}
    st.external_mcap_sol, st.external_ts = st.mcap_sol, T0 + 200
    assert eng.book.check(conn, st, T0 + 210, None, quoted=True) is None     # fresh price, no exit rule hit
    assert eng.book.check(conn, st, T0 + 210 + PaperBook.STALE_SEC + 1, None, quoted=True) == "no_market"
    assert p["status"] == "closed" and p["realized_sol"] == 0.0


def test_replay_stop_through_a_gap_fills_at_the_crash_not_at_the_stop():
    # a rug on the curve: the bucket gaps from 100 to 9 and closes at 10; live PaperBook sells after the crash
    net, why = research.replay([[20, 100, 100, 100], [25, 10, 9, 100]], 20, 100, RULE, 0.0, 0.0)
    assert why == "stop" and net < -0.85
    # a wick through the stop that closes above it still fills at 5 % under the stop
    net, _ = research.replay([[20, 100, 100, 100], [25, 90, 60, 100]], 20, 100, RULE, 0.0, 0.0)
    assert abs(net - (0.7 * 0.95 - 1)) < 1e-9


def test_dev_history_counts_each_bad_launch_once_and_not_the_current_one(conn):
    store.save_launch(conn, {"mint": "OLD", "creator": "DEV", "created_ts": T0 - 7200, "rugged": 1, "dev_sold_pct": 95})
    store.save_launch(conn, {"mint": "NEW", "creator": "DEV", "created_ts": T0})
    h = store.creator_history(conn, "DEV", T0, exclude_mint="NEW")
    assert h["n"] == 1 and h["bad"] == 1


def test_market_gate_cannot_waive_the_launch_data_duty(monkeypatch, conn):
    """Review: a too-low score skipped the 'unverified' check, then a hot market lowered the bar: bought unchecked."""
    from sniper import engine as engine_mod

    class HotMarket:
        @staticmethod
        def sniper_adjust(*_a):
            return {"delta": -99, "why": "heißes Narrativ"}
    monkeypatch.setattr(engine_mod, "_market", HotMarket)
    c = cfg(min_score=99)
    eng = Engine(c, conn, enricher=FakeEnricher())
    cv = Curve("HOTNODATA")
    eng.handle(cv.create(), T0)
    organic(eng, cv, T0 + 1, n=30)
    st = eng.tick(T0 + c.decide_sec + 1)[0]
    _, reason, pos = eng.finalize(st, {"errors": ["pump.fun down"]}, now=T0 + 22)
    assert reason == "unverified" and pos is None


def test_stale_dexscreener_price_is_no_outcome(conn):
    """Review: the last quote of a pair that stopped updating was written as the final 60 min result."""
    c = cfg()
    eng = Engine(c, conn, enricher=FakeEnricher())
    cv = Curve("STALE")
    eng.handle(cv.create(), T0)
    organic(eng, cv, T0 + 1, n=30)
    st = eng.tick(T0 + c.decide_sec + 1)[0]
    eng._unsub(st)
    d = st.decided_ts
    eng.apply_external({"STALE": st.base_mcap_sol * 2}, now=d + 50)
    eng.tick(d + 61)                                   # 1 min: fresh quote -> recorded
    eng.tick(d + 3601)                                 # 60 min: the quote is an hour old -> nothing
    r = conn.execute("SELECT ret_1m, ret_60m, done FROM sniper_launches WHERE mint='STALE'").fetchone()
    assert r["ret_1m"] is not None and r["ret_60m"] is None and r["done"] == 0
    eng.tick(d + 75 * 60 + 1)                          # no fresh price ever: finished without a label
    r = conn.execute("SELECT ret_60m, done, rugged, outcome_src FROM sniper_launches WHERE mint='STALE'").fetchone()
    assert r["done"] == 1 and r["ret_60m"] is None and r["rugged"] is None and r["outcome_src"] == "no_fresh_price"
    assert "STALE" not in eng.launches


def test_research_reads_the_stage1_features_the_rule_is_matched_on(conn):
    """Review: finalize overwrote the stage-1 columns, research trained on later values than the live match sees."""
    c = cfg()
    _enter(conn, c)
    s1 = json.loads(conn.execute("SELECT stage1_json FROM sniper_launches WHERE mint='RESTART'").fetchone()[0])
    conn.execute("UPDATE sniper_launches SET n_trades = 999, ret_60m = 0.1 WHERE mint='RESTART'")
    conn.execute("INSERT INTO sniper_launches(mint, source, pool, stage, decided_ts, created_ts, mcap_decide_sol, "
                 "n_trades, ret_60m) VALUES ('OLD2', 'pumpportal', 'pump', 2, 1, 1, 30, 5, 0.1)")
    conn.commit()
    rows = {r["mint"]: r for r in research.load(conn)}
    assert rows["RESTART"]["n_trades"] == s1["n_trades"] != 999
    assert "OLD2" not in rows                         # deep-checked before the snapshot existed: not comparable


def test_silent_trade_stream_counts_as_dead():
    from sniper import rpcws
    s = rpcws.LogStream("wss://x", lambda m: None, 150, stale_sec=180, mode="program", program=rpcws.PUMP_PROGRAM)
    assert s.silence_limit() is None                   # nothing subscribed yet
    s.subs = {rpcws.PUMP_PROGRAM: 1}
    assert s.silence_limit() == 60                     # the whole program is never quiet for a minute
    m = rpcws.LogStream("wss://x", lambda m: None, 150, stale_sec=180, mode="mints")
    m.subs = {f"m{i}": i for i in range(3)}
    assert m.silence_limit() is None                   # three quiet coins can be silent
    m.subs = {f"m{i}": i for i in range(12)}
    assert m.silence_limit() == 180
