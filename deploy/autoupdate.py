#!/usr/bin/env python3
"""mayhambot Auto-Update (opt-in, eingerichtet mit deploy/mac/install-updater.sh).

Einmal pro Aufruf (launchd alle 10 min): main holen, bei neuem Stand `mayham.py update` (Test-Gate, Backup,
Neustart, Rollback bei roten Tests) und danach `mayham.py install-autostart`. Jeder Deploy und jeder Abbruch
kommt per Telegram und steht in logs/update.log.

Spielt nichts ein, wenn
  - data/NO_AUTO_UPDATE existiert oder AUTO_UPDATE=0 in .env steht (Not-Aus),
  - LIVE_TRADING=true in .env steht oder offene Echtgeld-Positionen in der DB sind,
  - der Checkout lokale Änderungen hat oder nicht vorspulbar ist,
  - der letzte Deploy weniger als AUTO_UPDATE_MIN (10) Minuten her ist,
  - das Backup vor dem Update fehlschlägt (Integritätsprüfung),
Nach dem Neustart müssen alle Dienste 3 min stabil laufen (gleiche PID) und das Dashboard antworten,
sonst geht es automatisch zurück auf den alten Stand (Telegram-Meldung, Commit wird nicht wieder probiert).
  - derselbe Commit schon einmal an den Tests gescheitert ist (der nächste Push wird wieder probiert).

  python deploy/autoupdate.py           # einmal prüfen (das macht launchd)
  python deploy/autoupdate.py --status  # Stand anzeigen
"""
from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ops  # noqa: E402

ROOT = ops.ROOT
TRUE = {"1", "true", "yes", "on"}


def _paths():
    return (ROOT / "data" / "autoupdate_state.json", ROOT / "data" / "NO_AUTO_UPDATE",
            ROOT / "logs" / "update.log", ROOT / "data" / "autoupdate.lock")


def _load() -> dict:
    try:
        return json.loads(_paths()[0].read_text())
    except (OSError, ValueError):
        return {}


def _save(st: dict):
    f = _paths()[0]
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=1))
    tmp.replace(f)


def log(msg: str):
    f = _paths()[2]
    f.parent.mkdir(parents=True, exist_ok=True)
    with open(f, "a") as fh:
        fh.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")
    print(msg)


def git(*a, timeout=120) -> str:
    return subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True, timeout=timeout,
                          check=True).stdout.strip()


def _python() -> str:
    py = ROOT / ".venv" / "bin" / "python"
    return str(py) if py.exists() else sys.executable


def live_reason(env: dict) -> str | None:
    """Why real money might be at stake right now, or None."""
    # every source counts, the most cautious wins: a value an earlier dotenv import left in os.environ must
    # never hide LIVE_TRADING=true written to .env later (ops.load_env lets the environment override the file)
    vals = [env.get("LIVE_TRADING", ""), os.environ.get("LIVE_TRADING", ""), _env_file().get("LIVE_TRADING", "")]
    if any(str(v).strip().lower() in TRUE for v in vals):
        return "LIVE_TRADING=true in .env"
    db = ops.db_path()
    if db.exists():
        try:
            c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
            n = c.execute("SELECT COUNT(*) FROM ct_positions WHERE mode='live' AND status IN ('open','opening','closing')").fetchone()[0]
            c.close()
            if n:
                return f"{n} offene Echtgeld-Position(en)"
        except sqlite3.Error:
            pass
    return None


def disabled(env: dict) -> str | None:
    if _paths()[1].exists():
        return "data/NO_AUTO_UPDATE existiert"
    if str(env.get("AUTO_UPDATE", "1")).strip().lower() in {"0", "false", "no", "off"}:
        return "AUTO_UPDATE=0 in .env"
    return None


def _services_now() -> list[str]:
    """Service list of the version now on disk (a deploy can add services)."""
    try:
        out = subprocess.run([_python(), "-c", "import mayham; print(' '.join(mayham.SERVICES))"], cwd=ROOT,
                             capture_output=True, text=True, timeout=60).stdout.split()
        return out or list(ops.SERVICES)
    except Exception:  # noqa: BLE001
        return list(ops.SERVICES)


def _pids(names) -> dict:
    out = {}
    for n in names:
        try:
            pid = int((ROOT / "data" / "pids" / f"{n}.pid").read_text())
            os.kill(pid, 0)
            out[n] = pid
        except (OSError, ValueError):
            out[n] = None
    return out


