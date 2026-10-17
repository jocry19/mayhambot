import json
import sqlite3
import time

from core import retention


def _db(tmp_path):
    c = sqlite3.connect(tmp_path / "x.db")
    c.execute("CREATE TABLE trades (sig TEXT, ts INTEGER, wallet TEXT)")
    c.execute("CREATE INDEX idx_t ON trades(ts)")
    c.execute("CREATE TABLE kv (k TEXT PRIMARY KEY, v TEXT)")
    now = 1_760_000_000
    c.executemany("INSERT INTO trades VALUES (?,?,?)", [(f"s{i}", now - i * 600, "w" * 44) for i in range(500)])
    c.execute("INSERT INTO kv VALUES ('a','b')")
    c.commit()
    return c, now


def test_table_sizes_counts_rows_recent_and_index_bytes(tmp_path):
    c, now = _db(tmp_path)
    s = retention.table_sizes(c)
    assert s["trades"]["rows"] == 500
    assert s["trades"]["recent"] == 144            # 10-min spacing: 144 rows within 24 h of the newest
    assert s["kv"]["recent"] is None               # no time column
    if s["trades"]["bytes"]:                       # dbstat available
        assert "idx_t" not in s and s["trades"]["bytes"] > s["kv"]["bytes"]


def test_log_sizes_keeps_hourly_then_daily_and_reports_growth(tmp_path):
    c, now = _db(tmp_path)
    path = tmp_path / "sizes.json"
    t0 = time.time()
    for h in range(0, 80, 1):                      # 80 hourly runs
        if h == 40:
            c.executemany("INSERT INTO trades VALUES (?,?,?)", [(f"n{i}", now + i, "w") for i in range(240)])
            c.commit()
        sizes, fstats, grow = retention.log_sizes(c, path, now=t0 + h * 3600)
    log = json.loads(path.read_text())
    assert len(log) < 80 and len(log) >= 48         # 48 h hourly + one per older day
    assert grow["trades"][1] > 0                   # rows per day went up
    text = retention.size_report(sizes, fstats, grow)
    assert text.splitlines()[1].startswith("trades:") and "Zeilen" in text


def test_estimate_without_dbstat_is_close(tmp_path, monkeypatch):
    c, now = _db(tmp_path)
    c.executemany("INSERT INTO trades VALUES (?,?,?)", [(f"x{i}" * 20, now + i, "w" * 44) for i in range(20000)])
    c.commit()
    real = retention.table_sizes(c)["trades"]["bytes"]
    est = retention._estimate_bytes(c, "trades", c.execute("SELECT COUNT(*) FROM trades").fetchone()[0])
    if real:
        assert 0.6 * real < est < 1.6 * real, (real, est)


def test_trades_profile(tmp_path):
    c = sqlite3.connect(tmp_path / "p.db")
    c.execute("CREATE TABLE trades (sig, wallet, mint, side, ts, source)")
    rows = [("a1", "one1", "A", "buy", 1, "geckoterminal"), ("a2", "one2", "A", "buy", 2, "geckoterminal"),
            ("b1", "multi", "B", "buy", 3, "geckoterminal"), ("c1", "multi", "C", "buy", 4, "geckoterminal"),
            ("b2", "one3", "B", "buy", 5, "geckoterminal"), ("d1", "hist", "D", "buy", 6, "rpc")]
    c.executemany("INSERT INTO trades VALUES (?,?,?,?,?,?)", rows)
    c.execute("CREATE TABLE token_prices (mint, ts, price_usd, mcap_usd, liquidity_usd)")
    c.executemany("INSERT INTO token_prices VALUES (?,?,?,?,?)",
                  [("A", 3600 * 10 + 60 * i, 1 + (i % 7), 0, 100 - i) for i in range(60)])
    p = retention.trades_profile(c)
    assert p["prices_rows"] == 60 and p["prices_thin_rows"] <= 4
    assert p["oneoff_wallets"] == 3 and p["oneoff_rows"] == 3      # hist was backfilled: not one-off
    assert p["oneoff_coin_rows"] == 2                              # coin A: only one-off wallets
    assert "Einmal-Wallets" in retention.profile_report(p)


DAY = 86400


