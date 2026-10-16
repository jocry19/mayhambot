import dataclasses

from core import db
from core.config import settings
from signals import backtest


def test_replay_fires_with_cooldown_and_measures_after_cost(tmp_path):
    c = db.connect(str(tmp_path / "t.db"))
    for a, tier, cl in (("S1", "smart", None), ("S2", "smart", None), ("S3", "smart", "C"), ("S4", "smart", "C")):
        c.execute("INSERT INTO wallets(address, tier, score, cluster_id) VALUES (?,?,70,?)", (a, tier, cl))
    t0 = 1_000_000
    n = 0

    def tr(w, mint, ts, side="buy", usd=200.0, price=None):
        nonlocal n
        n += 1
        c.execute("INSERT INTO trades(sig, wallet, mint, side, ts, usd_amount, price_usd) VALUES (?,?,?,?,?,?,?)",
                  (f"s{n}", w, mint, side, ts, usd, price))
    tr("S1", "UP", t0)
    tr("S2", "UP", t0 + 60)
    for i in range(0, 4000, 60):                           # price path: up 80 % within the hour
        tr("X", "UP", t0 + 120 + i, price=1.0 + min(i, 1800) / 1800 * 0.8)
    tr("S3", "CL", t0)                                     # same cluster: one entity
    tr("S4", "CL", t0 + 60)
    for i in range(0, 4000, 60):
        tr("X", "CL", t0 + 120 + i, price=1.0)
    c.commit()
    cfg = dataclasses.replace(settings, signal_window_min=30, signal_cooldown_min=60)
    r = backtest.run(c, hours=3, now=t0 + 2 * 3600 + 600, cfg=cfg,
                     grid=dict(wallets=[1, 2], net=[300], score=[0], p=[None], sellers=[None]), folds=1)
    res = {x["wallets"]: x for x in r["results"]}
    assert res[2]["n"] == 1 and res[2]["hit_50"] == 1.0         # UP only: CL is one entity
    assert res[2]["mean_tp"] == round(0.5 - 0.06, 4)            # take profit at +50 % minus costs
    assert res[1]["n"] == 2                                      # CL counts with 1 entity; cooldown keeps 1 per coin


def test_point_in_time_tiers_and_walk_forward():
    import sqlite3
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE model_versions (id INTEGER PRIMARY KEY, ts INTEGER)")
    c.execute("CREATE TABLE model_tier_history (version INTEGER, address TEXT, tier TEXT, score REAL)")
    c.executemany("INSERT INTO model_versions VALUES (?,?)", [(1, 100), (2, 500)])
    c.executemany("INSERT INTO model_tier_history VALUES (?,?,?,?)",
                  [(1, "A", "smart", 60), (2, "B", "ultra", 80), (2, "A", None, 10)])
    v, pit = backtest.tier_versions(c)
    assert pit and backtest.tiers_for(v, 300) == {"A": ("smart", 60)}
    assert backtest.tiers_for(v, 900) == {"B": ("ultra", 80)}

    # walk-forward picks on the past, judges on the future: a rule that only won in the past is not rewarded
    def snap(t, ent, mint):
        return (t, mint, dict(net=1000, entities=ent, score=50, sellers=0), None)
    snaps = [snap(t, 1 if t < 4000 else 2, f"M{t}") for t in range(0, 8000, 400)]
    prices = {f"M{t}": [(t, 1.0), (t + 3600, 2.0 if t < 4000 else 0.5)] for t in range(0, 8000, 400)}
    grid = dict(wallets=[1], net=[100], score=[0], p=[None], sellers=[None])
    wf = backtest.walk_forward(snaps, prices, grid, folds=2, min_n=3)
    assert wf["oos"]["n"] == 10 and wf["oos"]["mean_tp"] < 0          # the future lost money


def test_outcome_uses_price_near_the_hour_and_counts_dead_coins():
    t = 10_000
    # price near +1h: the print 3 h later must not be used
    series = [(t + 10, 1.0), (t + 600, 1.2), (t + 3500, 0.9), (t + 3 * 3600, 5.0)]
    o = backtest.outcome(series, t)
    assert round(o["ret_1h"], 3) == -0.1 and round(o["max_ret_1h"], 3) == 0.2
    # watched coin that stops trading after 10 min: dead, not dropped
    dead = [(t + 10, 1.0), (t + 600, 0.4)]
    o = backtest.outcome(dead, t, watched=[(t - 300, 1.0)])
    assert o["ret_1h"] == backtest.DEAD_RET and o["dead"]
    # unwatched coin without later prices: not measurable
    assert backtest.outcome(dead, t) is None
