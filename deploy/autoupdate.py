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
    if str(env.get("LIVE_TRADING", "")).strip().lower() in TRUE:
        return "LIVE_TRADING=true in .env"
    db = ops.db_path()
    if db.exists():
        try:
            c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
            n = c.execute("SELECT COUNT(*) FROM ct_positions WHERE mode='live' AND status='open'").fetchone()[0]
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


def health_check(window=None, interval=15) -> tuple[bool, str]:
    """After a restart: every service must be up and keep the same pid (no crash loop) for the last part of
    the window, and the dashboard must answer. Returns (ok, detail)."""
    window = window if window is not None else ops.cfg("AUTO_UPDATE_HEALTH_S", 180)
    names = _services_now()
    end = time.time() + window
    stable_since, last, detail = None, None, "noch nicht geprüft"
    while True:
        cur = _pids(names)
        down = [n for n, p in cur.items() if not p]
        changed = [n for n in names if last and cur[n] and last[n] and cur[n] != last[n]]
        if down or changed:
            stable_since = None
            detail = ", ".join([f"{n} läuft nicht" for n in down] + [f"{n} startet neu" for n in changed])
        elif stable_since is None:
            stable_since = time.time()
        last = cur
        if time.time() >= end:
            break
        time.sleep(interval)
    if stable_since is None or time.time() - stable_since < min(60, window / 2):
        return False, detail
    if not ops.check_dashboard():
        return False, "Dashboard antwortet nicht"
    return True, f"{len(names)} von {len(names)} Dienste laufen stabil"


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


def run_once(alert=ops.send_alert, now=None, health=None) -> str:
    env = ops.load_env()
    now = now or time.time()
    st = _load()
    why = disabled(env)
    if why:
        return f"aus ({why})"
    if not (ROOT / ".git").exists():
        return "kein git-Checkout"
    git("fetch", "-q", "origin", "main")
    head, target = git("rev-parse", "HEAD"), git("rev-parse", "origin/main")
    if head == target:
        return f"aktuell ({head[:7]})"
    subject = git("log", "-1", "--format=%h %s", target)[:90]
    if target == st.get("bad"):
        return f"übersprungen: {target[:7]} ist schon an den Tests gescheitert"
    if subprocess.run(["git", "merge-base", "--is-ancestor", head, target], cwd=ROOT).returncode != 0:
        return _once(st, alert, f"diverged:{head}", f"⚠️ Auto-Update: lokaler Stand {head[:7]} ist nicht Teil "
                     "von main, bitte von Hand prüfen.")
    if git("status", "--porcelain", "--untracked-files=no"):
        return _once(st, alert, f"dirty:{target}", "⚠️ Auto-Update wartet: lokale Änderungen im Checkout.")
    live = live_reason(env)
    if live:
        log(f"update pending: live trading on ({live}), {subject}")
        return _once(st, alert, f"live:{target}", f"⏸ Update {subject} wartet: {live}. "
                     "Einspielen von Hand mit `.venv/bin/python mayham.py update`.")
    gap = ops.cfg("AUTO_UPDATE_MIN", 10) * 60
    if now - st.get("last_deploy", 0) < gap:
        return "warte (höchstens ein Deploy pro 10 min)"

    # no deploy without a fresh, verified backup (integrity_check in ops.backup); mayham.py update ignores its failure
    b = subprocess.run([_python(), "deploy/ops.py", "backup"], cwd=ROOT, capture_output=True, text=True, timeout=900)
    if b.returncode != 0:
        log(f"backup failed, update pending: {(b.stdout + b.stderr).strip()[-500:]}")
        return _once(st, alert, f"backup:{target}", f"⏸ Update {subject} wartet: Backup vor dem Update ist "
                     "fehlgeschlagen. Details: logs/update.log")
    log(f"deploy {head[:7]} -> {subject}")
    st["last_deploy"] = now
    _save(st)
    r = subprocess.run([_python(), "mayham.py", "update"], cwd=ROOT, capture_output=True, text=True, timeout=1800)
    log((r.stdout + r.stderr).strip()[-4000:])
    if git("rev-parse", "HEAD") != target:
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
        st["bad"] = target
        _save(st)
        log(f"rolled back after health check: {subject}: {detail}; old version: {detail2}")
        alert(f"↩️ Update zurückgerollt: {subject}. Grund: {detail}. Alte Version {head[:7]} läuft wieder"
              f"{'' if ok2 else ' NICHT sauber: ' + detail2}. Details: logs/update.log")
        return f"zurückgerollt {target[:7]} ({detail})"
    st.pop("bad", None)
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
            print(run_once())
        except Exception as e:  # noqa: BLE001
            log(f"Fehler: {e}")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
