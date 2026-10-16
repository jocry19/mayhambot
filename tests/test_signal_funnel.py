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


def test_outcomes_by_band_after_costs(tmp_path):
    c = conn_with(None, tmp_path)
    now = 50_000
    for score, r1, mx in ((25, 0.10, 0.6), (28, -0.20, 0.1), (45, 0.30, 0.9)):
        c.execute("INSERT INTO signals(ts, mint, score, ret_1h, max_ret_1h) VALUES (?,?,?,?,?)",
                  (now - 5000, "M", score, r1, mx))
    c.execute("INSERT INTO signals(ts, mint, score) VALUES (?,?,30)", (now - 100, "OPEN"))   # not due yet
    c.execute("INSERT INTO signals(ts, mint, score) VALUES (?,?,30)", (now - 5000, "GAP"))   # due, no price
    c.commit()
    b = {x["band"]: x for x in funnel.outcomes(c, hours=2, now=now)}
    assert b["no 1h price"]["n"] == 1
    assert b["20-30"]["n"] == 2 and b["20-30"]["hit_50"] == 0.5
    assert b["20-30"]["mean_ret_1h_after_cost"] == round(-0.05 - 0.06, 3)
    assert b["40-50"]["n"] == 1 and "30-40" not in b


def test_update_outcomes_bounded_and_dead(tmp_path):
    from signals import engine
    c = conn_with(None, tmp_path)
    now = 100_000
    t = now - 5 * 3600
    c.execute("INSERT INTO signals(ts, mint, score, price_usd) VALUES (?,?,?,?)", (t, "LIVE", 50, 1.0))
    c.execute("INSERT INTO signals(ts, mint, score, price_usd) VALUES (?,?,?,?)", (t, "RUG", 50, 1.0))
    c.execute("INSERT INTO tokens(mint, liquidity_usd) VALUES ('RUG', 10)")
    c.execute("INSERT INTO tokens(mint, liquidity_usd) VALUES ('LIVE', 50000)")
    for ts, p in ((t + 300, 1.1), (t + 3550, 1.3), (t + 4 * 3600, 9.0)):
        c.execute("INSERT INTO token_prices(mint, ts, price_usd) VALUES ('LIVE',?,?)", (ts, p))
    c.execute("INSERT INTO token_prices(mint, ts, price_usd) VALUES ('RUG',?,0.5)", (t + 200,))
    c.commit()
    engine.update_outcomes(c, now=now)
    live = c.execute("SELECT * FROM signals WHERE mint='LIVE'").fetchone()
    rug = c.execute("SELECT * FROM signals WHERE mint='RUG'").fetchone()
    assert round(live["ret_1h"], 3) == 0.3 and round(live["ret_5m"], 3) == 0.1 and live["outcome_flag"] == "gap"
    assert live["ret_15m"] is None                         # nothing within 5 min of +15m: no stale substitute
    assert rug["ret_1h"] == -0.95 and rug["max_ret_1h"] == 0.5 - 1 and rug["outcome_flag"] == "dead"


def test_report_file(tmp_path):
    c = conn_with(None, tmp_path)
    p = funnel.write_report(c, path=tmp_path / "r" / "signals_report.json", now=10_000)
    rep = json.loads(p.read_text())
    assert set(rep) >= {"funnel_4h", "funnel_24h", "outcomes_24h", "outcomes_7d", "calibration", "recent_signals"}


def test_metrics_trimmed_and_pruned(tmp_path):
    from signals import engine
    c = conn_with(None, tmp_path)
    big = {f"f{i}": {"points": i} for i in range(40)}
    top = engine.top_contributions(big)
    assert len(top) == 10 and "f39" in top and "f0" not in top
    now = 1_000_000
    c.execute("INSERT INTO signal_metrics(ts, mint, score) VALUES (?, 'OLD', 1)", (now - 4 * 86400,))
    c.execute("INSERT INTO signal_metrics(ts, mint, score) VALUES (?, 'NEW', 1)", (now - 3600,))
    c.commit()
    engine._last_prune["at"] = 0
    assert engine.prune_metrics(c, now=now) == 1
    assert [r[0] for r in c.execute("SELECT mint FROM signal_metrics")] == ["NEW"]
