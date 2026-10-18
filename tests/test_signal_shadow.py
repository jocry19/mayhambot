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
    assert shadow.record(c, "B", "no_smart", m, 1.0, t)                    # kept: big runners had <= 1 smart buyer
    c.execute("INSERT INTO tokens(mint, liquidity_usd, last_updated) VALUES ('A', 40000, ?)", (NOW - 60,))
    for dt, p in ((120, 1.1), (1200, 1.8), (3600, 1.3), (3 * 3600, 2.5)):
        c.execute("INSERT INTO token_prices(mint, ts, price_usd) VALUES ('A',?,?)", (t + dt, p))
    c.commit()
    assert shadow.update(c, NOW) == 3
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


def test_momentum_and_venue_features_recorded(tmp_path):
    c = db.connect(str(tmp_path / "s.db"))
    t = NOW - 5 * 3600
    c.execute("INSERT INTO tokens(mint, dex, mcap_usd, pair_created_at, txns_m5, txns_h1, volume_h1) "
              "VALUES ('D','pumpswap',900000,?,60,120,50000)", ((t - 3600) * 1000,))
    c.execute("INSERT INTO token_prices(mint, ts, price_usd) VALUES ('D',?,1.5)", (t - 900,))
    c.commit()
    shadow.record(c, "D", "few_entities", dict(entities=1), 1.0, t)
    r = c.execute("SELECT * FROM signal_shadow").fetchone()
    assert r["dex"] == "pumpswap" and round(r["ret_15m_before"], 3) == round(1 / 1.5 - 1, 3) and r["age_min"] == 60
    g = shadow.groups(c.execute("SELECT * FROM signal_shadow").fetchall())
    assert g["dex pumpswap"]["n"] == 1 and g["dip >= 20 % in 15 min before"]["n"] == 1
    assert g["txns 5 min >= 2x hour pace"]["n"] == 1


def test_every_candidate_is_an_opportunity_for_the_brain(tmp_path):
    import json
    c = db.connect(str(tmp_path / "o.db"))
    c.execute("INSERT INTO tokens(mint, symbol, dex, mcap_usd, price_change_m5) VALUES ('O','OPP','pumpswap',8e5,6.5)")
    c.commit()
    m = dict(entities=1, net_usd=300, net_sol=2.0, score=35, liquidity_usd=40000, safety=dict(risk=20, factor=1.0))
    assert shadow.record(c, "O", "few_entities", m, 1.0, NOW)
    shadow.record(c, "O", "no_smart", dict(entities=0, smart_buyers=0, net_usd=0), 1.0, NOW)
    rows = c.execute("SELECT source, source_reason, source_action, features FROM opportunities ORDER BY id").fetchall()
    assert [(r[0], r[1], r[2]) for r in rows] == [("signals", "few_entities", "passed"), ("signals", "no_smart", "passed")]
    f = json.loads(rows[0][3])
    assert f["wallets_n"] == 1 and f["inflow_usd"] == 300 and f["dex"] == "pumpswap" and f["pc_5m"] == 6.5
    assert f["risk"] == 20 and f["signals.reason"] == "few_entities"


def test_band_candidates_broken_down_by_reason():
    from signals import shadow
    keys = ("reason", "entities", "net_usd", "min_net", "safety_risk", "ret_1h", "max_ret_1h", "max_ret_4h")
    rows = [dict(zip(keys, v)) for v in (("thin_liq", 2, 900, 3000, None, 0.1, 0.6, 0.6),
                                        ("cooldown", 3, 1500, 3000, None, -0.2, 0.1, 0.2),
                                        ("thin_liq", 1, 900, 3000, None, 0.1, 0.6, 0.6))]
    rows = [type("R", (dict,), {"keys": dict.keys})(r) for r in rows]
    g = shadow.groups(rows)
    assert g["band candidate, thin_liq"]["n"] == 1 and g["band candidate, cooldown"]["n"] == 1
    assert g["entities>=2, net <500"]["n"] == 0 and g["entities>=2, net >2000"]["n"] == 0
    assert shadow._band(dict(entities=2, smart_buyers=2, net_usd=900)) is True
