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
                     grid=dict(wallets=[1, 2], net=[300], score=[0], p=[None]))
    res = {x["wallets"]: x for x in r["results"]}
    assert res[2]["n"] == 1 and res[2]["hit_50"] == 1.0         # UP only: CL is one entity
    assert res[2]["mean_tp"] == round(0.5 - 0.06, 4)            # take profit at +50 % minus costs
    assert res[1]["n"] == 2                                      # CL counts with 1 entity; cooldown keeps 1 per coin
