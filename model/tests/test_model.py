"""Model/research tests on a synthetic market with known truth (no network).
Run: python -m pytest model/tests -q"""
import os
import time
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pytest  # noqa: E402

from core import db  # noqa: E402
from core.models import Trade  # noqa: E402
from model import score_token, tier_of, token_signal_history, train, update_outcomes, wallet_tiers  # noqa: E402
from research import data, fixtures, stats, wallet_stats  # noqa: E402


@pytest.fixture(scope="module")
def market():
    conn, truth, now = fixtures.build(os.path.join(tempfile.mkdtemp(), "m.db"), days=4, n_tokens=100)
    vid = train.run_once(conn, now=now, write_files=False)
    return conn, truth, now, vid


def test_tiers_find_the_skilled_wallets(market):
    conn, truth, now, vid = market
    ultra = wallet_tiers(conn, tier="ultra")
    assert ultra, "no ultra wallets found"
    top = ultra[:15]
    skilled = sum(1 for w in top if truth.get(w["address"]) in ("ultra", "smart"))
    assert skilled / len(top) >= 0.75
    tiered = {w["address"]: w["tier"] for w in wallet_tiers(conn)}
    true_ultra = [w for w, t in truth.items() if t == "ultra"]
    assert sum(1 for w in true_ultra if tiered.get(w) in ("ultra", "smart")) >= len(true_ultra) - 1
    assert all(not w["address"].startswith("BOT") for w in wallet_tiers(conn))
    t = tier_of(ultra[0]["address"], conn)
    assert t["tier"] == "ultra" and set(t["components"]) >= {"win", "lead", "influence"}


def test_backtest_beats_chance(market):
    conn, *_ = market
    import json
    rep = json.loads(conn.execute("SELECT json FROM research_reports ORDER BY id DESC").fetchone()[0])
    bt = rep["backtest"]["1h"]
    assert bt["n"] > 50 and bt["auc"] > 0.75
    tf = rep["tier_forward"]
    assert tf["ultra"]["pump_rate"] > tf["none"]["pump_rate"]


def test_score_token_explains_itself(market):
    conn, truth, now, vid = market
    mint = conn.execute("SELECT mint FROM trades WHERE ts < ? ORDER BY ts DESC LIMIT 1", (now - 7200,)).fetchone()[0]
    ts = conn.execute("SELECT MAX(ts) FROM trades WHERE mint=?", (mint,)).fetchone()[0]
    r = score_token(mint, now=ts, conn=conn, store=True)
    assert r and r["version"] == vid and 0 <= r["score"] <= 100
    c = r["contributions"]["1h"]
    assert abs(sum(x["share"] for x in c) - 1) < 0.01
    assert set(r["horizons"]) == {"15m", "1h", "6h"}
    assert token_signal_history(mint, conn)[-1]["score"] == r["score"]
    from model.predict import prepare
    ctx = prepare(conn)
    r2 = score_token(mint, now=ts, conn=conn, tiers=ctx["tiers"], shared=ctx["shared"])
    assert r2["score"] == r["score"]  # per-cycle context gives the same answer
    assert update_outcomes(conn, now=now) >= 1


def test_wallet_stats_ignore_the_future():
    conn = db.connect(os.path.join(tempfile.mkdtemp(), "f.db"))
    from research.store import ensure_schema
    ensure_schema(conn)
    tr = [Trade("a", "W", "M", "buy", 1000, 1000, 1, 100, 0.1, "test_pool"),
          Trade("b", "X", "M", "buy", 5000, 100, 1, 100, 1.0, "test_pool")]
    db.insert_trades(conn, tr)
    ds = data.load(conn)
    st, _ = wallet_stats.compute(ds, 2000, min_position_usd=1)
    assert st["W"]["early_rate"] == 0  # the 10x after t=2000 must not be visible at cutoff 2000
    st2, _ = wallet_stats.compute(ds, 6000, min_position_usd=1)
    assert st2["W"]["early_rate"] == 1


def test_stats_helpers():
    assert stats.auc([1, 2, 3, 4], [0, 0, 1, 1]) == 1.0
    r, lo, hi = stats.wilson(5, 10)
    assert lo < 0.5 < hi
    m = stats.logistic_fit([[0], [1], [2], [3], [4], [5]], [0, 0, 0, 1, 1, 1], l2=0.1)
    assert stats.logistic_predict(m, [5]) > 0.8 > 0.2 > stats.logistic_predict(m, [0])


