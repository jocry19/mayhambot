"""install-autostart against a fake launchctl that unloads the agent with a delay (the exit 5 race)."""
import pytest

import mayham


class FakeLaunchd:
    def __init__(self, loaded=True, teardown_polls=3):
        self.loaded, self.teardown, self.calls = loaded, 0, []
        self.teardown_polls = teardown_polls

    def call(self, cmd, **kw):
        op = cmd[1]
        self.calls.append(op)
        if op == "print":
            if self.teardown:
                self.teardown -= 1
                return 0  # still tearing down
            return 0 if self.loaded else 113
        if op == "bootout":
            self.loaded, self.teardown = False, self.teardown_polls
            return 0
        if op == "bootstrap":
            if self.teardown:
                return 5  # Bad request: launchd not done yet
            self.loaded = True
            return 0
        return 0


def setup(monkeypatch, tmp_path, fake):
    monkeypatch.setattr(mayham, "PLIST", tmp_path / "agent.plist")
    monkeypatch.setattr(mayham, "LOGS", tmp_path / "logs")
    monkeypatch.setattr(mayham.subprocess, "call", fake.call)
    monkeypatch.setattr(mayham.time, "sleep", lambda s: None)


def test_reinstall_waits_for_teardown(monkeypatch, tmp_path):
    fake = FakeLaunchd(loaded=True)
    setup(monkeypatch, tmp_path, fake)
    mayham.install_autostart()
    assert fake.loaded and fake.calls.count("bootstrap") == 1
    assert fake.calls.index("bootstrap") > fake.calls.index("bootout")


def test_unchanged_and_loaded_is_a_noop(monkeypatch, tmp_path):
    fake = FakeLaunchd(loaded=True)
    setup(monkeypatch, tmp_path, fake)
    mayham.PLIST.write_text(mayham._plist_text())
    mayham.install_autostart()
    assert "bootout" not in fake.calls and "bootstrap" not in fake.calls


def test_bootstrap_retries_on_exit_5(monkeypatch, tmp_path):
    fake = FakeLaunchd(loaded=False)
    setup(monkeypatch, tmp_path, fake)
    orig = fake.call
    state = {"n": 0}

    def flaky(cmd, **kw):
        if cmd[1] == "bootstrap" and state["n"] == 0:
            state["n"] += 1
            fake.calls.append("bootstrap")
            return 5
        return orig(cmd, **kw)
    monkeypatch.setattr(mayham.subprocess, "call", flaky)
    mayham.install_autostart()
    assert fake.loaded and fake.calls.count("bootstrap") == 2


def test_stop_reports_services_stopped_by_launchd(monkeypatch, tmp_path, capsys):
    fake = FakeLaunchd(loaded=True)
    setup(monkeypatch, tmp_path, fake)
    mayham.PLIST.write_text("x")
    alive = {"signals": 1}
    monkeypatch.setattr(mayham, "_pid", lambda n: alive.get(n) if fake.loaded else None)
    mayham.stop()
    out = capsys.readouterr().out
    assert "signals: gestoppt" in out and "läuft nicht" not in out


def test_stale_pid_of_another_program_is_not_ours(monkeypatch, tmp_path):
    import os
    monkeypatch.setattr(mayham, "PIDS", tmp_path)
    (tmp_path / "web.pid").write_text(str(os.getpid()))  # the pytest process, not `python -m web`
    assert mayham._pid("web") is None and not (tmp_path / "web.pid").exists()


def test_failed_pip_rolls_back(monkeypatch, tmp_path):
    calls = []

    def call(cmd, **kw):
        calls.append(cmd)
        return 1 if "pip" in cmd else 0
    monkeypatch.setattr(mayham, "ROOT", tmp_path)
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(mayham.subprocess, "check_output", lambda *a, **k: "oldsha\n")
    monkeypatch.setattr(mayham.subprocess, "check_call", lambda *a, **k: 0)
    monkeypatch.setattr(mayham.subprocess, "call", call)
    monkeypatch.setattr(mayham, "_checks_ok", lambda: pytest.fail("no tests on a failed install"))
    assert mayham.update() is False
    assert ["git", "reset", "-q", "--hard", "oldsha"] in calls
