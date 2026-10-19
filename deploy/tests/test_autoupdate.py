"""deploy/autoupdate.py against a throwaway git origin + checkout (no network, no launchd)."""
import os
import json
import time
import types
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
    monkeypatch.setattr(ops, "ENV", {"AUTO_UPDATE_SETTLE_S": "0", "BACKUP_DIR": str(tmp_path / "backups")})
    monkeypatch.setattr(ops, "db_path", lambda: work / "data" / "mayhambot.db")
    monkeypatch.setattr(ops, "check_processes", lambda: {"signals": True, "web": True})
    alerts = []

    def push(name, red=False, msg=None):
        (dev / name).write_text("x")
        if red:
            (dev / "RED").write_text("x")
        sh(dev, "git", "add", ".")
        sh(dev, "git", "commit", "-qm", msg or name)
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
    ok = lambda: (True, "2 von 2 Dienste laufen stabil")   # noqa: E731
    assert autoupdate.run_once(alerts.append, health=ok, now=1e9 + 60).startswith("warte")
    assert autoupdate.run_once(alerts.append, health=ok, now=1e9 + 601).startswith("warte")   # bundled: 30 min
    push("c")
    assert autoupdate.run_once(alerts.append, health=ok, now=1e9 + 1801).startswith("eingespielt")   # b and c at once


def test_urgent_commit_skips_the_bundling(repo):
    work, push, head, alerts = repo
    ok = lambda: (True, "2 von 2 Dienste laufen stabil")   # noqa: E731
    push("a")
    assert autoupdate.run_once(alerts.append, health=ok, now=1e9).startswith("eingespielt")
    push("b", msg="safety: [sofort] rug check back on")
    assert autoupdate.run_once(alerts.append, health=ok, now=1e9 + 300).startswith("warte")
    assert autoupdate.run_once(alerts.append, health=ok, now=1e9 + 601).startswith("eingespielt")


def test_deploy_waits_for_a_running_retrain_unless_urgent(repo):
    work, push, head, alerts = repo
    ok = lambda: (True, "2 von 2 Dienste laufen stabil")   # noqa: E731
    marker = ops.db_path().parent / "TRAINING"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("1")
    push("a")
    assert autoupdate.run_once(alerts.append, health=ok, now=1e9) == "warte (Wallet-Modell trainiert gerade)"
    old = time.time() - 91 * 60          # a marker the dead process left behind does not block forever
    os.utime(marker, (old, old))
    assert autoupdate.run_once(alerts.append, health=ok, now=1e9).startswith("eingespielt")
    marker.write_text("1")
    push("b", msg="hotfix: copytrade exit")
    assert autoupdate.run_once(alerts.append, health=ok, now=1e9 + 601).startswith("eingespielt")


def test_deploy_marks_itself_while_the_gate_runs(repo, monkeypatch):
    work, push, head, alerts = repo
    seen = []
    real = autoupdate._run_group

    def run(*a, **k):
        seen.append((ops.db_path().parent / "DEPLOYING").exists())   # the research service starts no retrain now
        return real(*a, **k)
    monkeypatch.setattr(autoupdate, "_run_group", run)
    push("a")
    assert autoupdate.run_once(alerts.append, health=lambda: (True, "2 von 2 Dienste laufen stabil"),
                               now=1e9).startswith("eingespielt")
    assert seen == [True] and not (ops.db_path().parent / "DEPLOYING").exists()


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
    assert ops._broken_indexes(["row 5 missing from index idx_trades_wallet_ts",
                                "row 9 missing from index idx_trades_wallet_ts",
                                "wrong # of entries in index sqlite_autoindex_trades_1"]) == [
        "idx_trades_wallet_ts", "sqlite_autoindex_trades_1"]
    stmts = []
    real = sqlite3.connect

    def spy(*a, **k):
        c = real(*a, **k)
        c.set_trace_callback(stmts.append)
        return c
    import unittest.mock
    with unittest.mock.patch.object(ops.sqlite3, "connect", spy):
        assert ops._repair_indexes(db, names=["i"], check=False) == []
    assert 'REINDEX "i"' in stmts and "REINDEX" not in stmts      # named only, never the whole live database


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
    real = autoupdate._run_group

    def run(cmd, *a, **k):
        if cmd[-1:] == ["update"]:
            push("b")              # someone pushes while the update runs; the pull takes the newer head
        return real(cmd, *a, **k)
    monkeypatch.setattr(autoupdate, "_run_group", run)
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