PLANNED_GRACE_S = 60  # the supervisor waits 10 s, then the service needs a moment


def health_check(window=None, interval=15) -> tuple[bool, str]:
    """After a restart: every service must be up and keep the same pid (no crash loop) for the last part of
    the window, and the dashboard must answer. One planned restart per service (exit code 3, e.g. signals
    after a model retrain, see ops.planned_restarts) is no crash: it does not reset the stable time, and the
    window grows once by PLANNED_GRACE_S so the new process can come up. Returns (ok, detail)."""
    window = window if window is not None else ops.cfg("AUTO_UPDATE_HEALTH_S", 180)
    names = _services_now()
    waited = time.time()
    while ops.compacting() and time.time() - waited < ops.cfg("COMPACT_MAX_MIN", 120) * 60:
        time.sleep(interval)    # the supervisor VACUUMs before starting the services: the window starts after
    t0 = time.time()
    end = t0 + window
    stable_since, last, detail = None, None, "noch nicht geprüft"
    planned, extended = {}, False
    while True:
        for e in ops.planned_restarts(t0 - 30):
            planned.setdefault(e.get("service"), []).append(e)
        if planned and not extended:
            end, extended = max(end, time.time() + PLANNED_GRACE_S), True
        once = {n for n, es in planned.items() if len({round(e["ts"]) for e in es}) == 1}
        cur = _pids(names)
        down = [n for n, p in cur.items() if not p]
        changed = [n for n in names if last and cur[n] and last[n] and cur[n] != last[n]]
        bad_down = [n for n in down if n not in once]
        bad_changed = [n for n in changed if n not in once]
        if bad_down or bad_changed:
            stable_since = None
            detail = ", ".join([f"{n} läuft nicht" for n in bad_down] + [f"{n} startet neu" for n in bad_changed])
        elif stable_since is None:
            stable_since = time.time()
        last = cur
        if time.time() >= end:
            break
        time.sleep(interval)
    if any(not p for p in cur.values()):
        return False, ", ".join(f"{n} läuft nicht" for n, p in cur.items() if not p)
    if stable_since is None or time.time() - stable_since < min(60, window / 2):
        return False, detail
    if not ops.check_dashboard():
        return False, "Dashboard antwortet nicht"
    notes = "".join(f", {ops.planned_note(es[-1])}" for n, es in planned.items() if n in once)
    return True, f"{len(names)} von {len(names)} Dienste laufen stabil{notes}"


def _failing_tests(rev: str) -> list[str]:
    """Run the gate's pytest on `rev` in a throwaway worktree (the live checkout stays untouched) and return
    the FAILED/ERROR lines, so a red gate names its tests (mayham.py update hides pytest's output)."""
    import tempfile  # noqa: PLC0415
    with tempfile.TemporaryDirectory() as td:
        wt = Path(td) / "wt"
        try:
            git("worktree", "add", "-q", "--detach", str(wt), rev)
            dirs = ["tests"] + sorted(str(p.relative_to(wt)) for p in wt.glob("*/tests") if p.is_dir())
            r = subprocess.run([_python(), "-m", "pytest", *dirs, "-q", "-p", "no:cacheprovider"], cwd=wt,
                               capture_output=True, text=True, timeout=900)
            return [ln for ln in r.stdout.splitlines() if ln.startswith(("FAILED", "ERROR"))][:10]
        except Exception as e:  # noqa: BLE001
            return [f"Diagnose fehlgeschlagen: {e}"]
        finally:
            subprocess.run(["git", "worktree", "remove", "--force", str(wt)], cwd=ROOT, capture_output=True)
            subprocess.run(["git", "worktree", "prune"], cwd=ROOT, capture_output=True)


def _lock_count(since_sizes: dict) -> int:
    n = 0
    for f, size in since_sizes.items():
        try:
            with open(f, "rb") as fh:
                fh.seek(size if os.path.getsize(f) >= size else 0)
                n += fh.read().count(b"database is locked")
        except OSError:
            pass
    return n


def _log_sizes() -> dict:
    return {str(f): f.stat().st_size for f in (ROOT / "logs").glob("*.log") if f.name != "update.log"}


