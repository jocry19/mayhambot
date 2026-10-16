"""deploy/autoupdate.py against a throwaway git origin + checkout (no network, no launchd)."""
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import autoupdate  # noqa: E402
import ops  # noqa: E402

FAKE_MAYHAM = '''import subprocess, sys
from pathlib import Path
if sys.argv[1:2] == ["backup"]:
    sys.exit(1 if Path("BACKUP_FAILS").exists() else 0)
if sys.argv[1:2] == ["update"]:
    old = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    subprocess.check_call(["git", "pull", "-q", "--ff-only", "origin", "main"])
    if Path("RED").exists():
        subprocess.call(["git", "reset", "-q", "--hard", old])
        print("Update abgebrochen")
'''


def sh(cwd, *a):
    subprocess.run(a, cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    origin, work, dev = tmp_path / "origin.git", tmp_path / "work", tmp_path / "dev"
    sh(tmp_path, "git", "init", "-q", "--bare", "-b", "main", str(origin))
    sh(tmp_path, "git", "clone", "-q", str(origin), str(dev))
    for d in (dev,):
        sh(d, "git", "config", "user.email", "t@t")
        sh(d, "git", "config", "user.name", "t")
    (dev / "mayham.py").write_text(FAKE_MAYHAM)
    (dev / "deploy").mkdir()
    (dev / "deploy" / "ops.py").write_text(FAKE_MAYHAM.replace('sys.argv[1:2] == ["backup"]', 'True'))
    sh(dev, "git", "add", ".")
    sh(dev, "git", "commit", "-qm", "v1")
    sh(dev, "git", "push", "-q", "origin", "HEAD:main")
    sh(tmp_path, "git", "clone", "-q", str(origin), str(work))
    for m in (autoupdate, ops):
        monkeypatch.setattr(m, "ROOT", work)
    monkeypatch.setattr(ops, "ENV", {"AUTO_UPDATE_SETTLE_S": "0"})
    monkeypatch.setattr(ops, "db_path", lambda: work / "data" / "mayhambot.db")
    monkeypatch.setattr(ops, "check_processes", lambda: {"signals": True, "web": True})
    alerts = []

    def push(name, red=False):
        (dev / name).write_text("x")
        if red:
            (dev / "RED").write_text("x")
        sh(dev, "git", "add", ".")
        sh(dev, "git", "commit", "-qm", name)
        sh(dev, "git", "push", "-q", "origin", "HEAD:main")

    def head():
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=work, text=True).strip()

    return work, push, head, alerts


def test_deploys_new_commit_and_alerts(repo):
    work, push, head, alerts = repo
    assert autoupdate.run_once(alerts.append, health=lambda: (True, "2 von 2 Dienste laufen stabil")).startswith("aktuell")
    push("feature")
    assert autoupdate.run_once(alerts.append, health=lambda: (True, "2 von 2 Dienste laufen stabil")).startswith("eingespielt")
    assert (work / "feature").exists()
    assert "Update eingespielt" in alerts[-1] and "2 von 2" in alerts[-1]
    assert "deployed" in (work / "logs" / "update.log").read_text()


def test_rollback_is_reported_and_not_retried(repo):
    work, push, head, alerts = repo
    before = head()
    push("broken", red=True)
    assert autoupdate.run_once(alerts.append, health=lambda: (True, "2 von 2 Dienste laufen stabil"), now=1e9).startswith("zurückgerollt")
    assert head() == before and "zurückgerollt" in alerts[-1]
    assert "übersprungen" in autoupdate.run_once(alerts.append, health=lambda: (True, "2 von 2 Dienste laufen stabil"), now=2e9)


def test_live_trading_blocks_and_alerts_once(repo):
    work, push, head, alerts = repo
    (work / ".env").write_text("LIVE_TRADING=true\n")
    push("feature")
    before = head()
    autoupdate.run_once(alerts.append, health=lambda: (True, "2 von 2 Dienste laufen stabil"))
    autoupdate.run_once(alerts.append, health=lambda: (True, "2 von 2 Dienste laufen stabil"))
    assert head() == before
    assert len(alerts) == 1 and "wartet" in alerts[0]
    assert "update pending: live trading on" in (work / "logs" / "update.log").read_text()


def test_open_live_position_blocks(repo):
    work, push, head, alerts = repo
    (work / "data").mkdir()
    c = sqlite3.connect(work / "data" / "mayhambot.db")
    c.execute("CREATE TABLE ct_positions (mode TEXT, status TEXT)")
    c.execute("INSERT INTO ct_positions VALUES ('live', 'open')")
    c.commit()
    c.close()
    push("feature")
    before = head()
    assert "Echtgeld" in autoupdate.run_once(alerts.append, health=lambda: (True, "2 von 2 Dienste laufen stabil"))
    assert head() == before


def test_kill_switch_file_and_env(repo):
    work, push, head, alerts = repo
    push("feature")
    before = head()
    (work / "data").mkdir()
    (work / "data" / "NO_AUTO_UPDATE").write_text("")
    assert autoupdate.run_once(alerts.append, health=lambda: (True, "2 von 2 Dienste laufen stabil")).startswith("aus")
    (work / "data" / "NO_AUTO_UPDATE").unlink()
    (work / ".env").write_text("AUTO_UPDATE=0\n")
    assert autoupdate.run_once(alerts.append, health=lambda: (True, "2 von 2 Dienste laufen stabil")).startswith("aus")
    assert head() == before and not alerts