def test_gate_env_drops_env_file_keys_and_secrets(repo, monkeypatch):
    work, push, head, alerts = repo
    (work / ".env").write_text("HELIUS_API_KEY=k\nMY_KEY=v\n# C=1\n")
    monkeypatch.setenv("MY_KEY", "v")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    monkeypatch.setenv("PATH_LIKE", "keep")
    env = autoupdate._gate_env()
    assert "MY_KEY" not in env and "TELEGRAM_CHAT_ID" not in env and env["PATH_LIKE"] == "keep"
    assert env["MAYHAM_SKIP_BACKUP"] == "1"


def test_live_trading_in_file_wins_over_stale_environment(repo, monkeypatch):
    work, push, head, alerts = repo
    monkeypatch.setenv("LIVE_TRADING", "false")      # left behind by an earlier dotenv import
    (work / ".env").write_text("LIVE_TRADING=true\n")
    assert autoupdate.live_reason(ops.load_env())


def test_prune_backups_caps_total_size(tmp_path, monkeypatch):
    monkeypatch.setattr(ops, "ENV", {"BACKUP_MAX_GB": "0.000003"})   # 3 kB
    now = time.time()
    for h in range(6):                                                 # 6 hourly backups of 1 kB, all young
        f = tmp_path / f"mayhambot-20261016-{20 - h:02d}0000.tar.gz"
        f.write_bytes(b"x" * 1000)
        os.utime(f, (now - h * 3600, now - h * 3600))
    assert ops.prune_backups(tmp_path) == 3
    assert sorted(p.name for p in tmp_path.iterdir())[-1] == "mayhambot-20261016-200000.tar.gz"
    assert len(list(tmp_path.iterdir())) == 3


def test_recent_hourly_backup_is_reused(repo, tmp_path):
    work, push, head, alerts = repo
    (tmp_path / "backups").mkdir()
    f = tmp_path / "backups" / "mayhambot-20261016-190000.tar.gz"
    f.write_bytes(b"x")
    old = time.time() - 70 * 60   # the hourly job's archive is often a little over an hour old
    os.utime(f, (old, old))
    (work / "BACKUP_FAILS").write_text("")          # the updater's own backup would fail: it must not run
    push("feature")
    assert autoupdate.run_once(alerts.append, health=lambda: (True, "2 von 2 Dienste laufen stabil")).startswith("eingespielt")
    assert "reused" in (work / "logs" / "update.log").read_text()


def test_run_group_kills_the_whole_tree_on_timeout(repo, tmp_path):
    marker = tmp_path / "child_alive"
    child = tmp_path / "child.py"
    child.write_text(f"import time\ntime.sleep(3)\nopen({str(marker)!r}, 'w')\n")
    parent = tmp_path / "parent.py"
    parent.write_text(f"import subprocess, sys, time\nsubprocess.Popen([sys.executable, {str(child)!r}])\n"
                      "time.sleep(30)\n")
    r = autoupdate._run_group([sys.executable, str(parent)], 0.5)
    assert r.returncode == -9 and "Zeitüberschreitung" in r.stderr
    import time as _t
    _t.sleep(3.5)
    assert not marker.exists()          # the grandchild died with the group


def test_run_group_kills_leftovers_after_a_normal_exit(repo, tmp_path):
    """A process a test left behind (output redirected, so the pipes close) dies with the round."""
    marker = tmp_path / "leftover_alive"
    child = tmp_path / "child.py"
    child.write_text(f"import time\ntime.sleep(1)\nopen({str(marker)!r}, 'w')\n")
    parent = tmp_path / "parent.py"
    parent.write_text(f"import subprocess, sys\nsubprocess.Popen([sys.executable, {str(child)!r}], "
                      "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n")
    r = autoupdate._run_group([sys.executable, str(parent)], 30)
    assert r.returncode == 0
    import time as _t
    _t.sleep(1.5)
    assert not marker.exists()


def test_prune_backups_thins_hours_before_days(tmp_path, monkeypatch):
    """A growing database must not cost the daily history: hours beyond 6 h are thinned to one per 4 h first."""
    monkeypatch.setattr(ops, "ENV", {})
    monkeypatch.setattr(ops.shutil, "disk_usage", lambda p: types.SimpleNamespace(free=10**12))
    now = time.time()
    for h in range(0, 24 * 5, 1):                                     # 5 days of hourly backups
        t = now - h * 3600 - 60
        f = tmp_path / ("mayhambot-" + time.strftime("%Y%m%d-%H%M%S", time.localtime(t)) + ".tar.gz")
        f.write_bytes(b"x")
        os.utime(f, (t, t))
    ops.prune_backups(tmp_path)
    ages = sorted((now - p.stat().st_mtime) / 3600 for p in tmp_path.iterdir())
    assert sum(a < 6 for a in ages) == 6
    assert 4 <= sum(6 <= a < 24 for a in ages) <= 6     # 18 h touch at most 6 fixed 4-h windows
    days = {p.name[10:18] for p in tmp_path.iterdir() if now - p.stat().st_mtime >= 86400}
    assert len(days) >= 4                                               # one per older day survives