def _live(tmp_path, auto_vacuum=None):
    from core import db
    path = tmp_path / "live.db"
    if auto_vacuum:
        c0 = sqlite3.connect(path)
        c0.execute(f"PRAGMA auto_vacuum={auto_vacuum}")
        c0.execute("CREATE TABLE x(y)")
        c0.close()
    c = db.connect(str(path))
    now = 1_760_000_000
    old = now - 40 * DAY
    rows = []
    for i in range(300):   # coin JUNK: only one-off wallets, 40 days old
        rows.append((f"j{i}", f"one{i}", "JUNK", "buy" if i % 3 else "sell", old + i, 10.0, 0.1, 15.0, 1e-6 * (1 + i % 5),
                     "geckoterminal"))
    for i in range(50):    # coin GOOD: a two-coin wallet bought it, plus one-off followers
        rows.append((f"g{i}", f"fol{i}", "GOOD", "buy", old + i, 10.0, 0.1, 15.0, 1e-6, "geckoterminal"))
    rows += [("s1", "smart", "GOOD", "buy", old, 100.0, 1.0, 150.0, 1e-6, "geckoterminal"),
             ("s2", "smart", "OTHER", "buy", old, 100.0, 1.0, 150.0, 1e-6, "geckoterminal"),
             ("n1", "fresh", "NEW", "buy", now - DAY, 10.0, 0.1, 15.0, 1e-6, "geckoterminal")]  # recent: stays
    c.executemany("INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    c.executemany("INSERT OR IGNORE INTO wallets(address) VALUES (?)", [(r[1],) for r in rows])
    c.execute("UPDATE wallets SET manual=1 WHERE address='one7'")     # a manual wallet row is never dropped
    c.executemany("INSERT INTO token_prices VALUES (?,?,?,?,?)",
                  [("GOOD", old + 60 * i, 1.0 + (i == 17) * 9, 0, 1000 - i) for i in range(120)]
                  + [("GOOD", now - 60 * i, 1.0, 0, 1000) for i in range(30)])
    c.commit()
    c.isolation_level = None
    return c, now, path


def test_cleanup_dry_run_counts_and_deletes_nothing(tmp_path):
    c, now, _ = _live(tmp_path)
    n = c.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    out = retention.cleanup(c, now=now)
    assert out["trades"] == 350 and 100 <= out["prices"] < 120
    assert c.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == n


def test_cleanup_keeps_what_scoring_and_training_read(tmp_path):
    c, now, _ = _live(tmp_path)
    out = retention.cleanup(c, now=now, delete=True, batch=64, pause_s=0)
    assert out["trades"] == 350                                  # old trades of one-coin pool-feed wallets
    left = {r[0] for r in c.execute("SELECT DISTINCT mint FROM trades")}
    assert left == {"GOOD", "OTHER", "NEW"}                      # the two-coin wallet's trades and recent ones stay
    assert [r[0] for r in c.execute("SELECT wallet FROM trades WHERE mint='GOOD'")] == ["smart"]
    assert c.execute("SELECT SUM(n) FROM trades_hourly WHERE mint='GOOD'").fetchone()[0] == 50
    agg = c.execute("SELECT SUM(n), SUM(usd), MIN(price_lo), MAX(price_hi) FROM trades_hourly WHERE mint='JUNK'").fetchone()
    assert agg[0] == 300 and abs(agg[1] - 4500) < 1e-6 and agg[2] == 1e-6 and abs(agg[3] - 5e-6) < 1e-12
    w = {r[0] for r in c.execute("SELECT address FROM wallets")}
    assert "one3" not in w and "fol3" not in w and "one7" in w and "smart" in w and "fresh" in w
    # prices: the 10x spike survives, old hours keep at most 4 rows each, recent rows untouched
    assert c.execute("SELECT MAX(price_usd) FROM token_prices WHERE mint='GOOD'").fetchone()[0] == 10.0
    assert c.execute("SELECT COUNT(*) FROM token_prices WHERE ts < ?", (now - 7 * DAY,)).fetchone()[0] <= 3 * 4
    assert c.execute("SELECT COUNT(*) FROM token_prices WHERE ts > ?", (now - 2 * DAY,)).fetchone()[0] == 30
    again = retention.cleanup(c, now=now, delete=True, pause_s=0)    # idempotent
    assert again["trades"] == 0 and again["prices"] == 0


def test_reclaim_shrinks_an_incremental_file(tmp_path):
    c, now, path = _live(tmp_path, auto_vacuum="INCREMENTAL")
    c.execute("CREATE TABLE pad(x)")
    c.executemany("INSERT INTO pad VALUES (?)", [("x" * 2000,) for _ in range(2000)])
    c.execute("DROP TABLE pad")
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    before = path.stat().st_size
    r = retention.reclaim(c, time.time() + 30, pages=200, pause_s=0)
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    assert r["auto_vacuum"] == "incremental" and r["freed_mb"] > 3 and path.stat().st_size < before / 2


def test_compact_switches_to_incremental(tmp_path):
    c, now, path = _live(tmp_path)
    c.close()
    msg = retention.compact(path)
    assert "verdichtet" in msg and "incremental" in msg
    c = sqlite3.connect(path)
    assert retention.file_stats(c)["auto_vacuum"] == "incremental"
    assert c.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 353