def test_one_deploy_per_interval(repo):
    work, push, head, alerts = repo
    push("a")
    assert autoupdate.run_once(alerts.append, health=lambda: (True, "2 von 2 Dienste laufen stabil"), now=1e9).startswith("eingespielt")
    push("b")
    assert autoupdate.run_once(alerts.append, health=lambda: (True, "2 von 2 Dienste laufen stabil"), now=1e9 + 60).startswith("warte")
    assert autoupdate.run_once(alerts.append, health=lambda: (True, "2 von 2 Dienste laufen stabil"), now=1e9 + 601).startswith("eingespielt")


def test_dirty_checkout_waits(repo):
    work, push, head, alerts = repo
    (work / "mayham.py").write_text(FAKE_MAYHAM + "\n# local edit\n")
    push("feature")
    before = head()
    assert "lokale Änderungen" in autoupdate.run_once(alerts.append, health=lambda: (True, "2 von 2 Dienste laufen stabil"))
    assert head() == before


def test_index_only_problems_are_repairable(tmp_path):
    assert ops._index_only(["row 5 missing from index idx_trades_wallet_ts",
                            "wrong # of entries in index sqlite_autoindex_trades_1"])
    assert not ops._index_only(["Page 12: btreeInitPage() returns error code 11"])
    assert not ops._index_only([])
    db = tmp_path / "x.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE t (a, b)")
    c.execute("CREATE INDEX i ON t(a)")
    c.executemany("INSERT INTO t VALUES (?,?)", [(i, i) for i in range(100)])
    c.commit()
    c.close()
    assert ops._repair_indexes(db) == []


def test_failed_backup_blocks_deploy(repo):
    work, push, head, alerts = repo
    (work / "BACKUP_FAILS").write_text("")
    push("feature")
    before = head()
    assert "Backup" in autoupdate.run_once(alerts.append, health=lambda: (True, "2 von 2 Dienste laufen stabil"))
    autoupdate.run_once(alerts.append, health=lambda: (True, "2 von 2 Dienste laufen stabil"))
    assert head() == before and len(alerts) == 1
    (work / "BACKUP_FAILS").unlink()
    assert autoupdate.run_once(alerts.append, health=lambda: (True, "2 von 2 Dienste laufen stabil")).startswith("eingespielt")


def test_unhealthy_deploy_rolls_back(repo):
    work, push, head, alerts = repo
    before = head()
    push("crashy")
    calls = []

    def health():
        calls.append(1)
        return (False, "signals läuft nicht") if len(calls) == 1 else (True, "ok")
    assert autoupdate.run_once(alerts.append, health=health).startswith("zurückgerollt")
    assert head() == before and "signals läuft nicht" in alerts[-1]
    assert "übersprungen" in autoupdate.run_once(alerts.append, now=9e9)


def test_health_check_detects_crash_loop(repo, monkeypatch):
    work, push, head, alerts = repo
    monkeypatch.setattr(autoupdate, "_services_now", lambda: ["a", "b"])
    monkeypatch.setattr(ops, "check_dashboard", lambda: True)
    seq = iter([{"a": 1, "b": 2}, {"a": 1, "b": 3}, {"a": 1, "b": 4}])
    monkeypatch.setattr(autoupdate, "_pids", lambda names: next(seq))
    ok, detail = autoupdate.health_check(window=0.02, interval=0.01)
    assert not ok and "b" in detail
    monkeypatch.setattr(autoupdate, "_pids", lambda names: {"a": 1, "b": 2})
    assert autoupdate.health_check(window=0.02, interval=0.005)[0]


def test_lock_count_reads_only_new_log_lines(repo):
    work, push, head, alerts = repo
    (work / "logs").mkdir(exist_ok=True)
    f = work / "logs" / "signals.log"
    f.write_text("old database is locked\n")
    sizes = autoupdate._log_sizes()
    f.write_text(f.read_text() + "x database is locked\ny database is locked\n")
    assert autoupdate._lock_count(sizes) == 2


def test_watchdog_counts_new_lock_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(ops, "ROOT", tmp_path)
    (tmp_path / "logs").mkdir()
    f = tmp_path / "logs" / "copytrade.log"
    f.write_text("database is locked\n")
    st = {}
    assert ops.new_lock_errors(st) == 0          # history before the first run is not counted
    f.write_text(f.read_text() + "database is locked\n" * 3)
    assert ops.new_lock_errors(st) == 3
    f.write_text("database is locked\n")         # rotated (copytruncate)
    assert ops.new_lock_errors(st) == 1


def test_push_during_deploy_counts_as_success(repo, monkeypatch):
    work, push, head, alerts = repo
    push("a")
    real = autoupdate.subprocess.run

    def run(cmd, *a, **k):
        if cmd[-1:] == ["update"]:
            push("b")              # someone pushes while the update runs; the pull takes the newer head
        return real(cmd, *a, **k)
    monkeypatch.setattr(autoupdate.subprocess, "run", run)
    res = autoupdate.run_once(alerts.append, health=lambda: (True, "ok"))
    assert res.startswith("eingespielt") and (work / "b").exists()
    assert "b" in alerts[-1] and "zurückgerollt" not in alerts[-1]


def test_service_budget_alerts_once_per_day(monkeypatch):
    import datetime as dt
    sent = []
    monkeypatch.setattr(ops, "send_alert", lambda t: sent.append(t) or True)
    st = {}
    noon = dt.datetime(2026, 10, 16, 12, tzinfo=dt.timezone.utc)
    c = {"per_service": {"safety": (3000, 3750), "signals": (500, 3750), "web": (0, None)}}
    ops.check_service_budgets(st, c, noon)          # safety on pace for 6000 > 1.5 x 3750
    ops.check_service_budgets(st, c, noon)
    assert len(sent) == 1 and "safety" in sent[0] and "Hochrechnung" in sent[0]
    c["per_service"]["safety"] = (4000, 3750)
    ops.check_service_budgets(st, c, noon)
    assert len(sent) == 2 and "aufgebraucht" in sent[1]
