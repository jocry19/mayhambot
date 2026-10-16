#!/usr/bin/env python3
"""mayhambot app launcher: signal engine + copy-trader + dashboard as one app, no Claude needed.

  python mayham.py run               # everything in the foreground, Ctrl+C stops all (used by launchd)
  python mayham.py start             # everything in the background
  python mayham.py stop | status | restart
  python mayham.py logs [signals|copytrade|dashboard]
  python mayham.py update            # git pull (GitHub transport), install requirements, restart if running
  python mayham.py install-autostart # macOS: start at login and restart on crash (launchd)
  python mayham.py remove-autostart
"""
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOGS = ROOT / "logs"
PIDS = ROOT / "data" / "pids"
PORT = os.getenv("DASHBOARD_PORT", "8501")
HOST = os.getenv("DASHBOARD_HOST", "127.0.0.1")  # 0.0.0.0 = also reachable from other devices in the network
PY = sys.executable
LABEL = "de.mayhambot.app"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"

SERVICES = {
    "signals": [PY, "-m", "signals.run"],
    "copytrade": [PY, "-m", "copytrade", "run"],
    "dashboard": [PY, "-m", "streamlit", "run", "dashboard/app.py", "--server.headless", "true",
                  "--server.port", PORT, "--server.address", HOST, "--browser.gatherUsageStats", "false"],
}
# research + retraining loop from model/ (other thread), started only once it exists
if (ROOT / "model" / "__main__.py").exists():
    SERVICES["research"] = [PY, "-m", "model", "service"]


def _pid(name):
    f = PIDS / f"{name}.pid"
    if not f.exists():
        return None
    try:
        pid = int(f.read_text())
        os.kill(pid, 0)
        return pid
    except (ValueError, ProcessLookupError, PermissionError):
        f.unlink(missing_ok=True)
        return None


def _spawn(name, detach):
    LOGS.mkdir(exist_ok=True)
    PIDS.mkdir(parents=True, exist_ok=True)
    log = open(LOGS / f"{name}.log", "a")
    p = subprocess.Popen(SERVICES[name], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                         start_new_session=detach)
    (PIDS / f"{name}.pid").write_text(str(p.pid))
    return p


def start():
    for name in SERVICES:
        if _pid(name):
            print(f"{name}: läuft schon")
            continue
        p = _spawn(name, detach=True)
        print(f"{name}: gestartet (pid {p.pid}, log logs/{name}.log)")
    print(f"Dashboard: http://localhost:{PORT}")


def stop():
    for name in SERVICES:
        pid = _pid(name)
        if not pid:
            print(f"{name}: läuft nicht")
            continue
        try:
            os.killpg(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGTERM)
        (PIDS / f"{name}.pid").unlink(missing_ok=True)
        print(f"{name}: gestoppt")


def status():
    for name in SERVICES:
        pid = _pid(name)
        print(f"{name:10s} {'läuft (pid %d)' % pid if pid else 'aus'}")
    if PLIST.exists():
        print(f"Autostart: an ({PLIST})")
    print(f"Dashboard: http://localhost:{PORT}")


def run():
    """Foreground supervisor: restarts a crashed service after 10 s, stops all on Ctrl+C / SIGTERM."""
    procs = {n: _spawn(n, detach=False) for n in SERVICES}
    print(f"mayhambot läuft. Dashboard: http://localhost:{PORT}  (Ctrl+C beendet alles)")
    stopping = []

    def _term(*_):
        stopping.append(1)
    signal.signal(signal.SIGTERM, _term)
    try:
        while not stopping:
            time.sleep(5)
            for n, p in procs.items():
                if p.poll() is not None:
                    print(f"{n} beendet (code {p.returncode}), Neustart in 10 s")
                    time.sleep(10)
                    procs[n] = _spawn(n, detach=False)
    except KeyboardInterrupt:
        pass
    for n, p in procs.items():
        p.terminate()
        (PIDS / f"{n}.pid").unlink(missing_ok=True)
    for p in procs.values():
        try:
            p.wait(15)
        except subprocess.TimeoutExpired:
            p.kill()


