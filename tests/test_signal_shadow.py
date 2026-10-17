from core import db
from signals import shadow

NOW = 3_000_000


def test_rejected_coin_recorded_once_per_hour_and_rated(tmp_path):
    c = db.connect(str(tmp_path / "s.db"))
    t = NOW - 5 * 3600
    m = dict(entities=1, net_usd=300, score=35, min_net=100, min_score=40)
    assert shadow.record(c, "A", "few_entities", m, 1.0, t)
    assert not shadow.record(c, "A", "few_entities", m, 1.0, t + 600)     # same reason within the hour
    assert shadow.record(c, "A", "low_net", m, 1.0, t + 600)
    assert not shadow.record(c, "B", "no_smart", m, 1.0, t)                # nothing to learn
    c.execute("INSERT INTO tokens(mint, liquidity_usd, last_updated) VALUES ('A', 40000, ?)", (NOW - 60,))
    for dt, p in ((120, 1.1), (1200, 1.8), (3600, 1.3), (3 * 3600, 2.5)):
        c.execute("INSERT INTO token_prices(mint, ts, price_usd) VALUES ('A',?,?)", (t + dt, p))
    c.commit()
    assert shadow.update(c, NOW) == 2
    r = c.execute("SELECT * FROM signal_shadow WHERE reason='few_entities'").fetchone()
    assert round(r["max_ret_1h"], 3) == round(1.8 / 1.1 - 1, 3) and round(r["max_ret_4h"], 3) == round(2.5 / 1.1 - 1, 3)
    g = shadow.summary(c, 24, NOW)
    assert g["few_entities"]["hit_50"] == 1.0 and g["few_entities: 1 owner, net >= min"]["n"] == 1


def test_coin_that_vanished_after_rejection_is_dead(tmp_path):
    c = db.connect(str(tmp_path / "s.db"))
    t = NOW - 5 * 3600
    shadow.record(c, "R", "low_net", dict(entities=2, net_usd=50, min_net=100), 1.0, t)
    c.execute("INSERT INTO tokens(mint, liquidity_usd, last_updated) VALUES ('R', 40000, ?)", (t + 300,))
    c.commit()
    shadow.update(c, NOW)
    r = c.execute("SELECT ret_1h, outcome_flag FROM signal_shadow").fetchone()
    assert r[0] == -0.95 and r[1] == "dead"


def test_retro_from_signal_metrics(tmp_path):
    c = db.connect(str(tmp_path / "s.db"))
    t = NOW - 5 * 3600
    for ts, mint, ent, net, score in ((t, "A", 1, 300, 30), (t + 60, "A", 1, 300, 30), (t, "B", 3, 50, 30),
                                      (t, "C", 3, 900, 20)):
        c.execute("INSERT INTO signal_metrics(ts, mint, smart_buyers, net_usd, score, price_usd) VALUES (?,?,?,?,?,1.0)",
                  (ts, mint, ent, net, score))
    c.execute("INSERT INTO token_prices(mint, ts, price_usd) VALUES ('A',?,1.6)", (t + 1800,))
    c.commit()
    g = shadow.retro(c, 100, 40, hours=24, now=NOW)
    assert g["few_entities"]["n"] == 1 and g["few_entities"]["hit_50"] == 1.0
    assert g["low_net"]["n"] == 1 and g["score_or_safety"]["n"] == 1
