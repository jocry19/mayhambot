import dataclasses
import json

from core import db
from core.config import settings
from signals import engine, ev


def res(ent, net, sellers, tp, t=0):
    return dict(entities=ent, net=net, sellers=sellers, tp_ret=tp, t=t, ret_1h=tp, max_ret_1h=tp)


def test_fit_shrinks_small_buckets_toward_global_mean():
    rs = [res(2, 300, 0, 0.5)] * 2 + [res(1, 100, 0, 0.0)] * 18
    t = ev.fit(rs, cost=0.06, shrink=10)
    g = (2 * 0.44 + 18 * -0.06) / 20
    assert t["global_ev"] == round(g, 4)
    b = t["buckets"][ev.bucket(2, 300, 0)]
    assert b["n"] == 2 and b["mean"] == 0.44 and g < b["ev"] < 0.44      # shrunk, still above the mean
    assert ev.predict(t, 2, 300, 0)[0] == t["buckets"][ev.bucket(2, 300, 0) + "|pna"]["ev"]
    assert ev.predict(t, 3, 900, 2) == (t["global_ev"], 0, "e3+|n>=500|s1+")   # unseen bucket
    assert ev.predict(None, 2, 300, 0) == (None, 0, None)


def test_ev_gate_and_safety_veto():
    cfg = dataclasses.replace(settings, signal_min_wallets=2, signal_min_net_usd=500, signal_min_score=60)
    m = dict(smart_buyers=1, net_usd=150, score=10, ev=0.08)
    assert engine._reason(m, cfg) is None                       # EV replaces wallets / net / score minimums
    assert engine._reason(dict(m, ev=-0.02), cfg) == "low_ev"
    assert engine._reason(dict(m, safety={"factor": 0.3}), cfg) == "safety_drop"
    assert engine._reason(dict(m, smart_buyers=0), cfg) == "no_smart"
    unchecked = dict(m, safety={"factor": 0.5, "checked": False, "level": "unchecked"})
    assert engine._reason(unchecked, cfg) is None                       # gets its quick check first
    assert engine._reason(unchecked, cfg, strict=True) == "safety_drop"   # still unchecked afterwards: blocked
    assert engine._reason(dict(m, ev=None), cfg) == "few_entities"      # no table: fixed thresholds


def test_explain_names_pros_and_cons():
    txt = engine.explain(dict(smart_buyers=2, entities=2, net_usd=420, n_ultra=1, prediction=0.12, ev=0.07, ev_n=31,
                              sellers=1, model_exp_net=-0.03, model_p_net=0.31, safety={"risk": 40, "factor": 0.7, "reasons": ["Top-10 halten 60 %"]}))
    assert txt.startswith("Dafür: 2 Smart-Käufer") and "+7 %" in txt and "n=31" in txt
    assert "Modell erwartet -3 % nach Kosten (31 % Gewinnchance)" in txt
    assert "Dagegen:" in txt and "verkauft schon" in txt and "Rug-Risiko 40" in txt and "Modell: 12 %" in txt


def test_calibration_goes_active_only_after_out_of_sample_profit(tmp_path, monkeypatch):
    c = db.connect(str(tmp_path / "t.db"))
    good = [res(2, 400, 0, 0.5, t=i) for i in range(0, 60, 2)]
    bad = [res(1, 100, 1, -0.3, t=i) for i in range(1, 60, 2)]
    monkeypatch.setattr(ev.backtest, "load", lambda *a, **k: dict(pit=True, prices={}))
    monkeypatch.setattr(ev.backtest, "snapshots", lambda *a, **k: [])
    monkeypatch.setattr(ev.backtest, "replay", lambda *a, **k: good + bad)
    r = ev.calibrate(c, hours=24, now=10_000, min_oos_n=5)
    assert r["active"] and r["oos"]["n"] == 10 and r["oos"]["mean_tp"] > 0       # picked only the good bucket
    row = c.execute("SELECT active, table_json FROM signal_calibration").fetchone()
    assert row[0] == 1 and json.loads(row[1])["n"] == 60
    monkeypatch.setattr(ev.time, "time", lambda: 10_000)
    assert ev.active_table(c)["n"] == 60 and not ev.due(c, now=10_000)

    monkeypatch.setattr(ev.backtest, "replay", lambda *a, **k: bad + [res(2, 400, 0, -0.2, t=i) for i in range(0, 60, 2)])
    r = ev.calibrate(c, hours=24, now=10_100, min_oos_n=5)
    assert not r["active"]
    monkeypatch.setattr(ev.time, "time", lambda: 10_100)
    assert ev.active_table(c) is None                                             # losing table never goes live


