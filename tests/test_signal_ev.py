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
    assert ev.predict(t, 2, 300, 0)[0] == b["ev"]
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
