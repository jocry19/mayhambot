from core import db
from signals import engine


def test_no_smart_split_counts_db_tiered_and_model_zeroed(tmp_path):
    c = db.connect(str(tmp_path / "n.db"))
    now = 1_000_000
    c.execute("INSERT INTO wallets(address, tier, is_smart) VALUES ('W', 'smart', 1)")
    c.execute("INSERT INTO trades(sig, wallet, mint, side, ts, usd_amount) VALUES ('s','W','M','buy',?,100)", (now - 60,))
    c.commit()
    f = {}
    engine._why_no_smart(c, "M", {"smart_buyers": 1}, f, now)
    engine._why_no_smart(c, "X", {"smart_buyers": 0}, f, now)
    assert f["no_smart_db_tiered"] == 1 and f["no_smart_model_zeroed"] == 1


def test_smart_active_mints_follow_model_tiers_once_a_model_is_active(tmp_path):
    from signals import run
    c = db.connect(str(tmp_path / "m.db"))
    now = 1_000_000
    c.executemany("INSERT INTO wallets(address, tier, is_smart, tier_source, manual) VALUES (?,?,1,?,?)",
                  [("H", "smart", "heuristic", 0), ("M", "smart", "model", 0), ("U", "smart", "heuristic", 1)])
    c.executemany("INSERT INTO trades(sig, wallet, mint, side, ts, usd_amount) VALUES (?,?,?,'buy',?,100)",
                  [("1", "H", "HC", now - 60), ("2", "M", "MC", now - 60), ("3", "U", "UC", now - 60)])
    c.commit()
    assert set(run.smart_active_mints(c, now)) == {"MC", "UC"}      # heuristic-only buyers count nowhere in v1
    c.execute("UPDATE wallets SET tier_source='heuristic'")
    assert set(run.smart_active_mints(c, now)) == {"HC", "MC", "UC"}  # no model: heuristic tiers are the truth