def _finish_interrupted(pend: dict, alert, health=None) -> str | None:
    """A round stopped after `mayham.py update` may have pulled and restarted without its health check and
    end line. If the checkout moved, check the running version now: keep it when healthy, else roll back."""
    cur = git("rev-parse", "HEAD")
    log(f"vorige Runde ohne Abschluss ({pend.get('head', '?')[:7]} -> {pend.get('target', '?')[:7]}), "
        f"Checkout jetzt {cur[:7]}")
    if cur == pend.get("head"):
        return None    # nothing was pulled: a normal round follows
    subject = git("log", "-1", "--format=%h %s", cur)[:90]
    subprocess.run([_python(), "mayham.py", "restart"], cwd=ROOT, capture_output=True, timeout=300)
    ok, detail = (health or health_check)()
    st = _load()
    if ok:
        st.pop("bad", None)
        st["red_streak"] = 0
        st["deployed"] = cur
        _save(st)
        log(f"deployed {subject} (nachgeholt): {detail}")
        alert(f"⬆️ Update eingespielt: {subject}, Tests grün, {detail} (Prüfung nach abgebrochener Runde nachgeholt).")
        return f"eingespielt {cur[:7]} (nachgeholt)"
    git("reset", "-q", "--hard", pend["head"])
    subprocess.run([_python(), "-m", "pip", "install", "-q", "-r", "requirements.txt"], cwd=ROOT,
                   capture_output=True, timeout=900)
    subprocess.run([_python(), "mayham.py", "restart"], cwd=ROOT, capture_output=True, timeout=300)
    ok2, detail2 = (health or health_check)()
    st["bad"] = cur
    _save(st)
    log(f"rolled back after interrupted round: {subject}: {detail}; old version: {detail2}")
    alert(f"↩️ Update zurückgerollt: {subject}. Grund: {detail} (Runde war abgebrochen). Alte Version "
          f"{pend['head'][:7]} läuft wieder{'' if ok2 else ' NICHT sauber: ' + detail2}. Details: logs/update.log")
    return f"zurückgerollt {cur[:7]} ({detail})"


def _env_file() -> dict:
    """KEY=value pairs of .env as written in the file, without the environment on top."""
    out = {}
    try:
        for line in (ROOT / ".env").read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip().removeprefix("export ").strip()] = v.split(" #", 1)[0].strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def _gate_env() -> dict:
    """Environment for `mayham.py update`: without the .env keys (Telegram, live, wallet, API keys) that an
    earlier dotenv import may have copied into this process, so the test gate never sees real credentials;
    the services read .env themselves. MAYHAM_SKIP_BACKUP: the backup above is fresh, a second costs minutes."""
    keys = set(_env_file())
    env = {k: v for k, v in os.environ.items() if k not in keys and not k.startswith(SECRET_PREFIXES)}
    env["MAYHAM_SKIP_BACKUP"] = "1"
    return env


SECRET_PREFIXES = ("TELEGRAM_", "LIVE_", "SOLANA_", "HELIUS_", "WALLET_", "KEYPAIR")


def _recent_backup(max_age_s: float):
    """Newest finished backup archive if it is younger than max_age_s (written atomically by ops.backup)."""
    try:
        files = sorted(ops.backup_dir().glob("mayhambot-*.tar.gz"), key=lambda f: f.stat().st_mtime)
    except OSError:
        return None
    if files and time.time() - files[-1].stat().st_mtime < max_age_s:
        return files[-1]
    return None


def _run_group(cmd, timeout_s: float, env=None) -> subprocess.CompletedProcess:
    """Run cmd in its own process group; on timeout kill the whole group (pip, pytest and their children too,
    which subprocess.run's timeout would leave running) and report it as a failed run (returncode -9)."""
    p = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
                         start_new_session=True)
    import signal  # noqa: PLC0415
    try:
        out, err = p.communicate(timeout=timeout_s)
        try:   # a test that left a process behind must not keep running against anything after the gate
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
        return subprocess.CompletedProcess(cmd, p.returncode, out, err)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
        out, err = p.communicate()
        return subprocess.CompletedProcess(cmd, -9, out or "", (err or "") +
                                           f"\nZeitüberschreitung nach {timeout_s / 60:.0f} min, abgebrochen")


def _foreground():
    """launchd's ProcessType Background throttles CPU, I/O and timers for the job and its children (the test gate
    took 16+ min instead of ~3). Leave the Darwin background band for this process; children inherit it."""
    prio = getattr(os, "PRIO_DARWIN_PROCESS", 4 if sys.platform == "darwin" else None)
    if prio is None:
        return
    try:
        os.setpriority(prio, 0, 0)
        log(f"priority: left background band (nice {os.getpriority(os.PRIO_PROCESS, 0)})")
    except (OSError, AttributeError) as e:
        log(f"priority: still in background band ({e})")


