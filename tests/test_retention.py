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
