"""deploy/autoupdate.py against a throwaway git origin + checkout (no network, no launchd)."""
import json
import time
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
    elif Path("PIP_FAILS").exists():
        sys.exit(1)
    else:
        print("Update ok: x")
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
    assert head() == before and "nicht eingespielt" in alerts[-1] and len(alerts) == 1
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


class FakeClock:
    """health_check only sees this clock: no real sleeping, so a throttled launchd job (ProcessType Background
    on the Mac stretches short sleeps a lot) cannot change the outcome."""
    def __init__(self, t=1_000_000.0):
        self.t = t

    def time(self):
        return self.t

    def sleep(self, d):
        self.t += d


def test_health_check_detects_crash_loop(repo, monkeypatch):
    work, push, head, alerts = repo
    clock = FakeClock()
    monkeypatch.setattr(autoupdate, "time", clock)
    monkeypatch.setattr(autoupdate, "_services_now", lambda: ["a", "b"])
    monkeypatch.setattr(ops, "check_dashboard", lambda: True)
    n = []
    monkeypatch.setattr(autoupdate, "_pids", lambda names: {"a": 1, "b": len(n.append(1) or n)})
    ok, detail = autoupdate.health_check(window=180, interval=15)
    assert not ok and "b" in detail
    monkeypatch.setattr(autoupdate, "_pids", lambda names: {"a": 1, "b": 2})
    assert autoupdate.health_check(window=180, interval=15)[0]


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


def test_update_crash_after_pull_resets_checkout(repo):
    work, push, head, alerts = repo
    before = head()
    push("needs_pip")
    (work / "PIP_FAILS").write_text("")
    assert autoupdate.run_once(alerts.append, health=lambda: (True, "ok")).startswith("zurückgerollt")
    assert head() == before and not (work / "needs_pip").exists()


def test_red_streak_names_failing_tests_once(repo, monkeypatch):
    work, push, head, alerts = repo
    monkeypatch.setattr(autoupdate, "_failing_tests", lambda rev: ["FAILED web/tests/test_api.py::test_x"])
    for i in range(4):
        push(f"red{i}", red=True)
        autoupdate.run_once(alerts.append, now=1e9 + i * 1000, health=lambda: (True, "ok"))
    hangs = [a for a in alerts if "hängt" in a]
    assert len(hangs) == 1 and "test_x" in hangs[0]
    assert len([a for a in alerts if "nicht eingespielt" in a]) == 1     # only the first red round alerts
    assert autoupdate._load()["red_streak"] == 4


def test_local_commit_blocks_and_alerts(repo):
    work, push, head, alerts = repo
    sh(work, "git", "config", "user.email", "t@t")
    sh(work, "git", "config", "user.name", "t")
    (work / "local.txt").write_text("x")
    sh(work, "git", "add", ".")
    sh(work, "git", "commit", "-qm", "local filter")
    push("feature")
    before = head()
    res = autoupdate.run_once(alerts.append, health=lambda: (True, "ok"))
    assert "blockiert" in res and "local filter" in res and "1 Commit(s) von main fehlen" in res
    autoupdate.run_once(alerts.append, health=lambda: (True, "ok"))
    assert head() == before and len(alerts) == 1
    assert "blockiert" in autoupdate._load()["blocked"]


def _plan(root, service, ts, reason="model v7 active since start (was v6), 5 fallbacks this cycle"):
    (root / "data").mkdir(exist_ok=True)
    with open(root / "data" / "planned_restarts.jsonl", "a") as f:
        f.write(json.dumps({"ts": ts, "service": service}) + "\n")
    (root / "logs").mkdir(exist_ok=True)
    with open(root / "logs" / f"{service}.log", "a") as f:
        f.write(f"2026-10-16 ERROR restarting the {service} service: {reason}\n")