def run_once(alert=ops.send_alert, now=None, health=None) -> str:
    env = ops.load_env()
    now = now or time.time()
    st = _load()
    why = disabled(env)
    if why:
        return f"aus ({why})"
    if not (ROOT / ".git").exists():
        return "kein git-Checkout"
    pend = st.pop("deploying", None)
    if pend:   # the previous round died between pull and its end line (exception, kill, sleep): finish it now
        _save(st)
        done = _finish_interrupted(pend, alert, health)
        if done:
            return done
    git("fetch", "-q", "origin", "main")
    head, target = git("rev-parse", "HEAD"), git("rev-parse", "origin/main")
    if head == target:
        return f"aktuell ({head[:7]})"
    subject = git("log", "-1", "--format=%h %s", target)[:90]
    if target == st.get("bad"):
        return f"übersprungen: {target[:7]} ist schon an den Tests gescheitert"
    if subprocess.run(["git", "merge-base", "--is-ancestor", head, target], cwd=ROOT).returncode != 0:
        local = git("log", "--format=%h %s", f"{target}..{head}").splitlines()
        behind = len(git("rev-list", f"{head}..{target}").splitlines())
        msg = (f"⛔ Lokaler Commit blockiert das Update: {len(local)} Commit(s) nur auf dem Mac "
               f"({'; '.join(local[:3])}), {behind} Commit(s) von main fehlen. Bitte sichern und zurücksetzen.")
        log(msg)
        st["blocked"] = msg
        _save(st)
        return _once(st, alert, f"diverged:{head}:{target}", msg)
    st.pop("blocked", None)
    if git("status", "--porcelain", "--untracked-files=no"):
        return _once(st, alert, f"dirty:{target}", "⚠️ Auto-Update wartet: lokale Änderungen im Checkout.")
    live = live_reason(env)
    if live:
        log(f"update pending: live trading on ({live}), {subject}")
        return _once(st, alert, f"live:{target}", f"⏸ Update {subject} wartet: {live}. "
                     "Einspielen von Hand mit `.venv/bin/python mayham.py update`.")
    # launchd fires every 600 s counted from the previous start; without slack a deploy every 10 min would
    # often see 599 s and wait a whole extra round
    gap = ops.cfg("AUTO_UPDATE_MIN", 10) * 60 - 90
    if now - st.get("last_deploy", 0) < gap:
        return "warte (höchstens ein Deploy pro 10 min)"

    # no deploy without a fresh, verified backup (integrity_check in ops.backup; a damaged copy is never written).
    # The hourly backup job counts when it is young enough: a backup of the grown DB takes ~10 min on the Mac.
    t_b = time.time()
    recent = _recent_backup(ops.cfg("AUTO_UPDATE_BACKUP_MAX_AGE_MIN", 60) * 60)
    if recent:
        log(f"backup before deploy of {subject}: {recent.name} is {(time.time() - recent.stat().st_mtime) / 60:.0f} min old, reused")
    else:
        log(f"backup before deploy of {subject} ...")
        b = subprocess.run([_python(), "deploy/ops.py", "backup"], cwd=ROOT, capture_output=True, text=True,
                           timeout=1800)
        if b.returncode != 0:
            log(f"backup failed, update pending: {(b.stdout + b.stderr).strip()[-500:]}")
            return _once(st, alert, f"backup:{target}", f"⏸ Update {subject} wartet: Backup vor dem Update ist "
                         "fehlgeschlagen. Details: logs/update.log")
    log(f"deploy {head[:7]} -> {subject} (backup {time.time() - t_b:.0f} s)")
    st["last_deploy"] = now
    st["deploying"] = {"head": head, "target": target, "at": now}
    _save(st)
    t_u = time.time()
    r = _run_group([_python(), "mayham.py", "update"], ops.cfg("AUTO_UPDATE_TIMEOUT_MIN", 45) * 60, _gate_env())
    log((r.stdout + r.stderr).strip()[-4000:])
    log(f"update step took {time.time() - t_u:.0f} s (exit {r.returncode})")
    if r.returncode != 0 or "Update ok" not in r.stdout:
        st.pop("deploying", None)
        # crashed (e.g. pip) or the gate failed: never leave a new checkout behind the old running processes
        if git("rev-parse", "HEAD") != head:
            git("reset", "-q", "--hard", head)
        st["bad"] = target
        st["red_streak"] = st.get("red_streak", 0) + 1
        _save(st)
        log(f"rolled back: {subject} (exit {r.returncode}), red rounds in a row: {st['red_streak']}")
        if st["red_streak"] == 1:   # later red rounds stay in the log; round 3 names the tests once
            alert(f"↩️ Update nicht eingespielt: {subject}. Grund: Tests rot oder Update-Fehler, die laufende "
                  f"Version {head[:7]} bleibt aktiv, der Bot läuft normal weiter. Details: logs/update.log")
        if st["red_streak"] == 3:   # stuck: name the failing tests once
            failed = _failing_tests(target)
            st["red_tests"] = failed
            _save(st)
            log("gate red 3 rounds in a row, failing: " + "; ".join(failed))
            alert(f"🔴 Auto-Update hängt: Tests seit {st['red_streak']} Runden rot, der Mac bleibt auf {head[:7]}. "
                  "Rote Tests: " + ("; ".join(failed) or "nicht ermittelbar") + ".")
        return f"zurückgerollt {target[:7]}"
    # mayham.py update pulls the newest main, which may be past `target` when someone pushed during the backup
    now_head = git("rev-parse", "HEAD")
    if now_head != head and subprocess.run(["git", "merge-base", "--is-ancestor", target, now_head],
                                           cwd=ROOT).returncode == 0:
        target, subject = now_head, git("log", "-1", "--format=%h %s", now_head)[:90]
    if now_head != target:
        st.pop("deploying", None)
        st["bad"] = target
        _save(st)
        log(f"rolled back: {subject}")
        alert(f"↩️ Update zurückgerollt: {subject}. Grund: Tests rot oder Update-Fehler, alte Version "
              f"{head[:7]} läuft weiter. Details: logs/update.log")
        return f"zurückgerollt {target[:7]}"
    a = subprocess.run([_python(), "mayham.py", "install-autostart"], cwd=ROOT, capture_output=True, text=True,
                       timeout=300)
    log((a.stdout + a.stderr).strip()[-1000:])
    sizes = _log_sizes()
    ok, detail = (health or health_check)()
    locks = _lock_count(sizes)
    if not ok:
        # the new version starts but does not stay up: back to the previous commit and restart that
        git("reset", "-q", "--hard", head)
        subprocess.run([_python(), "-m", "pip", "install", "-q", "-r", "requirements.txt"], cwd=ROOT,
                       capture_output=True, timeout=900)
        subprocess.run([_python(), "mayham.py", "restart"], cwd=ROOT, capture_output=True, timeout=300)
        ok2, detail2 = (health or health_check)()
        st.pop("deploying", None)
        st["bad"] = target
        _save(st)
        log(f"rolled back after health check: {subject}: {detail}; old version: {detail2}")
        alert(f"↩️ Update zurückgerollt: {subject}. Grund: {detail}. Alte Version {head[:7]} läuft wieder"
              f"{'' if ok2 else ' NICHT sauber: ' + detail2}. Details: logs/update.log")
        return f"zurückgerollt {target[:7]} ({detail})"
    st.pop("deploying", None)
    st.pop("bad", None)
    st["red_streak"] = 0
    st.pop("red_tests", None)
    st["deployed"] = target
    _save(st)
    lock_note = f", {locks}× 'database is locked'" if locks else ""
    log(f"deployed {subject}: {detail}{lock_note}")
    alert(f"⬆️ Update eingespielt: {subject}, Tests grün, {detail}{lock_note}.")
    return f"eingespielt {target[:7]}"


def _once(st: dict, alert, key: str, msg: str) -> str:
    """Alert a waiting condition once per commit, not every 10 minutes."""
    if st.get("notified") != key:
        alert(msg)
        st["notified"] = key
        _save(st)
    return msg


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--status" in argv:
        env = ops.load_env()
        print(json.dumps({"state": _load(), "disabled": disabled(env), "live": live_reason(env)}, indent=1,
                         ensure_ascii=False))
        return 0
    lock = _paths()[3]
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("läuft schon")
            return 0
        try:
            _foreground()
            print(run_once())
        except Exception as e:  # noqa: BLE001
            log(f"Fehler: {e}")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
