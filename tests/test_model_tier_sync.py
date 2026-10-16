
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
    assert {k: r[k] for k in ("version", "ultra", "smart", "good", "scored")} == \
        {"version": 3, "ultra": 1, "smart": 0, "good": 1, "scored": 2}
    t = {a: (tier, s, src) for a, tier, s, src in
         conn.execute("SELECT address, tier, is_smart, tier_source FROM wallets")}
    assert t["OLD"] == ("smart", 1, "heuristic")   # not scored by the model yet -> keeps its heuristic tier
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


def test_scored_but_not_good_enough_loses_tier(tmp_path):
    conn = _conn(tmp_path)
    conn.execute("INSERT INTO wallets(address, tier, is_smart) VALUES ('X', 'smart', 1)")
    conn.execute("INSERT INTO model_versions VALUES (5, 0, 1)")
    conn.execute("INSERT INTO model_wallet_stats VALUES ('X', 5, NULL, 20, NULL, NULL)")
    scoring.sync_model_tiers(conn)
    assert tuple(conn.execute("SELECT tier, is_smart, tier_source FROM wallets WHERE address='X'").fetchone()) == \
        (None, 0, "model")


def test_cluster_counts_once(tmp_path):
    from signals import engine
    conn = _conn(tmp_path)
    conn.execute("INSERT INTO model_versions VALUES (6, 0, 1)")
    conn.executemany("INSERT INTO model_wallet_stats VALUES (?,?,?,?,?,?)",
                     [("A", 6, "smart", 70, "c1", None), ("B", 6, "smart", 70, "c1", None),
                      ("C", 6, "ultra", 90, None, None)])
    now = 10_000
    conn.executemany("INSERT INTO trades(sig, wallet, mint, side, ts, token_amount, sol_amount, usd_amount, price_usd, "
                     "source) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     [(f"s{w}", w, "M", "buy", now - 60, 1, 5, 1000, 1, "t") for w in "ABC"])
    m = engine.token_metrics(conn, "M", engine.QualityCache(conn), now=now)
    assert m["smart_buyers"] == 2 and m["n_smart"] == 1 and m["n_ultra"] == 1 and m["buy_usd"] == 3000


def test_rescore_skips_without_new_trades(tmp_path, monkeypatch):
    from core import db, scoring
    from core.models import Trade
    conn = db.connect(str(tmp_path / "r.db"))
    db.insert_trades(conn, [Trade("a", "W", "M", "buy", 1000, 10.0, 0.1, 10.0, 1.0, "t")])
    runs = []
    real = scoring._rescore
    monkeypatch.setattr(scoring, "_rescore", lambda c, cfg: runs.append(1) or real(c, cfg))
    scoring.rescore_all(conn)
    scoring.rescore_all(conn)  # nothing new: skipped
    assert len(runs) == 1
    db.insert_trades(conn, [Trade("b", "W", "M", "sell", 2000, 10.0, 0.2, 20.0, 2.0, "t")])
    scoring.rescore_all(conn)
    scoring.rescore_all(conn, force=True)
    assert len(runs) == 3
