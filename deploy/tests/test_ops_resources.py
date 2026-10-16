import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ops  # noqa: E402


def _setup(tmp_path, monkeypatch, env=None):
    monkeypatch.setattr(ops, "ROOT", tmp_path)
    monkeypatch.setattr(ops, "ENV", dict(env or {}))
    monkeypatch.setattr(ops, "db_path", lambda: tmp_path / "data" / "mayhambot.db")
    (tmp_path / "data" / "pids").mkdir(parents=True)
    (tmp_path / "logs").mkdir()


def test_memory_thresholds(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    for n, pid in (("signals", 11), ("web", 12)):
        (tmp_path / "data" / "pids" / f"{n}.pid").write_text(str(pid))
    rss = {11: 1800.0, 12: 200.0}
    monkeypatch.setattr(ops, "_rss_mb", lambda pid: rss[pid])
    p = ops.check_resources({"signals": True, "web": True})
    assert "mem_high" in p and "signals 1800 MB" in p["mem_high"] and "mem_runaway" not in p
    rss[11] = 3500.0
    p = ops.check_resources({"signals": True, "web": True})
    assert "mem_runaway" in p and "mem_runaway" not in ops.check_resources({"signals": False, "web": True})


def test_hung_service_and_big_wal(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, {"WAL_WARN_MB": "0.001"})
    monkeypatch.setattr(ops, "_rss_mb", lambda pid: None)
    log = tmp_path / "logs" / "signals.log"
    log.write_text("x")
    now = time.time()
    assert "service_hung" not in ops.check_resources({"signals": True}, now=now)
    assert "signals seit 35 min" in ops.check_resources({"signals": True}, now=now + 35 * 60)["service_hung"]
    (tmp_path / "data" / "mayhambot.db-wal").write_bytes(b"x" * 5000)
    assert "wal_big" in ops.check_resources({"signals": True}, now=now)


def test_nightly_maintenance_once_per_day(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, {"MAINT_HOUR": "4"})
    c = sqlite3.connect(tmp_path / "data" / "mayhambot.db")
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("CREATE TABLE t(x)")
    c.execute("INSERT INTO t VALUES (1)")
    c.commit()
    c.close()
    four = time.mktime((2026, 10, 17, 4, 5, 0, 0, 0, -1))
    st = {}
    assert ops.nightly_maintenance(st, now=four - 3600) is None          # 03:05: not the hour
    assert "Wartung 2026-10-17" in ops.nightly_maintenance(st, now=four)
    assert ops.nightly_maintenance(st, now=four + 600) is None           # once per day


def test_ops_report_shows_update_state(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(ops, "build_summary", lambda: "📊 <b>Stündlicher Bericht</b>\n🟢 Dienste 9/9 laufen")
    monkeypatch.setattr(ops, "last_backup", lambda: None)
    (tmp_path / "data" / "autoupdate_state.json").write_text('{"red_streak": 2}')
    (tmp_path / "logs" / "update.log").write_text("2026-10-16 20:59:33 deployed ff8ecfc x: 9 von 9 Dienste laufen stabil\n")
    r = ops.ops_report()
    assert "Dienste 9/9" in r and "Tests seit 2 Runde(n) rot" in r and "deployed ff8ecfc" in r and "Backup: keins" in r