def test_funding_detection_and_clusters():
    from research import funding
    tx = {"blockTime": 50, "meta": {"err": None, "innerInstructions": []},
          "transaction": {"message": {"instructions": [
              {"program": "system", "parsed": {"type": "transfer", "info": {
                  "source": "F", "destination": "A", "lamports": 2_000_000_000}}}]}}}
    assert funding.funder_in_tx(tx, "A") == ("F", 2_000_000_000)
    assert funding.funder_in_tx(tx, "B") is None
    ds = data.Dataset([], None, {}, set(), set(), 150.0)
    ds.funding = {"A": ("F", 50), "B": ("F", 60), "C": ("G", 70), "D": ("F", 9999)}
    cl = wallet_stats.clusters(ds, ["A", "B", "C", "D"], cutoff=1000)
    assert cl["A"] == cl["B"] != cl["C"]
    assert cl["D"] == "D"  # funded after the cutoff: not visible yet
    ds.funding.update({f"H{i}": ("HUB", 1) for i in range(20)})
    ds.funding.update({"A": ("HUB", 1), "B": ("HUB", 1)})
    cl = wallet_stats.clusters(ds, ["A", "B"], cutoff=1000)
    assert cl["A"] != cl["B"]  # exchanges fund everyone: no link


def test_retrain_if_due_keeps_last_success_on_failure(monkeypatch):
    from model import train
    conn = db.connect(os.path.join(tempfile.mkdtemp(), "r.db"))
    from research.store import ensure_schema
    ensure_schema(conn)
    assert train.last_success(conn) == 0.0
    calls = []

    def boom(c):
        calls.append(1)
        raise RuntimeError("database is locked?")
    monkeypatch.setattr(train, "run_once", boom)
    assert train.retrain_if_due(conn, 3600, 0.0) == 0.0  # failed: still due next cycle
    assert calls
    monkeypatch.setattr(train, "run_once", lambda c: None)
    assert train.retrain_if_due(conn, 3600, time.time() - 60) > 0  # not due: untouched, no call


def test_selection_log_explains_rejections():
    from model.train import selection_log
    sel = {"6h": {"status": "prior", "n": 0, "pos": 0, "need_n": 80, "need_pos": 12},
           "1h": {"status": "prior", "n": 42, "pos": 9, "need_n": 80, "need_pos": 12},
           "15m": {"status": "selected", "chosen": {"algo": "logistic", "drop": None}, "why": "bester Kandidat", "n": 98,
                   "n_train": 60, "n_test": 30, "base_logloss": 0.69, "holdout": {"logloss": 0.62},
                   "candidates": [{"cfg": {"algo": "logistic", "drop": None}, "logloss": 0.62, "auc": 0.6},
                                  {"cfg": {"algo": "gbm", "drop": None}, "logloss": 0.71, "auc": 0.55},
                                  {"cfg": {"algo": "gbm", "drop": "tiers"}, "error": "boom"}]}}
    text = "\n".join(selection_log(sel))
    assert "6h: prior (n 0" in text and "no outcomes" in text
    assert "need >= 80" in text
    assert "gbm rejected: lost in holdout, logloss 0.7100 vs champion 0.6200" in text
    assert "gbm-tiers rejected: not trainable" in text


def test_tier_sync_none_before_first_model():
    from model import tier_sync
    conn = db.connect(os.path.join(tempfile.mkdtemp(), "s.db"))
    assert tier_sync(conn) is None


