#!/usr/bin/env python3
"""mayhambot app launcher: signal engine + copy-trader + dashboard as one app, no Claude needed.

  python mayham.py run               # everything in the foreground, Ctrl+C stops all (used by launchd)
  python mayham.py start             # everything in the background
  python mayham.py stop | status | restart
  python mayham.py logs [signals|copytrade|research|web|safety|sniper|market|intel|brain]
  python mayham.py update            # git pull (GitHub transport), install requirements, restart if running
  python mayham.py install-autostart # macOS: start at login and restart on crash (launchd)
  python mayham.py remove-autostart
"""
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOGS = ROOT / "logs"
PIDS = ROOT / "data" / "pids"
PORT = os.getenv("WEB_PORT", "8600")
HOST = os.getenv("WEB_HOST", "127.0.0.1")  # read by web/ itself; 0.0.0.0 = also reachable in the network
PY = sys.executable
LABEL = "de.mayhambot.app"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"

SERVICES = {
    "signals": [PY, "-m", "signals.run"],
    "copytrade": [PY, "-m", "copytrade", "run"],
    "web": [PY, "-m", "web"],
    "safety": [PY, "-m", "safety", "run"],  # rug/honeypot/insider refresher, every SAFETY_REFRESH_EVERY_SEC  # the dashboard (FastAPI + built frontend); the old Streamlit dashboard/ is no longer started
    "sniper": [PY, "-m", "sniper", "run"],  # launch sniper with bundle check, paper mode only (sniper/README.md)
    "market": [PY, "-m", "market", "run"],  # market phase, sectors, narratives; keyless, no Helius (market/README.md)
    "live": [PY, "-m", "live", "run"],  # Echtgeld-Wächter: Not-Aus per Telegram, unklare Orders, Datenausfall; public RPC only (live/README.md)
    "intel": [PY, "-m", "intel", "run"],  # Zentrale: point-in-time features, ticker, source evaluation; DB only (intel/README.md)
    "brain": [PY, "-m", "brain", "run"],  # learning decider, own paper account next to the strategies (brain/README.md)
}
# research + retraining loop from model/ (other thread), started only once it exists
if (ROOT / "model" / "__main__.py").exists():
    SERVICES["research"] = [PY, "-m", "model", "service"]


