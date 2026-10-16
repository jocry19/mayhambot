
from core import db, scoring


def _conn(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS model_versions (id INTEGER PRIMARY KEY, ts INTEGER, active INTEGER);
        CREATE TABLE IF NOT EXISTS model_wallet_stats (address TEXT PRIMARY KEY, version INTEGER, tier TEXT,
            score REAL, cluster_id TEXT, exit_capture REAL);""")
    return conn


def test_without_active_model_heuristic_stays(tmp_path):
    conn = _conn(tmp_path)
    conn.execute("INSERT INTO wallets(address, tier, is_smart) VALUES ('A', 'smart', 1)")
    assert scoring.sync_model_tiers(conn) is None
    assert conn.execute("SELECT tier FROM wallets WHERE address='A'").fetchone()[0] == "smart"


def test_model_is_the_one_truth(tmp_path):
    conn = _conn(tmp_path)
    conn.executemany("INSERT INTO wallets(address, tier, is_smart, manual) VALUES (?,?,?,?)",
                     [("OLD", "smart", 1, 0), ("KEEP", "good", 0, 0), ("MAN", None, 0, 1)])
    conn.execute("INSERT INTO model_versions VALUES (3, 0, 1)")
    conn.executemany("INSERT INTO model_wallet_stats VALUES (?,?,?,?,?,?)",
                     [("KEEP", 3, "ultra", 91, "c1", 0.7), ("NEW", 3, "good", 50, None, None)])
    r = scoring.sync_model_tiers(conn)
    assert r == {"version": 3, "ultra": 1, "smart": 0, "good": 1}
    t = {a: (tier, s, src) for a, tier, s, src in
         conn.execute("SELECT address, tier, is_smart, tier_source FROM wallets")}
    assert t["OLD"] == (None, 0, "model")          # heuristic smart, model says nothing -> not smart
    assert t["KEEP"] == ("ultra", 1, "model")
    assert t["NEW"] == ("good", 0, "model")         # model wallet unknown to signals is added
    assert t["MAN"][:2] == ("smart", 1)             # manual wallets stay smart
    assert tuple(conn.execute("SELECT cluster_id, exit_capture FROM wallets WHERE address='KEEP'").fetchone()) == ("c1", 0.7)


def test_rescore_all_applies_model_tiers(tmp_path):
    conn = _conn(tmp_path)
    conn.execute("INSERT INTO model_versions VALUES (4, 0, 1)")
    conn.execute("INSERT INTO model_wallet_stats VALUES ('W', 4, 'smart', 70, NULL, NULL)")
    scoring.rescore_all(conn)
    assert tuple(conn.execute("SELECT tier, tier_version FROM wallets WHERE address='W'").fetchone()) == ("smart", 4)
