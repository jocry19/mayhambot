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