def test_research_credits_and_cheap_rpc(monkeypatch):
    from core import http
    from research import credits, rpc
    from research.config import rsettings
    monkeypatch.setattr(http, "credits_left_today", lambda service=None: 150.0)
    monkeypatch.setattr(credits, "used_today", lambda: 0)
    monkeypatch.setattr(rsettings, "helius_credits_per_day", 0)
    assert credits.can("enhanced") and not credits.can("rpc", 151)
    monkeypatch.setattr(rsettings, "helius_credits_per_day", 50)  # optional own cap below core's budget
    assert not credits.can("enhanced") and credits.can("rpc", 50)
    monkeypatch.setattr(rsettings, "helius_credits_per_day", 0)
    seen = []

    def post(url, method, params):
        seen.append(url)
        if "solana.com" in url:
            raise RuntimeError("429")
        return "ok"
    monkeypatch.setattr(rpc, "_post", post)
    monkeypatch.setattr(rpc, "_helius_rpc_url", lambda: "https://mainnet.helius-rpc.com/?api-key=x")
    monkeypatch.setattr(rsettings, "rpc_url", "https://api.mainnet-beta.solana.com")
    assert rpc.call("getSlot", []) == "ok" and len(seen) == 2  # public first, then 1-credit Helius

    def exhausted(w, limit):
        raise http.CreditsExhausted("used up")
    from core.sources import wallet_history
    monkeypatch.setattr(wallet_history, "_helius_wallet_trades", exhausted)
    monkeypatch.setattr(rpc.settings, "helius_api_key", "x", raising=False)
    monkeypatch.setattr(rpc, "call", lambda m, p: [])
    assert rpc.wallet_trades("W", enhanced=True) == []  # no traceback, falls back to RPC


def test_labels_after_costs_and_dead_coin_coverage():
    from research import events as ev
    from research.prices import PriceIndex
    idx = PriceIndex()
    idx._add("UP", [(0, 1.0), (600, 1.6), (3000, 1.1)])        # touched +60 % then faded
    idx._add("DEAD", [(0, 1.0), (300, 0.9)])                    # stopped trading after 5 min
    hz = [("1h", 3600, 0.5)]
    up = ev.labels_at("UP", 0, idx, hz)["1h"]
    assert up is None  # prices only reach t=3000, the hour is not covered yet
    idx.fetched["UP"] = 4000
    up = ev.labels_at("UP", 0, idx, hz)["1h"]
    assert up["pumped"] == 1 and abs(up["net"] - (0.5 - 0.06)) < 1e-9 and up["win"] == 1
    idx.fetched["DEAD"] = 5000  # candles fetched after the hour: no candle = no trade, last price holds
    dead = ev.labels_at("DEAD", 0, idx, hz)["1h"]
    assert dead["pumped"] == 0 and abs(dead["net"] - (-0.1 - 0.06)) < 1e-9 and dead["win"] == 0


def test_backtest_calibration_bins():
    from research.backtest import calibration
    c = calibration([0.1, 0.1, 0.9, 0.9], [0, 0, 1, 1], bins=2)
    assert c["ece"] < 0.11 and [b["rate"] for b in c["bins"]] == [0.0, 1.0]


def test_uncopyable_wallets_get_no_tier():
    from model.tiers import assign_tier, uncopyable
    good = dict(n_tokens=8, win_rate=0.7, rug_rate=0.0, total_pnl_usd=5000, net_pnl_usd=4000, launch_share=0.0,
                median_entry_rank=0.2, influence_samples=5, n_exits=6, median_hold_min=12.0)
    assert uncopyable(good) is None and assign_tier(good, 90) == "ultra"
    assert uncopyable(dict(good, launch_share=0.8)) == "launch_bot" and assign_tier(dict(good, launch_share=0.8), 90) is None
    assert uncopyable(dict(good, median_entry_rank=0)) == "launch_bot"
    assert uncopyable(dict(good, median_hold_min=0.1)) == "flipper"
    assert assign_tier(dict(good, net_pnl_usd=-10), 90) is None  # profitable before costs only


def test_ensure_schema_keeps_callers_busy_timeout():
    from research.store import ensure_schema
    conn = db.connect(os.path.join(tempfile.mkdtemp(), "b.db"))
    conn.execute("PRAGMA busy_timeout=5000")
    ensure_schema(conn)
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_realized_winner_mining():
    from research import mining
    from research.store import ensure_schema
    conn = ensure_schema(db.connect(os.path.join(tempfile.mkdtemp(), "m.db")))
    now = 1_800_000_000
    tr = [Trade("a1", "WIN", "M1", "buy", now - 3600, 1000, 1.0, 150, 0.15, "rpc"),
          Trade("a2", "WIN", "M1", "sell", now - 1800, 1000, 4.0, 600, 0.6, "rpc"),
          Trade("b1", "LOSE", "M1", "buy", now - 3600, 1000, 1.0, 150, 0.15, "rpc"),
          Trade("b2", "LOSE", "M1", "sell", now - 1800, 1000, 1.5, 225, 0.225, "rpc"),
          Trade("c1", "OLD", "M1", "buy", now - 30 * 86400, 1000, 1.0, 150, 0.15, "rpc"),
          Trade("c2", "OLD", "M1", "sell", now - 29 * 86400, 1000, 9.0, 1350, 1.35, "rpc")]
    db.insert_trades(conn, tr)
    w = mining.realized_winners(conn, now=now)
    assert set(w) == {"WIN"}
    assert mining.run(conn, now=now) == 1
    assert conn.execute("SELECT discovered_via FROM research_wallets WHERE address='WIN'").fetchone()[0] == "realized_winner"


