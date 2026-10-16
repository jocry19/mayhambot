import dataclasses
import json
import sqlite3

from core import db
from core.config import settings
from signals import engine, funnel


def conn_with(base_rate=None, tmp_path=None):
    c = db.connect(str(tmp_path / "t.db"))
    c.execute("""CREATE TABLE IF NOT EXISTS model_versions (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER,
                 active INTEGER DEFAULT 0, metrics_json TEXT)""")
    if base_rate is not None:
        c.execute("INSERT INTO model_versions(ts, active, metrics_json) VALUES (1, 1, ?)",
                  (json.dumps({"1h": {"base_rate": base_rate}}),))
    c.commit()
    engine._model_thr.update(version=None, value=None, base=None)
    return c


def test_model_threshold_follows_base_rate(tmp_path):
    cfg = dataclasses.replace(settings, model_signal_min_score=40, model_signal_min_lift=2.0, model_signal_floor=10)
    assert engine.cfg_min_model(conn_with(0.08, tmp_path), cfg) == 16.0          # 2 x 8 %
    assert engine.cfg_min_model(conn_with(0.3, tmp_path / ".."), cfg) == 40      # capped by MIN_SCORE


def test_model_threshold_floor_and_fallback(tmp_path):
    cfg = dataclasses.replace(settings, model_signal_min_score=40, model_signal_min_lift=2.0, model_signal_floor=10)
    assert engine.cfg_min_model(conn_with(0.01, tmp_path), cfg) == 10           # never below the floor
    assert engine.cfg_min_model(None, cfg) == 40
    c = sqlite3.connect(":memory:")                                               # no model tables at all
    assert engine.cfg_min_model(c, cfg) == 40


def test_reason_is_first_failing_condition():
    cfg = dataclasses.replace(settings, signal_min_wallets=2, signal_min_net_usd=500, signal_min_score=60)
    m = dict(smart_buyers=0, net_usd=0, score=0)
    assert engine._reason(m, cfg) == "no_smart"
    assert engine._reason(dict(m, smart_buyers=1), cfg) == "few_entities"
    assert engine._reason(dict(m, smart_buyers=2, net_usd=100), cfg) == "low_net"
    assert engine._reason(dict(m, smart_buyers=2, net_usd=900, score=59), cfg) == "low_score"
    assert engine._reason(dict(m, smart_buyers=2, net_usd=900, score=59, min_score=20), cfg) is None


def test_funnel_counts_stored_metrics(tmp_path):
    c = conn_with(0.1, tmp_path)
    now = 10_000
    model = json.dumps({"n_smart_buyers": {"points": 1}})
    heur = json.dumps({"inflow": {"points": 1}})
    rows = [(now - 60, "A", 0, 0, 0.0, heur), (now - 60, "B", 900, 2, 70.0, heur),
            (now - 30, "C", 900, 3, 25.0, model), (now - 30, "D", 100, 1, 50.0, model)]
    for ts, mint, net, sb, score, contrib in rows:
        c.execute("INSERT INTO signal_metrics(ts, mint, window_min, net_usd, smart_buyers, score, n_good, "
                  "contributions_json) VALUES (?,?,30,?,?,?,0,?)", (ts, mint, net, sb, score, contrib))
    c.commit()
    f = funnel.funnel(c, hours=1, now=now)
    assert f["rows"] == 4 and f["model_rows"] == 2 and f["coins"] == 4
    assert f["smart_ge_min"] == 2 and f["net_ge_min"] == 2
    assert f["all_pass"] == 1                      # B only: C misses the fixed 40
    assert f["all_pass_calibrated"] == 2           # C passes 2 x 10 % = 20
    assert f["max_score_model"] == 50.0 and f["trades"] == 0
