import types
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
    spawned = []
    monkeypatch.setattr(ops, "_spawn_retention", lambda: spawned.append(1))
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
    assert spawned == [1]


def test_ops_report_shows_update_state(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(ops, "build_summary", lambda: "📊 <b>Stündlicher Bericht</b>\n🟢 Dienste 9/9 laufen")
    monkeypatch.setattr(ops, "last_backup", lambda: None)
    (tmp_path / "data" / "autoupdate_state.json").write_text('{"red_streak": 2}')
    (tmp_path / "logs" / "update.log").write_text("2026-10-16 20:59:33 deployed ff8ecfc x: 9 von 9 Dienste laufen stabil\n")
    r = ops.ops_report()
    assert "Dienste 9/9" in r and "Tests seit 2 Runde(n) rot" in r and "deployed ff8ecfc" in r and "Backup: keins" in r


def test_watchdog_caps_restarts_per_hour(tmp_path, monkeypatch):
    import types
    _setup(tmp_path, monkeypatch)
    state, sent, restarts = {}, [], []
    clock = [1_000_000.0]
    monkeypatch.setattr(ops.time, "time", lambda: clock[0])
    monkeypatch.setattr(ops, "load_state", lambda: state)
    monkeypatch.setattr(ops, "save_state", lambda s: None)
    monkeypatch.setattr(ops, "send_alert", lambda m: sent.append(m) or True)
    monkeypatch.setattr(ops, "restart_app", lambda why: restarts.append(why) or "App neu gestartet")
    monkeypatch.setattr(ops, "rotate_logs", lambda **k: 0)
    monkeypatch.setattr(ops, "new_lock_errors", lambda st: 0)
    monkeypatch.setattr(ops, "collect", lambda: {"problems": {"service_hung": "signals hängt"}})
    monkeypatch.setattr(ops, "build_summary", lambda: "x")
    for name in ("nightly_maintenance", "check_planned_restarts", "check_credit_alerts", "check_service_budgets"):
        monkeypatch.setattr(ops, name, lambda *a, **k: None)
    monkeypatch.setattr(ops, "RESTART_COOLDOWN", 0)
    state["last_summary"] = 2e9
    args = types.SimpleNamespace(no_restart=False)
    for _ in range(12):                     # 12 rounds of 5 min = one hour of a hang that a restart cannot fix
        ops.watchdog(args)
        clock[0] += 300
    assert len(restarts) == 3
    assert sum("Neustarts in der letzten Stunde" in m for m in sent) == 1


def test_watchdog_leaves_restarts_to_a_running_update(tmp_path, monkeypatch):
    """Mid-round the updater restarts the app itself; a watchdog restart then would race it."""
    import json as _json
    import types
    _setup(tmp_path, monkeypatch)
    state, restarts = {"last_summary": 2e9}, []
    clock = [1_000_000.0]
    monkeypatch.setattr(ops.time, "time", lambda: clock[0])
    monkeypatch.setattr(ops, "load_state", lambda: state)
    monkeypatch.setattr(ops, "save_state", lambda s: None)
    monkeypatch.setattr(ops, "send_alert", lambda m: True)
    monkeypatch.setattr(ops, "restart_app", lambda why: restarts.append(why) or "App neu gestartet")
    monkeypatch.setattr(ops, "rotate_logs", lambda **k: 0)
    monkeypatch.setattr(ops, "new_lock_errors", lambda st: 0)
    monkeypatch.setattr(ops, "collect", lambda: {"problems": {"service_down": "brain läuft nicht"}})
    monkeypatch.setattr(ops, "build_summary", lambda: "x")
    for name in ("nightly_maintenance", "check_planned_restarts", "check_credit_alerts", "check_service_budgets"):
        monkeypatch.setattr(ops, name, lambda *a, **k: None)
    monkeypatch.setattr(ops, "RESTART_COOLDOWN", 0)
    f = ops.ROOT / "data" / "autoupdate_state.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(_json.dumps({"deploying": {"head": "a", "target": "b", "at": clock[0] - 60}}))
    args = types.SimpleNamespace(no_restart=False)
    for _ in range(3):
        ops.watchdog(args)
        clock[0] += 120
    assert restarts == []
    f.write_text(_json.dumps({}))           # round over: a service still down is the watchdog's again
    ops.watchdog(args)
    assert len(restarts) == 1


def test_proc_age_parses_ps_etime(monkeypatch):
    for etime, secs in (("05:02", 302), ("01:02:03", 3723), ("2-00:00:10", 172810)):
        monkeypatch.setattr(ops.subprocess, "run", lambda *a, _e=etime, **k: types.SimpleNamespace(stdout=_e + "\n"))
        assert ops._proc_age_s(1) == secs


def test_backup_logs_table_sizes(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, {"BACKUP_DIR": str(tmp_path / "bk")})
    c = sqlite3.connect(tmp_path / "data" / "mayhambot.db")
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("CREATE TABLE trades(sig TEXT, wallet TEXT, mint TEXT, ts INTEGER, source TEXT)")
    c.executemany("INSERT INTO trades VALUES (?,?,?,?,?)",
                  [(str(i), f"w{i % 7}", f"m{i % 3}", 1_760_000_000 + i, "geckoterminal") for i in range(100)])
    c.commit()
    c.close()
    monkeypatch.setattr(ops, "backup_dir", lambda: tmp_path / "bk")
    assert ops.backup(None) == 0
    log = (tmp_path / "logs" / "dbsize.log").read_text()
    assert "trades:" in log and "100 Zeilen" in log and "Einmal-Wallets" in log
    assert (tmp_path / "data" / "db_sizes.json").exists()


def test_retention_counts_only_until_switched_on(tmp_path, monkeypatch):
    import argparse
    _setup(tmp_path, monkeypatch)
    from core import db
    p = tmp_path / "data" / "mayhambot.db"
    c = db.connect(str(p))
    old = int(time.time()) - 30 * 86400
    c.executemany("INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?,?)",
                  [(f"s{i}", f"one{i}", "JUNK", "buy", old + i, 1, 1, 1, 1, "geckoterminal") for i in range(10)])
    c.commit()
    c.close()
    assert ops.retention_run(argparse.Namespace(db=str(p))) == 0
    c = sqlite3.connect(p)
    assert c.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 10
    c.close()
    monkeypatch.setattr(ops, "ENV", {"RETENTION_DELETE": "1"})
    assert ops.retention_run(argparse.Namespace(db=str(p))) == 0
    c = sqlite3.connect(p)
    assert c.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 0
    assert c.execute("SELECT SUM(n) FROM trades_hourly").fetchone()[0] == 10