def test_selection_refuses_models_worse_than_base_rate_and_survives_purge():
    import random
    from model import learn
    from research.events import FEATURE_NAMES
    rng = random.Random(3)
    n = len(FEATURE_NAMES)
    # pure noise: no candidate may beat the base rate on the holdout
    ev = [dict(ts=i * 60, x=[rng.random() for _ in range(n)],
               labels={"1h": {"pumped": int(rng.random() < 0.3), "max_ret": 0.1, "net": -0.1, "win": 0}})
          for i in range(300)]
    models, rep = learn.select_models(ev, horizons=[("1h", 3600, 0.5)])
    assert rep["1h"]["status"] == "base" and models["1h"]["cfg"]["algo"] == "base"
    p, _ = learn.predict_one(models["1h"], ev[0]["x"])
    assert abs(p - sum(e["labels"]["1h"]["pumped"] for e in ev) / len(ev)) < 1e-6
    # events bunched within 9 h with a 6 h horizon: the 70 % cut leaves too little purged training data
    ev6 = [dict(ts=i * 77, x=[float(i % 2)] + [0.0] * (n - 1),
                labels={"6h": {"pumped": i % 2, "max_ret": 1.0, "net": 0.5, "win": i % 2}}) for i in range(420)]
    _, rep6 = learn.select_models(ev6, horizons=[("6h", 21600, 1.0)])
    assert rep6["6h"]["n_train"] >= 40


def test_exit_replay_stop_before_target_and_costs():
    from research import exits
    from research.prices import PriceIndex
    idx = PriceIndex()
    idx._add("M", [(0, 1.0), (60, 0.7), (120, 1.6), (4000, 1.0)])
    r = dict(tp=0.5, sl=-0.2, hold="1h", hold_s=3600)
    assert abs(exits.replay(idx, "M", 0, r) - (-0.2 - 0.06)) < 1e-9  # stop hit first
    r2 = dict(tp=0.5, sl=None, hold="1h", hold_s=3600)
    assert abs(exits.replay(idx, "M", 0, r2) - (0.5 - 0.06)) < 1e-9
    assert exits.replay(idx, "M", 0, dict(r2, hold_s=21600)) is None  # 6 h not covered yet


def test_prediction_blobs_are_small_and_models_carry_a_feature_stamp():
    import json as _json
    from model import predict
    from research import events as ev
    f = {n: 0.123456789 for n in ev.FEATURE_NAMES}
    con = {h: [{"feature": n, "label": "x" * 30, "value": 1.0, "weight": 0.5, "contribution": 0.1, "share": 0.02}
               for n in ev.FEATURE_NAMES] for h in ("15m", "1h", "6h")}
    w = [{"address": "A" * 44, "tier": "good", "score": 50, "net_sol": i, "first_ts": 1, "extra": "y" * 100}
         for i in range(200)]
    fj, cj, wj = predict._compact(f, con, w)
    assert len(fj) + len(cj) + len(wj) < 6000
    assert len(_json.loads(cj)["1h"]) == 10 and len(_json.loads(wj)) == 15
    assert len(ev.FEATURE_SET) == 10