def test_blend_weights_bucket_by_evidence():
    assert ev.blend(0.10, 30, None) == 0.10
    assert ev.blend(0.10, 0, -0.05, shrink=10) == -0.05            # no bucket evidence: the model decides
    assert ev.blend(0.10, 30, -0.05, shrink=10) == round(0.75 * 0.10 + 0.25 * -0.05, 4)


def test_model_guard_restarts_only_after_a_retrain(tmp_path):
    c = db.connect(str(tmp_path / "t.db"))
    c.execute("CREATE TABLE IF NOT EXISTS model_versions (id INTEGER PRIMARY KEY, ts INTEGER, active INTEGER)")
    c.execute("INSERT INTO model_versions VALUES (4, 1, 1)")
    g = engine.ModelGuard()
    g.start(c)
    for _ in range(6):
        g.record(False, IndexError("list index out of range"))
    g.check(c)                                                     # same version: data problem, keep running
    assert g.stats()["fallbacks"] == 6 and "IndexError" in g.stats()["top_errors"][0][0]
    c.execute("INSERT INTO model_versions VALUES (5, 2, 1)")
    try:
        g.check(c)
        raise AssertionError("expected ModelCodeStale")
    except engine.ModelCodeStale as e:
        assert "v5" in str(e) and "v4" in str(e)
    g.reset()
    g.record(True)
    for _ in range(6):
        g.record(False, IndexError("x"))
    g.check(c)                                                     # the model still works for some coins: no restart
    g.reset()
    for _ in range(9):
        g.record(False, type("ModelNotTrained", (Exception,), {})("no version"))
    assert g.fallbacks == 0


def test_tokenized_stocks_are_not_memecoins():
    assert engine.not_a_memecoin(dict(name="NVIDIA xStock", symbol="NVDAx", mcap_usd=44e6))
    assert engine.not_a_memecoin(dict(name="", symbol="TSLAx", mcap_usd=20e6))
    assert not engine.not_a_memecoin(dict(name="dogwifhat", symbol="WIF", mcap_usd=2e9))
    assert not engine.not_a_memecoin(dict(name="Bonkx", symbol="BONKx", mcap_usd=50_000))
    assert not engine.not_a_memecoin(None)


def test_incompatible_model_counts_as_fallback():
    g = engine.ModelGuard()
    NotTrained = type("ModelNotTrained", (Exception,), {})
    Incompatible = type("ModelIncompatible", (NotTrained,), {})
    g.record(False, Incompatible("feature_set differs"))
    g.record(False, NotTrained("no version"))
    assert g.fallbacks == 1 and "ModelIncompatible" in g.stats()["top_errors"][0][0]


def test_predictions_stored_every_15_minutes_per_coin():
    engine._stored.clear()
    assert engine._store_now("M", 1000) and not engine._store_now("M", 1500) and engine._store_now("M", 1900)
    assert engine._store_now("N", 1500)


def test_model_band_bucket_shrinks_toward_its_setup():
    rs = ([dict(res(2, 300, 0, -0.2), p=0.05)] * 20 + [dict(res(2, 300, 0, 0.5), p=0.35)] * 5)
    t = ev.fit(rs, cost=0.06, shrink=10)
    base = t["buckets"][ev.bucket(2, 300, 0)]
    hi = t["buckets"][ev.bucket(2, 300, 0) + "|p>=.30"]
    assert hi["n"] == 5 and base["ev"] < hi["ev"] < 0.44
    assert ev.predict(t, 2, 300, 0, 0.4)[2].endswith("|p>=.30")
    assert ev.predict(t, 2, 300, 0, None)[2] == ev.bucket(2, 300, 0)      # no 'na' band seen: setup bucket


def test_reason_flags_thin_liquidity():
    txt = engine.explain(dict(smart_buyers=2, entities=2, net_usd=300, liquidity_usd=8000))
    assert "nur $8,000 Liquidität" in txt and "Dagegen:" in txt
    assert "Liquidität" not in engine.explain(dict(smart_buyers=2, entities=2, net_usd=300, liquidity_usd=40000))