def test_prune_backups_leaves_free_disk(tmp_path, monkeypatch):
    monkeypatch.setattr(ops, "ENV", {"BACKUP_MIN_FREE_GB": "0.000002"})   # keep 2 kB free
    monkeypatch.setattr(ops.shutil, "disk_usage", lambda p: types.SimpleNamespace(free=0))
    now = time.time()
    for h in range(5):
        f = tmp_path / f"mayhambot-20261016-{20 - h:02d}0000.tar.gz"
        f.write_bytes(b"x" * 1000)
        os.utime(f, (now - h * 3600, now - h * 3600))
    ops.prune_backups(tmp_path)
    assert len(list(tmp_path.iterdir())) == 3                           # 5 kB kept - 2 kB reserve, newest 3 stay


def test_prune_backups_hourly_over_days_keeps_dailies(tmp_path, monkeypatch):
    """Run pruning after every hourly backup for 4 days: the tiers must hold still, not rotate everything away."""
    monkeypatch.setattr(ops, "ENV", {})
    monkeypatch.setattr(ops.shutil, "disk_usage", lambda p: types.SimpleNamespace(free=10**12))
    clock = [1_760_000_000.0]
    monkeypatch.setattr(ops.time, "time", lambda: clock[0])
    for _ in range(24 * 4):
        t = clock[0]
        f = tmp_path / ("mayhambot-" + time.strftime("%Y%m%d-%H%M%S", time.localtime(t)) + ".tar.gz")
        f.write_bytes(b"x")
        os.utime(f, (t, t))
        ops.prune_backups(tmp_path)
        clock[0] += 3600
    clock[0] -= 3600
    ages = sorted((clock[0] - p.stat().st_mtime) / 3600 for p in tmp_path.iterdir())
    assert sum(a < 6 for a in ages) == 6
    assert 4 <= sum(6 <= a < 24 for a in ages) <= 6     # 18 h touch at most 6 fixed 4-h windows
    assert sum(a >= 24 for a in ages) >= 3          # one per earlier day survives


def test_health_check_starts_after_the_startup_vacuum(repo, monkeypatch):
    """Services start only after the VACUUM; down while it runs must not roll back a good commit."""
    work, push, head, alerts = repo
    clock = FakeClock()
    monkeypatch.setattr(autoupdate, "time", clock)
    monkeypatch.setattr(autoupdate, "_services_now", lambda: ["a"])
    monkeypatch.setattr(ops, "check_dashboard", lambda: True)
    done_at = clock.t + 600                                   # a 10-min VACUUM
    monkeypatch.setattr(ops, "compacting", lambda now=None: clock.t < done_at)
    monkeypatch.setattr(autoupdate, "_pids", lambda names: {"a": 1 if clock.t >= done_at else None})
    assert autoupdate.health_check(window=180, interval=15)[0]


def test_hot_inserts_name_their_columns():
    """A rollback after a migration that added a column must not break inserts: 'INSERT INTO t VALUES (...)'
    counts columns, 'INSERT INTO t (a, b) VALUES' does not."""
    import re as _re
    root = Path(__file__).resolve().parents[2]
    bare = _re.compile(r"INSERT(?: OR \w+)? INTO \w+ VALUES")
    for rel in ("core/db.py", "copytrade/store.py", "safety/store.py", "copytrade/strategy.py", "copytrade/ranking.py",
                "copytrade/control.py", "copytrade/learn.py"):
        assert not bare.findall((root / rel).read_text()), rel


def test_fix_after_a_red_round_skips_the_bundling(repo):
    work, push, head, alerts = repo
    ok = lambda: (True, "2 von 2 Dienste laufen stabil")   # noqa: E731
    push("a", red=True)
    assert autoupdate.run_once(alerts.append, health=ok, now=1e9).startswith("zurückgerollt")
    push("fix")
    r = autoupdate.run_once(alerts.append, health=ok, now=1e9 + 601)
    assert not r.startswith("warte"), r          # tested after 10 min, not 30 (red or green does not matter here)