def test_wash_wallets_and_repeat_early_and_tier_paper():
    from model.tiers import uncopyable
    base = dict(n_tokens=8, n_exits=6, median_hold_min=3.0, launch_share=0.0, median_entry_rank=0.3, influence_samples=4)
    assert uncopyable(dict(base, wash_share=0.7)) == "wash" and uncopyable(dict(base, wash_share=0.1)) is None
    from research import mining, report
    from research.store import ensure_schema, store_candles, upsert_research_token
    conn = ensure_schema(db.connect(os.path.join(tempfile.mkdtemp(), "e.db")))
    now = 1_800_000_000
    tr = []
    for i, m in enumerate(("P1", "P2")):
        upsert_research_token(conn, m, peak_multiple=10.0)
        store_candles(conn, m, 60, [(now - 3600, 1.0, 10.0, 1.0, 5.0, 100.0)])
        tr.append(Trade(f"e{i}", "EARLY", m, "buy", now - 7200, 100, 1, 100, 1.0, "rpc"))
        tr.append(Trade(f"l{i}", "LATE", m, "buy", now - 3000, 10, 1, 90, 9.0, "rpc"))
    db.insert_trades(conn, tr)
    assert set(mining.repeat_early(conn, now=now)) == {"EARLY"}
    assert report.tiers_vs_paper(conn) == {}  # no copytrade tables: empty, no error


def test_dead_coin_holdings_count_as_zero():
    conn = db.connect(os.path.join(tempfile.mkdtemp(), "d.db"))
    from research.store import ensure_schema
    ensure_schema(conn)
    db.insert_trades(conn, [Trade("a", "W", "M", "buy", 1000, 1000, 1, 100, 0.1, "test_pool"),
                            Trade("b", "X", "M", "buy", 2000, 1000, 1, 100, 0.1, "test_pool")])
    ds = data.load(conn)
    assert wallet_stats.compute(ds, 3000, min_position_usd=1)[0]["W"]["total_pnl_usd"] == 0
    assert wallet_stats.compute(ds, 2000 + 90000, min_position_usd=1)[0]["W"]["total_pnl_usd"] == -100  # stale 25 h


def test_seed_file_user_lines_get_top_priority(tmp_path, monkeypatch):
    from research import seeds, store
    conn = store.connect(str(tmp_path / "s.db"))
    f = tmp_path / "seed.txt"
    a, b = "A" * 43, "B" * 43
    f.write_text(f"# comment\n{a} # user 2026-10-16\n{b} # kolscan\n")
    monkeypatch.setattr(seeds, "SEED_FILES", [f])
    assert seeds.from_files(conn) == 2
    pr = dict(conn.execute("SELECT address, priority FROM research_wallets"))
    assert pr[a] == 100.0 and pr[b] == 80.0


def test_exit_study_keeps_out_of_sample_check_when_long_holds_are_not_complete(monkeypatch):
    from research import exits
    evs = [dict(mint=f"M{i}", ts=i, p=1.0) for i in range(100)]

    def fake(pidx, mint, t, r):
        if r["hold"] == "6h":
            return None if t >= 60 else 0.5  # best on the old part, but recent 6 h outcomes are open
        return 0.1 * r["tp"]

    monkeypatch.setattr(exits, "replay", fake)
    o = exits.study(evs, None, top_share=1.0)["out_of_sample"]
    assert o and o["rule"]["hold"] != "6h" and o["n"] >= 10


def test_candle_high_before_the_entry_does_not_count_as_reached_after_it():
    from research.prices import PriceIndex
    idx = PriceIndex()
    # 5-min candle 0..299 spiked to 2.0 before an entry at t=100; the next candle only reaches 1.2
    idx._add("M", [(50, 1.0), (299, 1.0), (599, 1.1)], candles=[(300, 0, 2.0, 0.9), (300, 300, 1.2, 1.0)])
    assert idx.max_between("M", 100, 3600) == 1.2
    assert idx.first_time_reaching("M", 100, 3600, 1.9) is None
    assert idx.first_time_reaching("M", 100, 3600, 1.15) == 599  # counts at the candle end
    assert idx.max_between("M", -1, 3600) == 2.0  # whole candle inside the window: its high counts
    assert idx.max_between("M", 300, 500) is None or idx.max_between("M", 300, 500) <= 1.1  # candle not finished


def test_labels_start_at_the_realistic_entry_after_the_delay():
    from research import events as ev
    from research.prices import PriceIndex
    idx = PriceIndex()
    idx._add("J", [(0, 1.0), (30, 1.6), (900, 1.6), (4000, 1.6)])  # jumped before we could have bought
    hz = [("15m", 900, 0.5)]
    assert ev.labels_at("J", 0, idx, hz, delay=0)["15m"]["pumped"] == 1
    lab = ev.labels_at("J", 0, idx, hz, delay=60)["15m"]
    assert lab["pumped"] == 0 and lab["net"] < 0