def test_planned_restart_is_no_crash(repo, monkeypatch):
    work, push, head, alerts = repo
    clock = FakeClock()
    monkeypatch.setattr(autoupdate, "time", clock)
    monkeypatch.setattr(autoupdate, "_services_now", lambda: ["a", "b"])
    monkeypatch.setattr(ops, "check_dashboard", lambda: True)
    # signals-like service b exits with code 3 at 165 s, late in the 180 s window, and is back 15 s later
    _plan(work, "b", clock.t + 165)
    real_planned = ops.planned_restarts
    monkeypatch.setattr(ops, "planned_restarts", lambda since=0.0: [e for e in real_planned(since) if e["ts"] <= clock.t])
    start = clock.t

    def pids(names):
        dt = clock.t - start
        return {"a": 1, "b": 2 if dt < 165 else (None if dt < 180 else 3)}
    monkeypatch.setattr(autoupdate, "_pids", pids)
    ok, detail = autoupdate.health_check(window=180, interval=15)
    assert ok and "b: geplanter Neustart nach Modellwechsel" in detail, detail
    # planned restarts every 30 s are a restart loop, not one model switch
    clock.t, start = 2_000_000.0, 2_000_000.0
    for k in range(1, 10):
        _plan(work, "b", clock.t + 30 * k)
    monkeypatch.setattr(autoupdate, "_pids", lambda names: {"a": 1, "b": 100 + int((clock.t - start) // 30)})
    ok, detail = autoupdate.health_check(window=180, interval=15)
    assert not ok and "b startet neu" in detail


def test_watchdog_reports_planned_restart_once(tmp_path, monkeypatch):
    monkeypatch.setattr(ops, "ROOT", tmp_path)
    sent = []
    monkeypatch.setattr(ops, "send_alert", lambda m: sent.append(m) or True)
    _plan(tmp_path, "signals", 1000.0)
    st = {}
    ops.check_planned_restarts(st)
    ops.check_planned_restarts(st)
    assert len(sent) == 1 and "signals: geplanter Neustart nach Modellwechsel" in sent[0]
    assert "Kein Fehler" in sent[0]


def test_setup_telegram_skips_get_updates_when_chat_id_set(monkeypatch):
    import types
    monkeypatch.setattr(ops, "ENV", {"TELEGRAM_BOT_TOKEN": "x", "TELEGRAM_CHAT_ID": "1"})
    monkeypatch.setattr(ops.urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("polled")))
    sent = []
    monkeypatch.setattr(ops, "send_alert", lambda m: sent.append(m) or True)
    assert ops.setup_telegram(types.SimpleNamespace(neu=False)) == 0 and len(sent) == 1


def test_send_alert_rereads_env_for_late_chat_id(tmp_path, monkeypatch):
    monkeypatch.setattr(ops, "ROOT", tmp_path)
    (tmp_path / ".env").write_text("TELEGRAM_BOT_TOKEN=t\nTELEGRAM_CHAT_ID=42\n")
    seen = []

    class Resp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(ops.urllib.request, "urlopen", lambda url, data, timeout: seen.append(data) or Resp())
    monkeypatch.setitem(sys.modules, "signals.notifier", None)    # force the stdlib path
    assert ops.send_alert("x") and seen and b"chat_id=42" in seen[0]


def test_interrupted_round_is_finished_next_time(repo):
    work, push, head, alerts = repo
    before = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=work, text=True).strip()
    push("new")
    sh(work, "git", "pull", "-q", "--ff-only", "origin", "main")    # the dead round got this far
    autoupdate._save({"deploying": {"head": before, "target": "x", "at": 1.0}})
    assert "nachgeholt" in autoupdate.run_once(alerts.append, health=lambda: (True, "2 von 2 Dienste laufen stabil"))
    st = autoupdate._load()
    assert "deploying" not in st and st["deployed"].startswith(head()) and "nachgeholt" in alerts[-1]
    # unhealthy: back to the old commit
    push("newer")
    sh(work, "git", "pull", "-q", "--ff-only", "origin", "main")
    mid = subprocess.check_output(["git", "rev-parse", "HEAD~1"], cwd=work, text=True).strip()
    autoupdate._save({"deploying": {"head": mid, "target": "x", "at": 1.0}})
    assert autoupdate.run_once(alerts.append, health=lambda: (False, "b läuft nicht")).startswith("zurückgerollt")
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=work, text=True).strip() == mid
