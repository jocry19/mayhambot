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
if sys.argv[1] == "update":
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
    assert autoupdate.run_once(alerts.append).startswith("aktuell")
    push("feature")
    assert autoupdate.run_once(alerts.append).startswith("eingespielt")
    assert (work / "feature").exists()
    assert "Update eingespielt" in alerts[-1] and "2 von 2" in alerts[-1]
    assert "deployed" in (work / "logs" / "update.log").read_text()


def test_rollback_is_reported_and_not_retried(repo):
    work, push, head, alerts = repo
    before = head()
    push("broken", red=True)
    assert autoupdate.run_once(alerts.append, now=1e9).startswith("zurückgerollt")
    assert head() == before and "zurückgerollt" in alerts[-1]
    assert "übersprungen" in autoupdate.run_once(alerts.append, now=2e9)


def test_live_trading_blocks_and_alerts_once(repo):
    work, push, head, alerts = repo
    (work / ".env").write_text("LIVE_TRADING=true\n")
    push("feature")
    before = head()
    autoupdate.run_once(alerts.append)
    autoupdate.run_once(alerts.append)
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
    assert "Echtgeld" in autoupdate.run_once(alerts.append)
    assert head() == before


def test_kill_switch_file_and_env(repo):
    work, push, head, alerts = repo
    push("feature")
    before = head()
    (work / "data").mkdir()
    (work / "data" / "NO_AUTO_UPDATE").write_text("")
    assert autoupdate.run_once(alerts.append).startswith("aus")
    (work / "data" / "NO_AUTO_UPDATE").unlink()
    (work / ".env").write_text("AUTO_UPDATE=0\n")
    assert autoupdate.run_once(alerts.append).startswith("aus")
    assert head() == before and not alerts


def test_one_deploy_per_interval(repo):
    work, push, head, alerts = repo
    push("a")
    assert autoupdate.run_once(alerts.append, now=1e9).startswith("eingespielt")
    push("b")
    assert autoupdate.run_once(alerts.append, now=1e9 + 60).startswith("warte")
    assert autoupdate.run_once(alerts.append, now=1e9 + 601).startswith("eingespielt")


def test_dirty_checkout_waits(repo):
    work, push, head, alerts = repo
    (work / "mayham.py").write_text(FAKE_MAYHAM + "\n# local edit\n")
    push("feature")
    before = head()
    assert "lokale Änderungen" in autoupdate.run_once(alerts.append)
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