def logs(name=None):
    files = [LOGS / f"{name}.log"] if name else [LOGS / f"{n}.log" for n in SERVICES]
    subprocess.call(["tail", "-n", "40", "-F", *[str(f) for f in files]])


def _checks_ok():
    """Tests must pass before a new version goes live."""
    for cmd in ([PY, "tests/test_logic.py"], [PY, "-m", "pytest", "tests", *(["model/tests"] if (ROOT / "model" / "tests").exists() else []), "-q", "-x"]):
        if subprocess.call(cmd, cwd=ROOT, stdout=subprocess.DEVNULL) != 0:
            print("Tests rot:", " ".join(cmd[1:]))
            return False
    return True


def update():
    """Pull main, install requirements, back up, run tests; restart only on green tests.
    On red tests the previous commit is restored, so a broken push never goes live."""
    old = None
    if (ROOT / ".git").exists():
        old = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        subprocess.check_call(["git", "pull", "--ff-only", "origin", "main"], cwd=ROOT)
    subprocess.check_call([PY, "-m", "pip", "install", "-q", "-r", "requirements.txt"], cwd=ROOT)
    if not _checks_ok():
        if old:
            subprocess.call(["git", "reset", "-q", "--hard", old], cwd=ROOT)
            print(f"Update abgebrochen, zurück auf {old[:7]}. Die laufende Version bleibt aktiv.")
        return False
    if (ROOT / "deploy" / "ops.py").exists():
        subprocess.call([PY, "deploy/ops.py", "backup"], cwd=ROOT)
    if PLIST.exists():
        subprocess.call(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{LABEL}"])
    elif any(_pid(n) for n in SERVICES):
        stop()
        start()
    print("Update ok:", subprocess.check_output(["git", "log", "-1", "--format=%h %s"], cwd=ROOT, text=True).strip()
          if old else "")
    return True


def update_loop(every):
    """Pull new code from GitHub every `every` seconds and restart only when something changed."""
    while True:
        try:
            before = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
            subprocess.check_call(["git", "fetch", "-q", "origin", "main"], cwd=ROOT)
            after = subprocess.check_output(["git", "rev-parse", "origin/main"], cwd=ROOT, text=True).strip()
            if before != after and after != getattr(update_loop, "bad", None):
                print(f"neue Version {after[:7]}, aktualisiere")
                if not update():
                    update_loop.bad = after  # do not retry the same broken commit every round
        except Exception as e:  # noqa: BLE001
            print("update-loop:", e)
        time.sleep(every)


def install_autostart():
    LOGS.mkdir(exist_ok=True)
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    PLIST.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key><array><string>{PY}</string><string>{ROOT / 'mayham.py'}</string><string>run</string></array>
  <key>WorkingDirectory</key><string>{ROOT}</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>{LOGS / 'launchd.log'}</string>
  <key>StandardErrorPath</key><string>{LOGS / 'launchd.log'}</string>
</dict></plist>
""")
    subprocess.call(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], stderr=subprocess.DEVNULL)
    subprocess.check_call(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(PLIST)])
    print(f"Autostart an. Dashboard: http://localhost:{PORT}")


def remove_autostart():
    subprocess.call(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"])
    PLIST.unlink(missing_ok=True)
    print("Autostart aus.")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    arg = sys.argv[2] if len(sys.argv) > 2 else None
    actions = {"run": run, "start": start, "stop": stop, "status": status, "update": update,
               "restart": lambda: (stop(), time.sleep(2), start()), "logs": lambda: logs(arg),
               "install-autostart": install_autostart, "update-loop": lambda: update_loop(int(arg or 300)), "remove-autostart": remove_autostart}
    if cmd not in actions:
        print(__doc__)
        sys.exit(1)
    actions[cmd]()