def _is_service(pid, name):
    """True unless `ps` shows that `pid` now belongs to another program (a pid file left by a crash or power
    loss; after a reboot the number can be any of the user's processes, and stop would kill its group)."""
    try:
        cmd = subprocess.run(["ps", "-ww", "-o", "command=", "-p", str(pid)], capture_output=True,
                             text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return True  # no ps: keep the old behaviour
    return not cmd or " ".join(SERVICES[name][1:]) in cmd


def _pid(name):
    f = PIDS / f"{name}.pid"
    if not f.exists():
        return None
    try:
        pid = int(f.read_text())
        os.kill(pid, 0)
        if not _is_service(pid, name):
            raise ProcessLookupError
        return pid
    except (ValueError, ProcessLookupError, PermissionError):
        f.unlink(missing_ok=True)
        return None


def _spawn(name, detach):
    LOGS.mkdir(exist_ok=True)
    PIDS.mkdir(parents=True, exist_ok=True)
    log = open(LOGS / f"{name}.log", "a")
    env = dict(os.environ, MAYHAM_SERVICE=name)  # core.http applies this service's API budgets
    p = subprocess.Popen(SERVICES[name], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                         start_new_session=detach, env=env)
    (PIDS / f"{name}.pid").write_text(str(p.pid))
    return p


def _agent():
    return f"gui/{os.getuid()}/{LABEL}"


def _agent_present():
    return subprocess.call(["launchctl", "print", _agent()], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL) == 0


def _agent_loaded():
    return PLIST.exists() and _agent_present()


def start():
    if PLIST.exists():
        # autostart owns the services: load/kick the agent instead of spawning a second copy
        _stop_detached(quiet=True)
        if not _agent_present():
            _bootstrap()
        subprocess.call(["launchctl", "kickstart", _agent()])
        print(f"mayhambot läuft über Autostart ({LABEL}). Dashboard: http://localhost:{PORT}")
        return
    for name in SERVICES:
        if _pid(name):
            print(f"{name}: läuft schon")
            continue
        p = _spawn(name, detach=True)
        print(f"{name}: gestartet (pid {p.pid}, log logs/{name}.log)")
    print(f"Dashboard: http://localhost:{PORT}")


def stop():
    if _agent_loaded():
        running = [n for n in SERVICES if _pid(n)]
        # unload only; the plist stays, so autostart is back at next login or with `start`
        subprocess.call(["launchctl", "bootout", _agent()])
        _wait_unloaded()
        for name in SERVICES:
            print(f"{name}: {'gestoppt' if name in running else 'lief nicht'}")
        print("Autostart-Agent angehalten (bleibt eingerichtet; `start` oder nächster Login startet ihn wieder)")
        _stop_detached(quiet=True)  # leftovers from a detached `start`, reported only if any
        return
    _stop_detached()


def restart():
    if _agent_loaded():
        subprocess.call(["launchctl", "kickstart", "-k", _agent()])
        print("mayhambot über Autostart neu gestartet")
        return
    stop()
    time.sleep(2)
    start()


def _stop_detached(quiet=False):
    for name in SERVICES:
        pid = _pid(name)
        if not pid:
            if not quiet:
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


PLANNED_EXIT = 3  # a service asks for a restart with the code on disk (e.g. signals after a model retrain)


def _note_planned(name):
    """deploy/ watchdog and auto-update read this, so a planned restart is no crash and no rollback reason."""
    with open(ROOT / "data" / "planned_restarts.jsonl", "a") as f:
        f.write(json.dumps({"ts": time.time(), "service": name}) + "\n")


RESTART_S, RESTART_MAX_S, STABLE_S = 10, 300, 600


def _compact_if_flagged(db=None):
    """The nightly retention pass (deploy/ops.py retention) asks for a one-time VACUUM into incremental auto_vacuum
    by leaving data/COMPACT_DB; it rewrites the whole file, so it runs here, before any service has it open."""
    db = Path(db or os.getenv("MAYHAM_DB_PATH") or ROOT / "data" / "mayhambot.db")
    flag = db.parent / "COMPACT_DB"
    if not flag.exists() or not db.exists():
        return None
    try:
        from core import retention
        msg = retention.compact(db)
    except Exception as e:  # noqa: BLE001 - a failed VACUUM leaves the file as it was; services start anyway
        msg = f"Verdichtung fehlgeschlagen: {type(e).__name__}: {e}"
    flag.unlink(missing_ok=True)
    print(msg, flush=True)
    return msg


def run():
    """Foreground supervisor: restarts a crashed service after 10 s, doubling up to 5 min while it keeps crashing
    (reset after 10 min up or a planned restart), without holding up the other services; stops all on
    Ctrl+C / SIGTERM."""
    _stop_detached(quiet=True)  # never run next to copies started with `start`
    _compact_if_flagged()
    procs = {n: _spawn(n, detach=False) for n in SERVICES}
    started = {n: time.time() for n in SERVICES}
    crashes = {n: 0 for n in SERVICES}
    due = {}
    print(f"mayhambot läuft. Dashboard: http://localhost:{PORT}  (Ctrl+C beendet alles)")
    stopping = []

    def _term(*_):
        stopping.append(1)
    signal.signal(signal.SIGTERM, _term)
    try:
        while not stopping:
            time.sleep(5)
            now = time.time()
            for n, p in procs.items():
                if p is None:
                    if now >= due[n] and not stopping:
                        procs[n], started[n] = _spawn(n, detach=False), now
                    continue
                if p.poll() is None:
                    continue
                planned = p.returncode == PLANNED_EXIT
                if planned:
                    _note_planned(n)
                if planned or now - started[n] >= STABLE_S:
                    crashes[n] = 0
                delay = min(RESTART_MAX_S, RESTART_S * 2 ** crashes[n])
                crashes[n] += 0 if planned else 1
                print(f"{n} beendet (code {p.returncode}), Neustart in {delay} s")
                procs[n], due[n] = None, now + delay
    except KeyboardInterrupt:
        pass
    _shutdown(procs)


def _shutdown(procs, grace_s=10):
    """Stop all services within one shared deadline: launchd kills the supervisor itself ~20 s after SIGTERM, and
    waiting 15 s per service in turn left slow ones (brain mid-training) running next to the new copies."""
    live = {n: p for n, p in procs.items() if p is not None}
    for p in live.values():
        p.terminate()
    deadline = time.time() + grace_s
    for n, p in live.items():
        try:
            p.wait(max(0.0, deadline - time.time()))
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
        (PIDS / f"{n}.pid").unlink(missing_ok=True)


def logs(name=None):
    files = [LOGS / f"{name}.log"] if name else [LOGS / f"{n}.log" for n in SERVICES]
    subprocess.call(["tail", "-n", "40", "-F", *[str(f) for f in files]])


def _checks_ok(ref="HEAD"):
    """Tests must pass before a new version goes live. They run in a throwaway git worktree of HEAD: no data/,
    logs/ or .env of the live bot, so the gate on the Mac sees the same world as a fresh clone (tests that read
    data/ by relative path would otherwise trip over the bot's real state files)."""
    import tempfile
    with tempfile.TemporaryDirectory(prefix="mayham-gate-") as td:
        wt = Path(td) / "wt"
        subprocess.run(["git", "worktree", "add", "-q", "--detach", str(wt), ref], cwd=ROOT, check=True,
                       capture_output=True)
        # paths a test might resolve absolutely stay inside the throwaway tree, never the live DB or logs/
        (wt / "data").mkdir(parents=True, exist_ok=True)
        (wt / "logs").mkdir(parents=True, exist_ok=True)
        env = {**os.environ, "MAYHAM_DB_PATH": str(wt / "data" / "mayhambot.db"), "MAYHAM_LOG_DIR": str(wt / "logs")}
        try:
            dirs = ["tests"] + sorted(str(p.relative_to(wt)) for p in wt.glob("*/tests")
                                      if p.is_dir() and not p.parent.name.startswith(".")
                                      and p.parent.name != "node_modules")
            print("Tests: " + ", ".join(dirs))
            for cmd in ([PY, "tests/test_logic.py"], [PY, "-m", "pytest", *dirs, "-q", "-x", "-p", "no:cacheprovider"]):
                t0 = time.time()
                r = subprocess.run(cmd, cwd=wt, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
                last = ((r.stdout or "").strip().splitlines() or [""])[-1]
                print(f"{cmd[-1] if len(cmd) == 2 else 'pytest'}: {time.time() - t0:.0f} s ({last[:120]})")
                if r.returncode != 0:
                    print("Tests rot:", " ".join(cmd[1:]))
                    print("\n".join((r.stdout or "").splitlines()[-30:]))  # the failing test lands in update.log
                    return False
            return True
        finally:
            subprocess.run(["git", "worktree", "remove", "--force", str(wt)], cwd=ROOT, capture_output=True)
            subprocess.run(["git", "worktree", "prune"], cwd=ROOT, capture_output=True)


def update():
    """Pull main, install requirements, back up, run tests; restart only on green tests.
    On red tests the previous commit is restored, so a broken push never goes live."""
    old = target = None
    if (ROOT / ".git").exists():
        old = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        subprocess.check_call(["git", "fetch", "-q", "origin", "main"], cwd=ROOT)
        target = subprocess.check_output(["git", "rev-parse", "origin/main"], cwd=ROOT, text=True).strip()
    # The live checkout stays on the running, tested version until the gate is green: a service the supervisor
    # or the watchdog restarts meanwhile must never pick up untested code (a new service was started that way).
    reqs = "requirements.txt"
    if target and target != old:
        reqs = os.path.join(tempfile.mkdtemp(prefix="mayham-req-"), "requirements.txt")
        Path(reqs).write_text(subprocess.check_output(["git", "show", f"{target}:requirements.txt"], cwd=ROOT,
                                                      text=True))
    pip = [PY, "-m", "pip", "install", "-q", "-r", reqs]
    installed = subprocess.call(pip, cwd=ROOT) == 0
    if not installed:
        print("pip install fehlgeschlagen")
    if not installed or not _checks_ok(target or "HEAD"):
        if old:
            subprocess.call(["git", "reset", "-q", "--hard", old], cwd=ROOT)  # no-op unless something moved it
            subprocess.call([PY, "-m", "pip", "install", "-q", "-r", "requirements.txt"], cwd=ROOT)
            print(f"Update abgebrochen, zurück auf {old[:7]}. Die laufende Version bleibt aktiv.")
        return False
    if target and target != old:
        subprocess.check_call(["git", "merge", "-q", "--ff-only", target], cwd=ROOT)
    if (ROOT / "deploy" / "ops.py").exists() and not os.environ.get("MAYHAM_SKIP_BACKUP"):
        subprocess.call([PY, "deploy/ops.py", "backup"], cwd=ROOT)  # the auto-updater backs up before the pull
    if PLIST.exists():
        # never bootout/unlink here: kickstart a loaded agent, load it if it got unloaded
        restart() if _agent_loaded() else start()
    elif any(_pid(n) for n in SERVICES):
        _stop_detached()
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


def _plist_text():
    return f"""<?xml version="1.0" encoding="UTF-8"?>
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
"""


def _wait_unloaded(timeout=30):
    """launchd tears an agent down asynchronously after bootout; bootstrap before that fails (exit 5)."""
    end = time.time() + timeout
    while _agent_present() and time.time() < end:
        time.sleep(0.5)
    return not _agent_present()


def _bootstrap(attempts=3, pause=3):
    for i in range(attempts):
        rc = subprocess.call(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(PLIST)])
        if rc == 0 or _agent_present():
            return True
        print(f"launchctl bootstrap exit {rc}, neuer Versuch in {pause} s")
        time.sleep(pause)
        _wait_unloaded(10)
    return False


def install_autostart():
    LOGS.mkdir(exist_ok=True)
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    text = _plist_text()
    if PLIST.exists() and PLIST.read_text() == text and _agent_present():
        print(f"Autostart ist schon an und unverändert. Dashboard: http://localhost:{PORT}")
        return
    PLIST.write_text(text)
    if _agent_present():
        subprocess.call(["launchctl", "bootout", _agent()], stderr=subprocess.DEVNULL)
        if not _wait_unloaded():
            sys.exit("Autostart-Agent lässt sich nicht entladen; bitte später erneut versuchen.")
    if not _bootstrap():
        sys.exit("Autostart konnte nicht geladen werden (launchctl bootstrap). Der Plist bleibt; "
                 "`mayham.py start` versucht es erneut.")
    print(f"Autostart an. Dashboard: http://localhost:{PORT}")


def remove_autostart():
    subprocess.call(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"])
    PLIST.unlink(missing_ok=True)
    print("Autostart aus.")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    arg = sys.argv[2] if len(sys.argv) > 2 else None
    actions = {"run": run, "start": start, "stop": stop, "status": status, "update": update,
               "restart": restart, "logs": lambda: logs(arg),
               "install-autostart": install_autostart, "update-loop": lambda: update_loop(int(arg or 300)), "remove-autostart": remove_autostart}
    if cmd not in actions:
        print(__doc__)
        sys.exit(1)
    actions[cmd]()