def test_intel_source_survives_a_locked_read(monkeypatch):
    import sqlite3
    import types
    from research import intel_features as itf
    calls = {"n": 0}

    def features_at(kind, mint, ts, conn=None):
        calls["n"] += 1
        if calls["n"] <= 3:
            raise sqlite3.OperationalError("database is locked")
        return {"features": {}}

    monkeypatch.setitem(sys.modules, "intel", types.SimpleNamespace(features_at=features_at))
    monkeypatch.setattr(itf.time, "sleep", lambda s: None)
    src = itf.Source(None)
    src.ok = True
    assert src("M", 0)["i_coverage"] == 0  # neutral after 3 locked tries
    assert src.ok  # still on
    assert "i_coverage" in src("M", 120) and calls["n"] == 4


def test_trades_are_valued_on_one_sol_scale():
    from research import data
    from research.store import ensure_schema
    conn = ensure_schema(db.connect(os.path.join(tempfile.mkdtemp(), "s.db")))
    db.insert_trades(conn, [Trade("a", "W1", "M", "buy", 100, 10, 1.0, 120.0, 12.0, "geckoterminal"),
                            Trade("b", "W2", "M", "buy", 200, 10, 1.0, 250.0, 25.0, "rpc"),  # SOL price of today
                            Trade("c", "W3", "M", "buy", 300, 10, 2.0, 400.0, 20.0, "geckoterminal")])
    ds = data.load(conn)
    usd = {t.wallet: t.usd for t in ds.trades}
    assert usd["W1"] == usd["W2"] and abs(usd["W3"] - 2 * usd["W1"]) < 1e-9


def test_no_model_events_from_before_a_coin_was_picked_by_its_pump(market):
    from model.config import msettings
    from research import backtest
    conn, truth, now, vid = market
    ds = data.load(conn)
    mint = ds.trades[len(ds.trades) // 2].mint
    ds.hindsight = {mint: now + 10 ** 9}
    tmap = {t.wallet: ("ultra", 90.0, 0.0, None, None) for t in ds.trades}
    evs = backtest._coin_events(ds, tmap, 0, now + 1, msettings)
    assert evs and all(e["mint"] != mint for e in evs)


def test_wallet_found_through_a_pump_is_not_credited_for_it(market):
    conn, truth, now, vid = market
    ds = data.load(conn)
    w = next(a for a, t in truth.items() if t == "ultra")
    before = wallet_stats.compute(ds, now + 1)[0][w]["n_tokens"]
    mints = {t.mint for t in ds.trades if t.wallet == w and t.side == "buy"}
    m = sorted(mints)[0]
    ds.hindsight, ds.early_found = {m: now + 10 ** 9}, {w}
    assert wallet_stats.compute(ds, now + 1)[0][w]["n_tokens"] == before - 1


def test_isolated_bad_prints_are_dropped_and_real_moves_kept():
    from research.prices import PriceIndex
    idx = PriceIndex()
    idx._add("S", [(0, 1.0), (60, 40.0), (120, 1.1), (180, 1.0)])  # one +3900 % print
    assert idx.max_between("S", 0, 600) < 2 and idx.price_at("S", 90) == 1.0
    idx._add("R", [(0, 1.0), (60, 8.0), (120, 8.5), (180, 9.0)])  # a real move stays
    assert idx.max_between("R", 0, 600) == 9.0 and idx.price_at("R", 90) == 8.0


def test_one_hit_wallets_get_no_tier_and_edge_component_exists():
    from model.tiers import uncopyable
    from research.wallet_stats import COMPONENTS
    base = dict(n_tokens=6, n_exits=0, launch_share=0.0, median_entry_rank=0.4, influence_samples=4)
    assert uncopyable(dict(base, top_coin_share=0.9, net_pnl_wo_best_usd=-50)) == "one_hit"
    assert uncopyable(dict(base, top_coin_share=0.9, net_pnl_wo_best_usd=500)) is None
    assert "edge" in COMPONENTS
