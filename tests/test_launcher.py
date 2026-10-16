"""install-autostart against a fake launchctl that unloads the agent with a delay (the exit 5 race)."""
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
