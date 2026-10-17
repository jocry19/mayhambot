import subprocess
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
    monkeypatch.setattr(mayham, "_checks_ok", lambda *a: pytest.fail("no tests on a failed install"))
    assert mayham.update() is False
    assert ["git", "reset", "-q", "--hard", "oldsha"] in calls


def test_update_tests_the_target_before_the_live_checkout_moves(monkeypatch, tmp_path):
    """While the gate runs, a service restarted by the supervisor or watchdog must still get the tested code."""
    events = []
    monkeypatch.setattr(mayham, "ROOT", tmp_path)
    monkeypatch.setattr(mayham, "PLIST", tmp_path / "none.plist")
    (tmp_path / ".git").mkdir()
    shas = {("git", "rev-parse", "HEAD"): "old\n", ("git", "rev-parse", "origin/main"): "new\n"}
    monkeypatch.setattr(mayham.subprocess, "check_output",
                        lambda cmd, **k: shas.get(tuple(cmd), "requests\n" if cmd[1] == "show" else "x\n"))
    monkeypatch.setattr(mayham.subprocess, "check_call", lambda cmd, **k: events.append(tuple(cmd[:2])) or 0)
    monkeypatch.setattr(mayham.subprocess, "call", lambda cmd, **k: events.append(tuple(cmd[:2])) or 0)
    monkeypatch.setattr(mayham, "_pid", lambda n: None)
    monkeypatch.setenv("MAYHAM_SKIP_BACKUP", "1")

    def gate(ref):
        events.append(("gate", ref))
        assert ("git", "merge") not in events, "checkout moved before the gate was green"
        return True
    monkeypatch.setattr(mayham, "_checks_ok", gate)
    assert mayham.update() is True
    assert ("gate", "new") in events and events.index(("gate", "new")) < events.index(("git", "merge"))

    events.clear()
    monkeypatch.setattr(mayham, "_checks_ok", lambda ref: events.append(("gate", ref)) and False)
    assert mayham.update() is False
    assert ("git", "merge") not in events


def test_red_gate_prints_the_failing_test(monkeypatch, capsys):
    class R:
        returncode, stdout = 1, "\n".join(f"line {i}" for i in range(50)) + "\nFAILED tests/x.py::test_y"
    monkeypatch.setattr(mayham.subprocess, "run", lambda *a, **k: R())
    assert mayham._checks_ok() is False
    out = capsys.readouterr().out
    assert "FAILED tests/x.py::test_y" in out and "line 5\n" not in out


def test_crash_loop_backs_off_without_holding_up_others(monkeypatch, tmp_path):
    clock = [0.0]
    spawned = []

    class Proc:
        def __init__(self, name):
            self.name, self.returncode = name, None
            spawned.append((name, clock[0]))

        def poll(self):
            if self.name == "web" and self.returncode is None:
                self.returncode = 1  # crashes right away, every time
            return self.returncode

        def terminate(self):
            pass

        def wait(self, t):
            pass

    def sleep(s):
        clock[0] += s
        if clock[0] > 400:
            raise KeyboardInterrupt
    monkeypatch.setattr(mayham, "PIDS", tmp_path)
    monkeypatch.setattr(mayham, "_stop_detached", lambda quiet=False: None)
    monkeypatch.setattr(mayham, "_spawn", lambda n, detach: Proc(n))
    monkeypatch.setattr(mayham.time, "sleep", sleep)
    monkeypatch.setattr(mayham.time, "time", lambda: clock[0])
    monkeypatch.setattr(mayham.signal, "signal", lambda *a: None)
    mayham.run()
    web = [t for n, t in spawned if n == "web"]
    gaps = [b - a for a, b in zip(web, web[1:])]
    assert gaps[:4] == [15, 25, 45, 85]  # 10, 20, 40, 80 s back-off plus the 5 s poll
    assert len([n for n, _ in spawned if n != "web"]) == len(mayham.SERVICES) - 1  # the others never restarted


def test_shutdown_shares_one_deadline(monkeypatch, tmp_path):
    """launchd kills the supervisor ~20 s after SIGTERM: slow services must not each get their own 15 s."""
    monkeypatch.setattr(mayham, "PIDS", tmp_path)
    waits = []

    class Slow:
        def terminate(self): pass
        def kill(self): self.killed = True
        def wait(self, t=None):
            if t is None:
                return 0
            waits.append(t)
            clock[0] += t
            raise subprocess.TimeoutExpired("x", t)
    procs = {f"s{i}": Slow() for i in range(5)}
    clock = [0.0]
    monkeypatch.setattr(mayham.time, "time", lambda: clock[0])
    mayham._shutdown(procs, grace_s=10)
    assert sum(waits) <= 10 and all(p.killed for p in procs.values())
