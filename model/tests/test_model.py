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
