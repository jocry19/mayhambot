#!/usr/bin/env python3
"""mayhambot operations: health check, watchdog with Telegram alerts, backups, log rotation.

Standard library only, so it also runs when the app's venv is broken and on a bare Linux host.
Run from anywhere; paths are relative to the mayhambot folder (parent of deploy/).

  python deploy/ops.py status                 # human-readable overview (app, feed, backups, power)
  python deploy/ops.py health [--json]        # exit 0 = healthy, 1 = problem (Docker HEALTHCHECK)
  python deploy/ops.py watchdog               # one watchdog round: check, restart, alert, rotate logs
  python deploy/ops.py backup                 # consistent DB snapshot + config + models -> BACKUP_DIR
  python deploy/ops.py list-backups
  python deploy/ops.py restore <archive>      # stops the app, restores, starts it again
  python deploy/ops.py rotate-logs
  python deploy/ops.py setup-telegram         # after /start to your bot: finds + saves TELEGRAM_CHAT_ID, sends test
                                             # (chat id set: test only, no getUpdates; --neu searches again)
  python deploy/ops.py test-alert             # sends a Telegram test message
  python deploy/ops.py summary [--send]       # hourly status report (watchdog sends it every SUMMARY_EVERY_HOURS)
  python deploy/ops.py db-sizes               # MB and rows per table (also logged by every backup: logs/dbsize.log)
  python deploy/ops.py retention              # nightly cleanup (core/retention.py), RETENTION_DELETE=0 counts only

Settings (optional, in .env):
  BACKUP_DIR            where backups go (default: ~/mayhambot-backups); use a 2nd disk or cloud folder
  BACKUP_KEEP_HOURS     keep every backup this long (default 24), then one per day ...
  BACKUP_KEEP_DAYS      ... for this many days (default 14)
  BACKUP_MAX_GB         and never more than this in total, oldest go first, the newest 3 always stay (default 5)
  BACKUP_FULL_CHECK_HOUR  full integrity_check of the copy once a day from this hour on (default 3), quick_check
                          otherwise; BACKUP_FULL_CHECK_HOURS (30) at the latest
  BACKUP_GZIP_LEVEL     1 (fast, default) to 9 (smallest); level 6 took ~3x longer for ~5 % less size
  FEED_STALL_MIN        alert/restart when no new market data for this long (default 15)
  ALERT_REPEAT_HOURS    repeat an unresolved alert every N hours (default 6)
  WATCHDOG_RUNTIME      local (default) | docker   (docker: the host watches the container)
  WATCHDOG_RESTART_CMD  override the restart command
  SUMMARY_EVERY_HOURS   Telegram status report interval (default 1, 0 = off); includes Helius credits,
                        plus one alert at 80 % / 100 % of HELIUS_CREDITS_MONTH per month and key
  LOG_MAX_MB            rotate logs/*.log above this size (default 20), keeps 5 compressed
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import re
import os
import platform
import shutil
import signal
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IS_MAC = platform.system() == "Darwin"
APP_LABEL = "de.mayhambot.app"


def _services():
    """Service names from the launcher (mayham.py SERVICES), so new services are watched automatically."""
    try:
        sys.path.insert(0, str(ROOT))
        import mayham  # noqa: PLC0415 - stdlib-only module, no side effects on import
        return tuple(mayham.SERVICES)
    except Exception:  # noqa: BLE001
        return ("signals", "copytrade", "dashboard", "research")


SERVICES = _services()
CONFIRM_RUNS = 2          # a problem must be seen in 2 consecutive rounds before restart/alert
RESTART_COOLDOWN = 15 * 60
SECRET_KEYS = ("SOLANA_PRIVATE_KEY",)   # never written into backups


# ---------------------------------------------------------------- config
def load_env() -> dict:
    env = {}
    f = ROOT / ".env"
    if f.exists():
        for line in f.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = v.split(" #", 1)[0].strip().strip('"').strip("'")
            env[k.strip()] = v
    env.update({k: v for k, v in os.environ.items() if k in env or k.startswith(("BACKUP_", "WATCHDOG_",
                "FEED_", "ALERT_", "LOG_", "MAYHAM_", "TELEGRAM_", "DASHBOARD_"))})
    return env


ENV = load_env()
_BOOT_ENV = ENV  # send_alert re-reads .env only while ENV is the one loaded from disk (tests swap it)


def cfg(name, default):
    v = ENV.get(name)
    return type(default)(v) if v not in (None, "") else default


def db_path() -> Path:
    p = Path(os.path.expanduser(cfg("MAYHAM_DB_PATH", str(ROOT / "data" / "mayhambot.db"))))
    return p if p.is_absolute() else ROOT / p


def backup_dir() -> Path:
    return Path(os.path.expanduser(cfg("BACKUP_DIR", "~/mayhambot-backups")))


STATE_FILE = ROOT / "data" / "watchdog_state.json"


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_state(s: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, indent=1))
    tmp.replace(STATE_FILE)


def ago(ts) -> str:
    if not ts:
        return "nie"
    m = (time.time() - ts) / 60
    return f"vor {m:.0f} min" if m < 120 else f"vor {m / 60:.1f} h"


# ---------------------------------------------------------------- checks
def check_processes() -> dict:
    """Per service: alive? (pid files written by mayham.py)."""
    out = {}
    for name in SERVICES:
        f = ROOT / "data" / "pids" / f"{name}.pid"
        alive = False
        try:
            os.kill(int(f.read_text()), 0)
            alive = True
        except (OSError, ValueError):
            pass
        out[name] = alive
    return out


def dashboards() -> list[tuple[str, str]]:
    """(name, health URL) for each UI the launcher runs: FastAPI 'web' and/or the old Streamlit 'dashboard'."""
    out = []
    if "web" in SERVICES or not SERVICES:
        out.append(("web", f"http://127.0.0.1:{cfg('WEB_PORT', '8600')}/api/system"))
    if "dashboard" in SERVICES:
        out.append(("dashboard", f"http://127.0.0.1:{cfg('DASHBOARD_PORT', '8501')}/_stcore/health"))
    return out


def dashboard_url() -> str:
    return " / ".join(u.split("/api/")[0].split("/_stcore/")[0].replace("127.0.0.1", "localhost")
                      for _, u in dashboards())


def check_dashboard(timeout=10) -> bool:
    for _, url in dashboards():
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                if r.status != 200:
                    return False
        except Exception:  # noqa: BLE001
            return False
    return True


HEARTBEATS = {  # name -> SQL returning the newest unix ts of fresh data
    "Marktdaten": "SELECT MAX(ts) FROM token_prices",
    "Trades": "SELECT MAX(ts) FROM trades",
    "Copy-Trading-Polling": "SELECT MAX(last_polled) FROM ct_wallets WHERE active=1",
    "Sniper-Heartbeat": "SELECT json_extract(value, '$.ts') FROM sniper_state WHERE key='service_status'",
}


def check_feed() -> dict:
    """Newest timestamp per data feed, read-only. Missing table/DB = None (not yet started)."""
    out = {k: None for k in HEARTBEATS}
    p = db_path()
    if not p.exists():
        return out
    try:
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=10)
        for k, sql in HEARTBEATS.items():
            try:
                out[k] = conn.execute(sql).fetchone()[0]
            except sqlite3.Error:
                pass
        conn.close()
    except sqlite3.Error:
        pass
    return out


def check_power() -> str | None:
    if not IS_MAC:
        return None
    try:
        r = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True, timeout=5).stdout
        return "Netzteil" if "AC Power" in r else "Akku"
    except (OSError, subprocess.SubprocessError):
        return None


def launchd_loaded(label: str) -> bool:
    if not IS_MAC:
        return False
    r = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{label}"], capture_output=True, text=True)
    return r.returncode == 0


def last_backup() -> Path | None:
    files = sorted(backup_dir().glob("mayhambot-*.tar.gz"))
    return files[-1] if files else None


def collect() -> dict:
    """Run all checks and derive the list of problems (key -> German message)."""
    runtime = cfg("WATCHDOG_RUNTIME", "local")
    stall = cfg("FEED_STALL_MIN", 15) * 60
    procs = check_processes() if runtime == "local" else {}
    dash = check_dashboard()
    feed = check_feed()
    now = time.time()
    problems = {}
    dead = [n for n, ok in procs.items() if not ok]
    if runtime == "local" and len(dead) == len(SERVICES):
        problems["app_down"] = "mayhambot läuft nicht (keine Prozesse)."
    elif dead:
        problems["service_down"] = f"Teil-Ausfall: {', '.join(dead)} läuft nicht."
    if not dash and "app_down" not in problems:
        problems["dashboard_down"] = ("Dashboard antwortet nicht." if runtime == "local"
                                      else "mayhambot-Container antwortet nicht.")
    md = feed.get("Marktdaten")
    if md and now - md > stall:
        problems["feed_stalled"] = f"Keine neuen Marktdaten seit {(now - md) / 60:.0f} min (Internet/APIs?)."
    cp = feed.get("Copy-Trading-Polling")
    if cp and now - cp > stall:
        problems["copytrade_stalled"] = f"Copy-Trader pollt seit {(now - cp) / 60:.0f} min keine Wallets."
    sn = feed.get("Sniper-Heartbeat")
    if sn and "sniper" in SERVICES and now - float(sn) > 5 * 60:   # heartbeat every 10 s
        problems["sniper_stalled"] = f"Sniper-Heartbeat steht seit {(now - float(sn)) / 60:.0f} min."
    try:
        free_gb = shutil.disk_usage(db_path().parent if db_path().parent.exists() else ROOT).free / 1e9
        if free_gb < 2:
            problems["disk_low"] = f"Nur noch {free_gb:.1f} GB Speicher frei."
    except OSError:
        free_gb = None
    lb = last_backup()
    if db_path().exists() and (not lb or now - lb.stat().st_mtime > 3 * 3600):
        problems["backup_old"] = f"Letztes Backup: {ago(lb.stat().st_mtime) if lb else 'nie'}."
    try:
        problems.update(check_resources(procs))
    except Exception as e:  # noqa: BLE001 - a resource probe must never break the watchdog
        print(f"resources: {e}")
    power = check_power()
    if power == "Akku":
        problems["on_battery"] = "Mac läuft auf Akku: bitte Netzteil anschließen, sonst schläft er ein."
    return {"ts": now, "runtime": runtime, "processes": procs, "dashboard": dash, "feed": feed,
            "free_gb": free_gb, "last_backup": str(lb) if lb else None, "power": power, "problems": problems}


# ---------------------------------------------------------------- alerts
def send_alert(text: str) -> bool:
    """Telegram via the app's notifier when importable, else plain urllib (same bot/chat from .env)."""
    # .env is re-read here: the auto-updater runs for many minutes and the chat id may be set meanwhile
    fresh = load_env() if ENV is _BOOT_ENV else {}
    token = fresh.get("TELEGRAM_BOT_TOKEN") or cfg("TELEGRAM_BOT_TOKEN", "")
    chat = fresh.get("TELEGRAM_CHAT_ID") or cfg("TELEGRAM_CHAT_ID", "")
    print(f"[alert] {text}")
    if not (token and chat):
        print("[alert] nur Konsole (Telegram nicht eingerichtet)")
        return True   # nothing configured: console only, don't keep retrying
    text = f"🛠 <b>mayhambot Betrieb</b> ({platform.node()})\n{text}"
    ok = None
    try:
        sys.path.insert(0, str(ROOT))
        from signals.notifier import TelegramNotifier  # noqa: PLC0415
        ok = TelegramNotifier(token, chat).send(text) is not False
    except Exception:  # noqa: BLE001 - app venv missing/broken: fall back to stdlib
        pass
    if ok is None:
        try:
            data = urllib.parse.urlencode({"chat_id": chat, "text": text, "parse_mode": "HTML"}).encode()
            with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data, timeout=15) as r:
                ok = r.status == 200
        except Exception as e:  # noqa: BLE001 - the message names no URL, so the token never reaches a log
            print(f"[alert] telegram failed: {type(e).__name__}")
            ok = False
    print("[alert] Telegram zugestellt" if ok else "[alert] Telegram NICHT zugestellt")
    return ok


# ---------------------------------------------------------------- restart
def restart_app(reason: str) -> str:
    cmd = cfg("WATCHDOG_RESTART_CMD", "")
    if cmd:
        r = subprocess.run(cmd, shell=True, cwd=ROOT)
        return f"Neustart ({cmd}) -> {r.returncode}"
    if IS_MAC and launchd_loaded(APP_LABEL):
        r = subprocess.run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{APP_LABEL}"])
        return f"Neustart über launchd -> {r.returncode}"
    py = ROOT / ".venv" / "bin" / "python"
    py = str(py) if py.exists() else sys.executable
    subprocess.run([py, "mayham.py", "stop"], cwd=ROOT, capture_output=True)
    time.sleep(2)
    r = subprocess.run([py, "mayham.py", "start"], cwd=ROOT, capture_output=True)
    return f"Neustart über mayham.py start -> {r.returncode}"


def new_lock_errors(st: dict) -> int:
    """'database is locked' lines written to logs/*.log since the last watchdog run (8 services share SQLite)."""
    seen = st.setdefault("log_offsets", {})
    n = 0
    for f in (ROOT / "logs").glob("*.log"):
        try:
            size = f.stat().st_size
            start = seen.get(f.name, size)       # first sighting: count from now on
            with open(f, "rb") as fh:
                fh.seek(start if start <= size else 0)   # truncated by rotation -> from the start
                n += fh.read().count(b"database is locked")
            seen[f.name] = size
        except OSError:
            pass
    return n


def planned_restarts(since: float = 0.0) -> list[dict]:
    """Restarts a service asked for itself (exit code 3, noted by the supervisor in mayham.py), newest last,
    each with the reason from the service's own log ("restarting the ... service: <reason>")."""
    f = ROOT / "data" / "planned_restarts.jsonl"
    out = []
    try:
        lines = f.read_text().splitlines()[-50:]
    except OSError:
        return out
    for ln in lines:
        try:
            e = json.loads(ln)
        except ValueError:
            continue
        if e.get("ts", 0) > since:
            e["reason"] = _restart_reason(e.get("service", ""))
            out.append(e)
    return out


def _restart_reason(name: str) -> str:
    try:
        with open(ROOT / "logs" / f"{name}.log", "rb") as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - 65536))
            tail = fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return ""
    for ln in reversed(tail):
        if "restarting the " in ln and ": " in ln.split("restarting the ", 1)[1]:
            return ln.split("restarting the ", 1)[1].split(": ", 1)[1].strip()
    return ""


def planned_note(e: dict) -> str:
    why = "nach Modellwechsel" if "model" in e.get("reason", "") else "auf eigenen Wunsch"
    return f"{e.get('service', '?')}: geplanter Neustart {why}" + (f" ({e['reason']})" if e.get("reason") else "")


def check_planned_restarts(st: dict):
    """One info message per planned restart, so it never reads as a crash."""
    last = st.get("planned_seen", 0.0)
    for e in planned_restarts(last):
        send_alert(f"🔄 {planned_note(e)}. Kein Fehler, der Dienst läuft mit dem neuen Code weiter.")
        last = max(last, e["ts"])
    st["planned_seen"] = last


# ---------------------------------------------------------------- resources / maintenance
LOG_STALL_MIN = {"signals": 30, "brain": 120}   # services that log every cycle (brain: hourly "brain hour" line); silent this long = hung


def _rss_mb(pid: int) -> float | None:
    try:
        out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True, timeout=5).stdout
        return int(out.strip()) / 1024 if out.strip() else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _proc_age_s(pid: int) -> float | None:
    """Seconds since the process started (ps etime: [[dd-]hh:]mm:ss, same on macOS and Linux)."""
    try:
        out = subprocess.run(["ps", "-o", "etime=", "-p", str(pid)], capture_output=True, text=True,
                             timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    if not out:
        return None
    days, _, rest = out.rpartition("-")
    secs = 0
    for part in rest.split(":"):
        secs = secs * 60 + int(part)
    return secs + int(days or 0) * 86400


def wal_checkpoint(busy_ms: int = 2000) -> str:
    """Fold the WAL back and truncate it: PASSIVE first (never waits), then TRUNCATE, which waits for readers at
    most busy_ms while new writers queue behind it. 'ok …' when the WAL was emptied, else why not."""
    try:
        c = sqlite3.connect(str(db_path()), timeout=busy_ms / 1000, isolation_level=None)
    except sqlite3.Error as e:
        return f"nicht möglich ({e})"
    try:
        c.execute(f"PRAGMA busy_timeout={int(busy_ms)}")
        busy, log_pages, done = c.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
        try:
            tbusy, *_ = c.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        except sqlite3.OperationalError:
            tbusy = 1
        if not tbusy:
            return f"ok, {done} von {log_pages} Seiten übertragen und geleert"
        if log_pages >= 0 and done >= log_pages:
            # everything is in the database, only the file keeps its size until a TRUNCATE gets through:
            # nothing a restart would fix
            return f"ok, alle {log_pages} Seiten übertragen (Datei bleibt groß, bis sie geleert werden kann)"
        return f"{done} von {log_pages} Seiten übertragen, leeren nicht möglich (ein Leser hält noch eine alte Sicht)"
    except sqlite3.Error as e:
        return f"fehlgeschlagen ({e})"
    finally:
        c.close()


RUNAWAY: dict = {}   # service -> pid over MEM_RESTART_MB in the last check_resources()


def check_resources(procs: dict, now: float | None = None) -> dict:
    """Memory per service, a growing SQLite WAL, and services that are alive but stopped logging."""
    now = now or time.time()
    problems = {}
    soft, hard = cfg("MEM_WARN_MB", 1500.0), cfg("MEM_RESTART_MB", 3000.0)
    big, runaway = [], []
    RUNAWAY.clear()
    for name, alive in procs.items():
        if not alive:
            continue
        try:
            pid = int((ROOT / "data" / "pids" / f"{name}.pid").read_text())
        except (OSError, ValueError):
            continue
        mb = _rss_mb(pid)
        if mb is None:
            continue
        if mb > hard and name == "research" and training(now, cfg("MEM_TRAINING_MAX_MIN", 180)) \
                and mb <= cfg("MEM_TRAINING_HARD_MB", 10000.0):
            # the wallet-model retrain needs > 3 GB (60k wallets, 19.10.); restarting it every 17 min killed every
            # retrain for 2.5 days. While data/TRAINING is younger than MEM_TRAINING_MAX_MIN it may, up to
            # MEM_TRAINING_HARD_MB (10 GB: the Mac has 32 GB, 75 % free on 22.10.); past either research alone
            # restarts, which also ends an endless retrain
            big.append(f"{name} {mb:.0f} MB (trainiert)")
        elif mb > hard:
            runaway.append(f"{name} {mb:.0f} MB")
            RUNAWAY[name] = pid
        elif mb > soft:
            big.append(f"{name} {mb:.0f} MB")
    if runaway:
        problems["mem_runaway"] = f"Speicher läuft über: {', '.join(runaway)} (Grenze {hard:.0f} MB), Neustart."
    elif big:
        problems["mem_high"] = f"Hoher Speicherverbrauch: {', '.join(big)}."
    wal = Path(str(db_path()) + "-wal")
    try:
        wal_mb = wal.stat().st_size / 1e6
        if wal_mb > cfg("WAL_WARN_MB", 1000.0):
            problems["wal_big"] = (f"Datenbank-Journal (WAL) ist {wal_mb:.0f} MB groß: ein Leser hält den Checkpoint "
                                   "auf. Ich versuche einen Checkpoint.")
    except OSError:
        pass
    stalled = []
    for name, mins in LOG_STALL_MIN.items():
        f = ROOT / "logs" / f"{name}.log"
        if procs.get(name) and f.exists() and now - f.stat().st_mtime > mins * 60:
            stalled.append(f"{name} seit {(now - f.stat().st_mtime) / 60:.0f} min")
    if stalled:
        problems["service_hung"] = f"Dienst hängt (läuft, schreibt aber nichts): {', '.join(stalled)}."
    return problems


def nightly_maintenance(st: dict, now: float | None = None) -> str | None:
    """Once a day in the quiet hour (MAINT_HOUR, local time, default 4): let SQLite refresh its planner stats
    (PRAGMA optimize) and fold the WAL back into the database (passive checkpoint, never blocks writers).
    No VACUUM: it would lock the 8 writers out for minutes. Index damage is repaired by the backup."""
    now = now or time.time()
    lt = time.localtime(now)
    day = time.strftime("%Y-%m-%d", lt)
    if lt.tm_hour != cfg("MAINT_HOUR", 4) or st.get("maint_day") == day or not db_path().exists():
        return None
    st["maint_day"] = day
    t0 = time.time()
    c = sqlite3.connect(str(db_path()), timeout=30)
    try:
        c.execute("PRAGMA analysis_limit=1000")  # an older SQLite would ANALYZE whole tables under the write lock
        c.execute("PRAGMA optimize")
        busy, log_pages, done = c.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
    finally:
        c.close()
    msg = (f"Wartung {day}: optimize, Checkpoint {done}/{log_pages} Seiten"
           f"{' (Leser aktiv)' if busy else ''}, {time.time() - t0:.1f} s")
    print(msg)
    _spawn_retention()
    return msg


def _spawn_retention():
    """The retention pass runs detached (it may take minutes), so the watchdog's next rounds are not held up."""
    (ROOT / "logs").mkdir(exist_ok=True)
    with open(ROOT / "logs" / "maintenance.log", "a") as fh:
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "retention", "--db", str(db_path())],
                         cwd=str(ROOT), stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                         start_new_session=True)


def retention_run(args) -> int:
    """`ops.py retention`: the nightly cleanup (core/retention.py). Deletes by default (user approved 2026-10-17); RETENTION_DELETE=0 in .env counts only.
    Small batches with pauses, so the services keep writing; at most RETENTION_BUDGET_MIN minutes per night."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from core import retention  # noqa: PLC0415 - stdlib only
    path = Path(args.db) if getattr(args, "db", None) else db_path()
    if not path.exists():
        return 1
    delete = str(cfg("RETENTION_DELETE", "1")).lower() in ("1", "true", "on", "yes")
    c = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    t0 = time.time()
    try:
        c.execute("PRAGMA busy_timeout=30000")
        r = retention.cleanup(c, delete=delete, trades_days=cfg("RETENTION_TRADES_DAYS", 30),
                              prices_days=cfg("RETENTION_PRICES_DAYS", 7), budget_s=cfg("RETENTION_BUDGET_MIN", 15) * 60)
        fs = retention.file_stats(c)
    finally:
        c.close()
    verb = "gelöscht" if delete else "würde löschen (RETENTION_DELETE aus)"
    msg = (f"{time.strftime('%Y-%m-%d %H:%M:%S')} Aufräumen {verb}: trades {r.get('trades', 0)}, "
           f"Kurse {r.get('prices', 0)}, Wallets {r.get('wallets', 0)}; Datei {fs['bytes'] / 1e6:.0f} MB, "
           f"frei {fs['free_bytes'] / 1e6:.0f} MB, freigegeben {r.get('freed_mb', 0)} MB, {time.time() - t0:.0f} s")
    # the file only shrinks with incremental auto_vacuum; switching needs one VACUUM while no service runs
    if delete and fs["auto_vacuum"] != "incremental" and fs["free_bytes"] > max(200e6, 0.15 * fs["bytes"]):
        (path.parent / retention.COMPACT_FLAG).write_text(msg)
        msg += "; Verdichtung beim nächsten Neustart vorgemerkt"
    print(msg, flush=True)
    return 0


def ops_report() -> str:
    """Answer to the Telegram command /betrieb: services, updater, backups, disk, open problems."""
    lines = [build_summary().split("\n", 1)[1]]
    try:
        stf = ROOT / "data" / "autoupdate_state.json"
        au = json.loads(stf.read_text()) if stf.exists() else {}
        head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True,
                              timeout=10).stdout.strip()
        upd = f"Version {head or '?'}"
        if au.get("red_streak"):
            upd += f", Tests seit {au['red_streak']} Runde(n) rot"
        if au.get("blocked"):
            upd += ", " + au["blocked"]
        if (ROOT / "data" / "NO_AUTO_UPDATE").exists() or str(cfg("AUTO_UPDATE", "1")).lower() in ("0", "false", "off"):
            upd += ", Auto-Update AUS"
        last = ""
        lf = ROOT / "logs" / "update.log"
        if lf.exists():
            for ln in reversed(lf.read_text(errors="replace").splitlines()[-300:]):
                if ln[:4].isdigit() and (" deployed " in ln or " rolled back" in ln):
                    last = ln[:120]
                    break
        lines.append(f"Update: {upd}" + (f"\nLetzte Runde: {last}" if last else ""))
    except Exception as e:  # noqa: BLE001
        lines.append(f"Update-Status nicht lesbar ({type(e).__name__})")
    lb = last_backup()
    lines.append(f"Backup: {ago(lb.stat().st_mtime) if lb else 'keins'}"
                 + (f", {lb.stat().st_size / 1e6:.0f} MB" if lb else ""))
    try:
        size = db_path().stat().st_size / 1e6 if db_path().exists() else 0
        lines.append(f"Datenbank: {size:.0f} MB · frei: {shutil.disk_usage(ROOT).free / 1e9:.0f} GB")
    except OSError:
        pass
    try:
        last = json.loads((ROOT / "data" / "db_sizes.json").read_text())[-1]["tables"]
        big = sorted(last.items(), key=lambda kv: -kv[1][1])[:3]
        lines.append("Größte Tabellen: " + ", ".join(f"{t} {v[1] / 1e6:.0f} MB" for t, v in big))
    except (OSError, ValueError, LookupError):
        pass
    return "🛠 <b>Betrieb</b>\n" + "\n".join(lines)


COMPACTING = "COMPACTING"   # written by mayham.py next to the DB while the one-time VACUUM runs before the services


def training(now: float | None = None, max_min: float | None = None) -> bool:
    """True while the research service retrains the wallet model (data/TRAINING, written by model/train.py; a
    marker older than TRAINING_MAX_MIN (90, or max_min) is stale: the process died without removing it)."""
    f = db_path().parent / "TRAINING"
    max_min = cfg("TRAINING_MAX_MIN", 90) if max_min is None else max_min
    try:
        return 0 <= (now or time.time()) - f.stat().st_mtime < max_min * 60
    except OSError:
        return False


def compacting(now: float | None = None) -> bool:
    """True while the supervisor compacts the database before starting the services (no service is up then)."""
    f = db_path().parent / COMPACTING
    try:
        return 0 <= (now or time.time()) - f.stat().st_mtime < cfg("COMPACT_MAX_MIN", 120) * 60
    except OSError:
        return False


def supervised() -> bool:
    """The launchd agent runs `mayham.py run`, which restarts a crashed service on its own (with backoff)."""
    return IS_MAC and launchd_loaded(APP_LABEL)


def update_in_progress(now: float | None = None) -> bool:
    """True while an auto-update round is between its pull/gate and the end of its health check."""
    try:
        d = json.loads((ROOT / "data" / "autoupdate_state.json").read_text()).get("deploying") or {}
    except (OSError, ValueError):
        return False
    limit = (cfg("AUTO_UPDATE_TIMEOUT_MIN", 45) + 10) * 60
    return bool(d.get("at")) and 0 <= (now or time.time()) - d["at"] < limit


def watchdog(args) -> int:
    st = load_state()
    now = time.time()
    # gap detection: the watchdog runs every ~2 min, a long gap means the Mac slept or was off
    last_run = st.get("last_run")
    gap_min = (now - last_run) / 60 if last_run else 0
    if gap_min > cfg("GAP_ALERT_MIN", 10):
        send_alert(f"⏸ {gap_min:.0f} min Pause erkannt (Mac schlief oder war aus). Läuft wieder.")
    st["last_run"] = now

    rotate_logs(quiet=True)
    r = collect()
    if compacting(now):
        # services wait for the VACUUM; a restart now would kill it half way and start it over on the next start
        for k in ("app_down", "service_down", "dashboard_down", "feed_stalled", "copytrade_stalled",
                  "sniper_stalled", "service_hung"):
            r["problems"].pop(k, None)
    locks = new_lock_errors(st)
    if locks > cfg("LOCK_ALERT_PER_RUN", 20):
        r["problems"]["db_locked"] = f"Datenbank überlastet: {locks}× 'database is locked' in 2 min."
    seen = st.setdefault("problems", {})     # key -> {first, count, alerted, msg}
    repeat = cfg("ALERT_REPEAT_HOURS", 6) * 3600
    restartable = {"app_down", "service_down", "dashboard_down", "feed_stalled", "copytrade_stalled",
                   "sniper_stalled", "service_hung", "mem_runaway", "wal_big"}
    need_restart = []
    if "wal_big" in r["problems"]:
        # a restart did not shrink the WAL (2366 MB right after the 11:06 restart, 22.10.) but stopped every
        # service 3 times an hour: checkpoint it instead; restart only if that fails for WAL_RESTART_HOURS (6)
        res = wal_checkpoint() if not training(now, cfg("MEM_TRAINING_MAX_MIN", 180)) else "Training läuft, später"
        r["problems"]["wal_big"] += f" Checkpoint: {res}."
        if res.startswith("ok"):
            r["problems"].pop("wal_big")

    for key, msg in r["problems"].items():
        p = seen.setdefault(key, {"first": now, "count": 0, "alerted": 0})
        p["count"] += 1
        p["msg"] = msg
        if p["count"] < CONFIRM_RUNS:
            continue
        if key == "wal_big" and (now - p["first"] < cfg("WAL_RESTART_HOURS", 6) * 3600
                                 or training(now, cfg("MEM_TRAINING_MAX_MIN", 180))):
            pass   # checkpoints first (above); a whole-app restart only after hours of failed ones
        elif key in ("wal_big", "mem_runaway") and now - st.setdefault("app_restart_by", {}).get(key, 0) \
                < cfg("SAME_REASON_RESTART_HOURS", 6) * 3600 and not (key == "mem_runaway" and supervised()):
            pass   # at most one whole-app restart per 6 h for the same slow reason; the alert below still goes
        elif key in restartable and not (key == "service_down" and supervised()
                                         and now - p["first"] < cfg("SERVICE_DOWN_RESTART_MIN", 15) * 60):
            # one service in its crash backoff is the supervisor's job; restarting the whole app for it would
            # stop the 9 healthy ones too. Only when it stays down past the backoff does the watchdog step in.
            need_restart.append(key)
        if not p["alerted"] or now - p["alerted"] > repeat:
            if send_alert(f"🔴 {msg}"):
                p["alerted"] = now

    action = None
    recent = [t for t in st.get("restarts", []) if now - t < 3600]
    st["restarts"] = recent
    if need_restart and update_in_progress(now):
        need_restart = []   # the updater restarts the app itself; a second restart mid-round would race it
    if "mem_runaway" in need_restart and RUNAWAY and supervised():
        # memory: only the service that grew, the supervisor (mayham.py run) starts it again after 10 s; a whole-
        # app restart stopped brain, copytrade, signals and sniper every 17 min for research's retrain (19.10.-22.10.)
        need_restart.remove("mem_runaway")
        done = st.setdefault("svc_restarts", {})
        for name, pid in RUNAWAY.items():
            if now - done.get(name, 0) <= RESTART_COOLDOWN or args.no_restart:
                continue
            try:
                os.kill(pid, signal.SIGTERM)
                done[name] = now
                print(f"Speicher: nur {name} neu gestartet (pid {pid})")
                send_alert(f"🔁 Nur {name} neu gestartet wegen: {seen['mem_runaway']['msg']} Die anderen Dienste "
                           "laufen weiter.")
            except OSError as e:
                print(f"Speicher: {name} nicht beendet: {e}")
    if need_restart and not args.no_restart and now - st.get("last_restart", 0) > RESTART_COOLDOWN:
        if len(recent) >= cfg("MAX_RESTARTS_PER_HOUR", 3):
            # a real defect must not turn into a restart loop: alarm once per hour, leave the bot as it is
            if now - st.get("loop_alerted", 0) > 3600:
                send_alert(f"🛑 {len(recent)} Neustarts in der letzten Stunde, weitere setze ich aus. Problem: "
                           f"{', '.join(seen[k]['msg'] for k in need_restart)}")
                st["loop_alerted"] = now
        else:
            action = restart_app(", ".join(need_restart))
            for k in need_restart:
                if k in ("wal_big", "mem_runaway"):
                    st.setdefault("app_restart_by", {})[k] = now
            st["last_restart"] = now
            st["restarts"] = recent + [now]
            send_alert(f"🔁 {action} wegen: {', '.join(seen[k]['msg'] for k in need_restart)}")

    for key in list(seen):
        if key not in r["problems"]:
            if seen[key].get("alerted"):
                send_alert(f"🟢 Wieder OK (war: {seen[key].get('msg', key)})")
            del seen[key]

    try:
        m = nightly_maintenance(st)
        if m:
            log_line = ROOT / "logs" / "maintenance.log"
            with open(log_line, "a") as fh:
                fh.write(time.strftime("%Y-%m-%d %H:%M:%S ") + m + "\n")
    except Exception as e:  # noqa: BLE001
        print(f"maintenance: {e}")
    try:
        check_planned_restarts(st)
    except Exception as e:  # noqa: BLE001
        print(f"planned restarts: {e}")
    try:
        check_credit_alerts(st)
        check_service_budgets(st)
    except Exception as e:  # noqa: BLE001
        print(f"credit alerts: {e}")

    every = cfg("SUMMARY_EVERY_HOURS", 1.0) * 3600
    if every > 0 and now - st.get("last_summary", 0) >= every:
        if send_alert(build_summary()):
            st["last_summary"] = now

    save_state(st)
    print(json.dumps({"problems": r["problems"], "action": action}, ensure_ascii=False))
    return 0


# ---------------------------------------------------------------- backup
INDEX_ONLY = ("missing from index", "wrong # of entries in index", "non-unique entry in index")


def _index_only(problems) -> bool:
    """True when integrity_check found only broken indexes (rebuildable from the tables, no data lost)."""
    return bool(problems) and all(any(k in p for k in INDEX_ONLY) for p in problems)


def _broken_indexes(problems) -> list[str]:
    """Index names from integrity_check lines like 'row 5 missing from index idx_x'."""
    names = []
    for p in problems:
        m = re.search(r"\bindex (\S+)\s*$", p)
        if m and m.group(1) not in names:
            names.append(m.group(1))
    return names


def _repair_indexes(path: Path, timeout: float = 60, names=None, check=True) -> list[str]:
    """REINDEX (all, or only the named indexes one at a time: each holds the write lock just for its own rebuild,
    not one lock across every index of the live database), then re-check unless check=False."""
    c = sqlite3.connect(path, timeout=timeout, isolation_level=None)
    try:
        c.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
        for stmt in ([f'REINDEX "{n}"' for n in names] if names else ["REINDEX"]):
            c.execute(stmt)
        if not check:
            return []
        return [r[0] for r in c.execute("PRAGMA integrity_check").fetchall() if r[0] != "ok"]
    finally:
        c.close()


def _full_check_due(now: float | None = None) -> bool:
    """The full integrity_check reads the whole copy once more: 1707 s of the 04:37 backup on 19.10. (quick_check
    4 to 311 s). It runs once a day in the first backup from BACKUP_FULL_CHECK_HOUR (3, local time) on, or when the
    last one is more than BACKUP_FULL_CHECK_HOURS (30) old; quick_check in between."""
    now = now or time.time()
    try:
        last = float(json.loads((ROOT / "data" / "backup_check.json").read_text()).get("full_check_at", 0))
    except (OSError, ValueError, AttributeError):
        last = 0.0
    age_h = (now - last) / 3600
    if age_h >= cfg("BACKUP_FULL_CHECK_HOURS", 30.0):
        return True
    return age_h >= 12 and time.localtime(now).tm_hour >= cfg("BACKUP_FULL_CHECK_HOUR", 3) \
        and time.localtime(last).tm_yday != time.localtime(now).tm_yday


def _snapshot_db(src: Path, dst: Path, phases: dict | None = None, full: bool = True):
    """Online-consistent copy via SQLite's backup API (safe while the app writes).

    Broken indexes (10/2026: rows missing from idx_trades_wallet_ts and ct_swaps' key) are rebuilt in the copy
    and, natively on this machine, in the live database (user's choice); damaged tables still abort.
    phases: filled with the seconds of the copy and the check. full=False: quick_check (no index comparison)."""
    phases = {} if phases is None else phases
    t0 = time.time()
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=60)
    d = sqlite3.connect(dst)
    # one step: a paged backup restarts on every write from another connection and may never finish;
    # in WAL mode a single-step read does not block the writers
    s.backup(d)
    s.close()
    phases["Kopie"] = time.time() - t0
    t0 = time.time()
    # full integrity_check: quick_check does not compare indexes with their tables on every SQLite version
    check = "integrity_check" if full else "quick_check"
    problems = [r[0] for r in d.execute(f"PRAGMA {check}").fetchall() if r[0] != "ok"]
    phases["Prüfung voll" if full else "Prüfung kurz"] = time.time() - t0
    ok = problems[0] if problems else "ok"
    if ok != "ok":
        d.close()
        if not _index_only(problems):
            raise RuntimeError(f"Integritätsprüfung der Kopie fehlgeschlagen: {ok}")
        left = _repair_indexes(dst)
        if left:
            raise RuntimeError(f"Integritätsprüfung der Kopie fehlgeschlagen nach REINDEX: {left[0]}")
        # live: only the broken indexes, one by one, and no full integrity_check on the live file (the next
        # hourly backup checks its copy again and repeats this if something is still off)
        names = _broken_indexes(problems)
        _repair_indexes(src, names=names or None, check=False)
        msg = (f"🛠️ Datenbank: {len(problems)} kaputte Index-Einträge neu aufgebaut "
               f"({', '.join(names) or 'alle Indizes'}; Live-DB repariert, das nächste Backup prüft nach), "
               "keine Daten verloren.")
        print(msg)
        try:
            send_alert(msg)
        except Exception:  # noqa: BLE001
            pass
        d = sqlite3.connect(dst)
    d.execute("PRAGMA journal_mode=DELETE")
    d.close()


def log_db_sizes(path: Path, profile=False, every_h: float | None = None) -> str | None:
    """Size per table (on the backup's own copy, so the live file is not read twice) into data/db_sizes.json and
    one block in logs/dbsize.log, at most every DB_SIZES_EVERY_HOURS (3) from the backup, with 30 min slack: the
    backups come about hourly with jitter, and a strict 6 h gate missed the 19:04 one by 4 minutes (18.10.), so the
    file went 7 h without an entry. Never fails the backup.
    profile: also the trades breakdown (minutes on the Mac's 640k rows, so only on demand)."""
    log = ROOT / "logs" / "dbsize.log"
    every_h = cfg("DB_SIZES_EVERY_HOURS", 3.0) if every_h is None else every_h
    if every_h and log.exists() and time.time() - log.stat().st_mtime < every_h * 3600 - 1800:
        return None
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from core import retention  # noqa: PLC0415 - stdlib only
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
        try:
            sizes, fstats, grow = retention.log_sizes(c, ROOT / "data" / "db_sizes.json")
            text = retention.size_report(sizes, fstats, grow)
            if "trades" in sizes:   # who writes the growth: newest rows by insertion order (cheap, rowid)
                rec = c.execute("SELECT COALESCE(source, ''), COUNT(*) FROM (SELECT source FROM trades ORDER BY rowid "
                                "DESC LIMIT 100000) GROUP BY 1 ORDER BY 2 DESC").fetchall()
                text += "\ntrades, neueste 100k Zeilen nach Quelle: " + ", ".join(f"{s or '?'} {n}" for s, n in rec)
            if profile and "trades" in sizes:
                text += "\n" + retention.profile_report(retention.trades_profile(c))
        finally:
            c.close()
        (ROOT / "logs").mkdir(exist_ok=True)
        with open(log, "a") as fh:
            fh.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')}\n{text}\n")
        return text
    except Exception as e:  # noqa: BLE001
        print(f"db sizes: {type(e).__name__}: {e}")
        return None


def db_sizes(args) -> int:
    """`ops.py db-sizes`: the table sizes, measured on a fresh snapshot (same as the hourly backup does)."""
    if not db_path().exists():
        print("keine Datenbank")
        return 1
    with tempfile.TemporaryDirectory() as td:
        snap = Path(td) / "mayhambot.db"
        _snapshot_db(db_path(), snap)
        text = log_db_sizes(snap, profile=args.profile, every_h=0)
    print(text or "Größen nicht lesbar")
    return 0 if text else 1


def _redacted_env() -> str:
    lines = []
    for line in (ROOT / ".env").read_text().splitlines():
        k = line.split("=", 1)[0].strip()
        lines.append(f"{k}=<entfernt: nicht im Backup>" if k in SECRET_KEYS and "=" in line
                     and line.split("=", 1)[1].strip() else line)
    return "\n".join(lines) + "\n"


def leave_background_band():
    """launchd's ProcessType Background throttles CPU, I/O and timers (the hourly backup of a 178 MB database took
    18 min). Leave the Darwin background band for this process; children inherit it. No-op off macOS."""
    if not IS_MAC:
        return
    try:
        os.setpriority(getattr(os, "PRIO_DARWIN_PROCESS", 4), 0, 0)
    except (OSError, AttributeError):
        pass


def _mark_full_check():
    """In a file of its own: the watchdog rewrites its state file from a copy it read earlier."""
    try:
        (ROOT / "data").mkdir(parents=True, exist_ok=True)
        (ROOT / "data" / "backup_check.json").write_text(json.dumps({"full_check_at": int(time.time())}))
    except OSError:  # a missed mark only means the next backup checks fully again
        pass


def backup(args) -> int:
    t0 = time.time()
    phases = {}
    leave_background_band()
    out = backup_dir()
    out.mkdir(parents=True, exist_ok=True)
    os.chmod(out, 0o700)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    final = out / f"mayhambot-{stamp}.tar.gz"
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        tar_tmp = td / "b.tar.gz"
        with tarfile.open(tar_tmp, "w:gz", compresslevel=cfg("BACKUP_GZIP_LEVEL", 1)) as tar:
            src = db_path()
            if src.exists():
                snap = td / "mayhambot.db"
                full = _full_check_due()
                _snapshot_db(src, snap, phases, full=full)
                if full:
                    _mark_full_check()
                t1 = time.time()
                if log_db_sizes(snap):
                    phases["Größen"] = time.time() - t1
                tar.add(snap, "data/mayhambot.db")
            if (ROOT / ".env").exists():
                e = td / "env"
                e.write_text(_redacted_env())
                tar.add(e, ".env")
            # everything else in data/ (models, caches, state), minus live DB files and pids
            data = ROOT / "data"
            for f in sorted(data.rglob("*")) if data.exists() else []:
                rel = f.relative_to(ROOT)
                if (not f.is_file() or "pids" in rel.parts or f.name.startswith(src.name)
                        or ".db" in f.name or f.suffix in (".tmp",)):
                    continue
                tar.add(f, str(rel))
            for extra in ("copytrade/wallets.txt",):
                if (ROOT / extra).exists():
                    tar.add(ROOT / extra, extra)
            meta = td / "BACKUP.json"
            meta.write_text(json.dumps({"created": stamp, "host": platform.node(), "db": str(src)}))
            tar.add(meta, "BACKUP.json")
        shutil.move(str(tar_tmp), final)
    os.chmod(final, 0o600)
    removed = prune_backups(out)
    kept = list(out.glob("mayhambot-*.tar.gz"))
    phases["Packen"] = time.time() - t0 - sum(phases.values())
    split = ", ".join(f"{k} {v:.0f} s" for k, v in phases.items())
    print(f"Backup: {final} ({final.stat().st_size / 1e6:.1f} MB, {time.time() - t0:.0f} s: {split}), {removed} alte "
          f"entfernt, {len(kept)} Backups mit {sum(f.stat().st_size for f in kept) / 1e9:.2f} GB")
    return 0


def prune_backups(out: Path) -> int:
    """Everything of the last BACKUP_KEEP_HOURS (6), then one per BACKUP_KEEP_EVERY_HOURS (4) up to a day, then one
    per day for BACKUP_KEEP_DAYS (14); the total stays under BACKUP_MAX_GB (8) and leaves BACKUP_MIN_FREE_GB (20)
    free on the disk. Thinning the hours first keeps the daily history when the database (and every backup) grows."""
    keep_h, every_h = cfg("BACKUP_KEEP_HOURS", 6), cfg("BACKUP_KEEP_EVERY_HOURS", 4)
    keep_d = cfg("BACKUP_KEEP_DAYS", 14)
    now = time.time()
    slots = set()
    removed = 0
    kept = []
    for f in sorted(out.glob("mayhambot-*.tar.gz"), reverse=True):   # newest first
        mtime = f.stat().st_mtime
        age = now - mtime
        if age < keep_h * 3600:
            slot = None
        elif age < 86400:
            # fixed clock windows, not age windows: an age window moves every hour, so each new backup crossing
            # into it pushed out the one already there and nothing ever lived to become a daily backup
            slot = ("h", int(mtime // (every_h * 3600)))
        elif age < keep_d * 86400:
            slot = ("d", f.name[10:18])
        else:
            slot = "drop"
        if slot != "drop" and (slot is None or slot not in slots):
            slots.add(slot)
            kept.append(f)
            continue
        f.unlink()
        removed += 1
    # the database grows: cap the total, dropping the oldest first but never the newest 3
    total = sum(f.stat().st_size for f in kept)
    try:
        room = shutil.disk_usage(out).free + total - cfg("BACKUP_MIN_FREE_GB", 20.0) * 1e9
    except OSError:
        room = float("inf")
    max_bytes = min(cfg("BACKUP_MAX_GB", 8.0) * 1e9, room)
    for f in reversed(kept[3:]):
        if total <= max_bytes:
            break
        total -= f.stat().st_size
        f.unlink()
        removed += 1
    return removed


def list_backups(args) -> int:
    for f in sorted(backup_dir().glob("mayhambot-*.tar.gz")):
        print(f"{f}  {f.stat().st_size / 1e6:7.1f} MB  {ago(f.stat().st_mtime)}")
    return 0


def restore(args) -> int:
    arc = Path(args.archive).expanduser()
    if not arc.exists():
        arc = backup_dir() / args.archive
    py = ROOT / ".venv" / "bin" / "python"
    py = str(py) if py.exists() else sys.executable
    managed = IS_MAC and launchd_loaded(APP_LABEL)
    if not args.no_stop:
        if managed:
            subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{APP_LABEL}"])
        subprocess.run([py, "mayham.py", "stop"], cwd=ROOT)
        time.sleep(3)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = db_path()
    with tempfile.TemporaryDirectory() as td, tarfile.open(arc) as tar:
        try:
            tar.extractall(td, filter="data")
        except TypeError:  # Python without extraction filters (macOS system 3.9)
            if any(m.name.startswith(("/", "..")) or ".." in Path(m.name).parts for m in tar.getmembers()):
                raise SystemExit("Archiv enthält unsichere Pfade, Abbruch")
            tar.extractall(td)
        td = Path(td)
        if (td / "data" / "mayhambot.db").exists():
            c = sqlite3.connect(td / "data" / "mayhambot.db")
            chk = c.execute("PRAGMA integrity_check").fetchone()[0]
            c.close()
            if chk != "ok":
                raise SystemExit(f"DB im Backup ist beschädigt ({chk}), nichts geändert")
            dst.parent.mkdir(parents=True, exist_ok=True)
            for suffix in ("", "-wal", "-shm"):
                old = Path(str(dst) + suffix)
                if old.exists():
                    old.rename(f"{old}.vor-restore-{stamp}")
            shutil.copy2(td / "data" / "mayhambot.db", dst)
        for f in (td / "data").rglob("*") if (td / "data").exists() else []:
            # the Helius credit ledger and rate-limit state must never run backwards (monthly quota)
            if f.is_file() and f.name != "mayhambot.db" and "ratelimit" not in f.relative_to(td).parts:
                t = ROOT / f.relative_to(td)
                t.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, t)
        if (td / ".env").exists() and (args.with_env or not (ROOT / ".env").exists()):
            if (ROOT / ".env").exists():
                shutil.copy2(ROOT / ".env", ROOT / f".env.vor-restore-{stamp}")
            shutil.copy2(td / ".env", ROOT / ".env")
            os.chmod(ROOT / ".env", 0o600)
            print(".env wiederhergestellt (SOLANA_PRIVATE_KEY war nicht im Backup, ggf. neu eintragen)")
    print(f"Wiederhergestellt aus {arc}; alte DB liegt als *.vor-restore-{stamp} daneben")
    if not args.no_stop:
        if managed:
            plist = Path.home() / "Library" / "LaunchAgents" / f"{APP_LABEL}.plist"
            for _ in range(3):   # bootstrap right after bootout can fail with "5: Bad request"
                if subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)]).returncode == 0:
                    break
                time.sleep(2)
        else:
            subprocess.run([py, "mayham.py", "start"], cwd=ROOT)
    return 0


# ---------------------------------------------------------------- logs
def rotate_logs(args=None, quiet=False) -> int:
    """copytruncate: the app keeps its O_APPEND handles, so copy + truncate is safe without a restart."""
    limit = cfg("LOG_MAX_MB", 20) * 1e6
    for f in (ROOT / "logs").glob("*.log"):
        try:
            if f.stat().st_size < limit:
                continue
            for i in range(4, 0, -1):
                a = f.with_name(f"{f.name}.{i}.gz")
                if a.exists():
                    a.replace(f.with_name(f"{f.name}.{i + 1}.gz"))
            with open(f, "rb") as src, gzip.open(f.with_name(f"{f.name}.1.gz"), "wb") as dst:
                shutil.copyfileobj(src, dst)
            with open(f, "r+b") as h:
                h.truncate(0)
            f.with_name(f"{f.name}.6.gz").unlink(missing_ok=True)
            if not quiet:
                print(f"rotiert: {f.name}")
        except OSError as e:
            print(f"rotate {f.name}: {e}")
    return 0


# ---------------------------------------------------------------- telegram setup
def _set_env(key: str, value: str):
    f = ROOT / ".env"
    lines = f.read_text().splitlines() if f.exists() else []
    out, done = [], False
    for line in lines:   # replace the first occurrence, drop duplicates (later lines would win in dotenv)
        if line.split("=", 1)[0].strip() == key:
            if not done:
                out.append(f"{key}={value}")
                done = True
            continue
        out.append(line)
    lines = out if done else out + [f"{key}={value}"]
    f.write_text("\n".join(lines) + "\n")
    os.chmod(f, 0o600)


def setup_telegram(args) -> int:
    """Finds the chat id from the bot's updates (after you sent /start), writes it to .env, sends a test.
    The token is read from .env and never printed."""
    token = cfg("TELEGRAM_BOT_TOKEN", "")
    if not token:
        print("TELEGRAM_BOT_TOKEN fehlt in .env (Token von @BotFather eintragen).")
        return 1
    if cfg("TELEGRAM_CHAT_ID", "") and not getattr(args, "neu", False):
        # the live service polls getUpdates for /notaus etc.; Telegram allows one poller per bot (409 Conflict),
        # so with a chat id already set we only send the test and never call getUpdates
        ok = send_alert("Telegram-Test ✅ Chat-ID ist eingetragen, Meldungen kommen hier an.")
        print("Chat-ID ist schon eingetragen. " + ("Testnachricht gesendet." if ok else "Senden fehlgeschlagen.")
              + " Neue Chat-ID suchen: setup-telegram --neu (vorher die App stoppen).")
        return 0 if ok else 1
    api = f"https://api.telegram.org/bot{token}"
    try:
        with urllib.request.urlopen(f"{api}/getMe", timeout=15) as r:
            me = json.loads(r.read())["result"]
        with urllib.request.urlopen(f"{api}/getUpdates", timeout=15) as r:
            ups = json.loads(r.read()).get("result", [])
    except urllib.error.HTTPError as e:  # noqa: BLE001 - never echo the URL (contains the token)
        print("Ein anderer Prozess liest gerade die Bot-Nachrichten (der live-Dienst). Erst die App stoppen: "
              ".venv/bin/python mayham.py stop" if e.code == 409 else f"Telegram-Fehler HTTP {e.code}.")
        return 1
    except Exception as e:  # noqa: BLE001 - never echo the URL (contains the token)
        print(f"Telegram nicht erreichbar oder Token ungültig ({type(e).__name__}).")
        return 1
    chats = {}
    for u in ups:
        m = u.get("message") or u.get("edited_message") or {}
        c = m.get("chat") or {}
        if c.get("type") == "private":
            chats[c["id"]] = c.get("first_name", "")
    print(f"Bot: @{me.get('username')}")
    if not chats:
        print(f"Noch keine Nachricht an den Bot gefunden: schick @{me.get('username')} in Telegram /start "
              "und starte diesen Befehl dann nochmal.")
        return 1
    chat_id = list(chats)[-1]
    _set_env("TELEGRAM_CHAT_ID", str(chat_id))
    ENV["TELEGRAM_CHAT_ID"] = str(chat_id)
    print(f"Chat-ID gefunden ({chats[chat_id]}) und in .env eingetragen.")
    ok = send_alert("Telegram eingerichtet ✅ Ab jetzt kommen hier Signale, Alarme und der stündliche Bericht.")
    if ok:
        send_alert(build_summary())
    print("Testnachricht gesendet." if ok else "Senden fehlgeschlagen.")
    print("Damit auch die Signale über Telegram kommen, die App neu starten: .venv/bin/python mayham.py restart")
    return 0 if ok else 1


# ---------------------------------------------------------------- helius credits
def helius_credits() -> dict | None:
    """Month/day totals of the shared Helius credit ledger (core.http), None when there is no ledger yet."""
    import hashlib  # noqa: PLC0415
    month = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m")
    stats, led, status, pace, limit_on = None, {}, None, None, False
    try:
        sys.path.insert(0, str(ROOT))
        from core import http as core_http  # noqa: PLC0415
        stats = core_http.credits()
        if hasattr(core_http, "credits_status"):
            status = core_http.credits_status()
        if hasattr(core_http, "plan_pace"):
            pace = core_http.plan_pace().get("ratio")
        limit_on = getattr(core_http, "daily_limit_on", lambda: True)()
    except Exception:  # noqa: BLE001 - app venv missing: read the ledger file directly
        pass
    f = ROOT / "data" / "ratelimit" / "credits-helius.json"
    try:
        led = json.loads(f.read_text() or "{}")
    except (OSError, ValueError):
        led = {}
    if not stats and led.get("month") != month:
        return None
    limit = float(cfg("HELIUS_CREDITS_MONTH", 1_000_000.0))
    if stats:
        any_st = next(iter(stats.values()), {})
        month_used = any_st.get("credits_month_all", sum(v.get("credits_month", 0) for v in stats.values()))
        limit = any_st.get("credits_month_limit") or limit
        today = sum(v.get("credits_today", 0) for v in stats.values())
        budget = sum(v["credits_day_budget"] for v in stats.values() if v.get("credits_day_budget") is not None)
    else:
        svcs = led.get("services", {}).values()
        day = dt.datetime.now(dt.timezone.utc).strftime("%d")  # ledger keys days by day of month
        month_used = sum(v.get("month", 0) for v in svcs)
        today = sum((v.get("days") or {}).get(day, 0) for v in svcs)
        budget = None
    if status:   # core's whole-plan view: used/limit for the current key, key fingerprint (never the key)
        month_used = status.get("used", month_used)
        limit = status.get("limit") or limit
    # identifies the key without ever storing it: core's 8-hex fingerprint, else our own short hash
    key = ((status or {}).get("key_fp") or led.get("key_fp")
           or hashlib.sha256(cfg("HELIUS_API_KEY", "").encode()).hexdigest()[:8])
    per = {n: (v.get("credits_today", 0), v.get("credits_day_budget")) for n, v in (stats or {}).items()}
    # when the month runs out at today's rate (the ledger's day is UTC); the calendar ratio alone hid a day that
    # burns ten times the days before (18.10.: x0.79 while the cap was a day away)
    now_utc = dt.datetime.now(dt.timezone.utc)
    hours = (now_utc - now_utc.replace(hour=0, minute=0, second=0, microsecond=0)).total_seconds() / 3600
    def _cap(spent):
        if not (limit and spent > 0 and hours >= 1):
            return None
        left_h = max(0.0, limit - month_used) / (spent / hours)
        return (now_utc + dt.timedelta(hours=left_h)).timestamp() if left_h < 31 * 24 else None
    cap_at = _cap(today)
    # second case: sniper's stream 2 leaves Helius on its own once the month runs ahead of the calendar
    cap_at_no_stream = _cap(today - per.get("sniper", (0, None))[0]) if "sniper" in per else None
    if status and "cap_at" in status:   # core's own forecast (same rule, read straight from the ledger)
        cap_at, cap_at_no_stream = status["cap_at"], status.get("cap_at_no_stream")
    return {"month": month, "month_used": month_used, "month_limit": limit, "today": today,
            "day_budget": budget if limit_on else None, "key": str(key),
            "warn_level": (status or {}).get("warn_level"), "pace": pace, "cap_at": cap_at,
            "cap_at_no_stream": cap_at_no_stream, "per_service": per}


def _fmt_int(x) -> str:
    return f"{x:,.0f}".replace(",", ".")


def credits_line(c: dict) -> str:
    """The day's sum against the summed day shares only while those shares limit (HELIUS_DAILY_LIMIT=1); since
    18.10. the user watches the plan, so the line shows the month and the plan against the calendar instead."""
    day = f"heute {_fmt_int(c['today'])}" + (f" von {_fmt_int(c['day_budget'])}" if c["day_budget"] else "")
    pace = f" (Kalender x{c['pace']:.2f})".replace(".", ",") if c.get("pace") is not None else ""
    when = lambda t: time.strftime('%d.%m. %H:%M', time.localtime(t)) if t else "nach Monatsende"  # noqa: E731
    cap = f", Kappe bei aktueller Rate am {when(c['cap_at'])}" if c.get("cap_at") else ""
    if cap and "cap_at_no_stream" in c:
        cap += f" (ohne Sniper-Strom 2: {when(c['cap_at_no_stream'])})"
    return f"Helius-Credits: {day}, Monat {_fmt_int(c['month_used'])} von {_fmt_int(c['month_limit'])}{pace}{cap}"


def check_credit_alerts(st: dict):
    """One Telegram message at 80 % and at 100 % of the monthly Helius quota, once per month and key."""
    c = helius_credits()
    if not c or not c["month_limit"]:
        return
    pct = 100 * c["month_used"] / c["month_limit"]
    sent = st.setdefault("credit_alerts", [])
    for level in (80, 100):
        tag = f"{c['month']}:{c['key']}:{level}"
        if pct < level or tag in sent:
            continue
        if level == 80 and pct >= 100:   # jumped straight past 80 %: only the 100 % message
            sent.append(tag)
            continue
        if level == 100:
            text = (f"🔴 Helius-Kontingent aufgebraucht ({pct:.0f} %). Der Bot läuft über öffentliche RPCs weiter, "
                    "etwas langsamer. Für volle Geschwindigkeit einen neuen kostenlosen Helius-Key in die .env "
                    "eintragen (HELIUS_API_KEY), dann mayham.py stop und start.")
        else:
            text = (f"🟡 Helius-Credits bei {pct:.0f} % des Monats ({_fmt_int(c['month_used'])} von "
                    f"{_fmt_int(c['month_limit'])}). Bei 100 % läuft der Bot über öffentliche RPCs weiter; "
                    "dann kann ein neuer Key in die .env.")
        if send_alert(text):
            sent.append(tag)
    st["credit_alerts"] = sent[-12:]


def check_service_budgets(st: dict, c: dict | None = None, now: dt.datetime | None = None):
    """Once per service and UTC day: alert when a service is on pace to blow its daily Helius share
    (projection from the hours elapsed today, from 2 h into the day) or has already used it up."""
    c = c if c is not None else helius_credits()
    if not c or not c.get("per_service"):
        return []
    now = now or dt.datetime.now(dt.timezone.utc)
    hours = now.hour + now.minute / 60
    sent = st.setdefault("budget_alerts", [])
    out = []
    for name, (used, budget) in sorted(c["per_service"].items()):
        if not budget:
            continue
        pace = used / hours * 24 if hours >= 2 else used
        level = "over" if used > budget else ("pace" if pace > 1.5 * budget else None)
        tag = f"{now:%Y-%m-%d}:{name}:{level}"
        if not level or tag in sent or (level == "pace" and f"{now:%Y-%m-%d}:{name}:over" in sent):
            continue
        text = (f"🟠 Helius: {name} hat sein Tagesbudget aufgebraucht ({_fmt_int(used)} von {_fmt_int(budget)}); "
                "der Dienst weicht jetzt auf öffentliche RPCs aus." if level == "over" else
                f"🟡 Helius: {name} verbraucht zu schnell, Hochrechnung {_fmt_int(pace)} heute bei einem "
                f"Tagesbudget von {_fmt_int(budget)} (bisher {_fmt_int(used)}).")
        print(text)
        if send_alert(text):
            sent.append(tag)
        out.append(text)
    st["budget_alerts"] = sent[-40:]
    return out


# ---------------------------------------------------------------- hourly summary
def _q(conn, sql, args=(), default=None):
    try:
        r = conn.execute(sql, args).fetchone()
        return r[0] if r and r[0] is not None else default
    except sqlite3.Error:
        return default


def build_summary() -> str:
    """Short German status report for Telegram: uptime, feed, coins, wallets, signals, paper trading."""
    r = collect()
    now = int(time.time())
    lines = []
    pids = ROOT / "data" / "pids"
    if r["processes"]:
        ages = []
        for n, alive in r["processes"].items():
            try:
                age = _proc_age_s(int((pids / f"{n}.pid").read_text())) if alive else None
            except (OSError, ValueError):
                age = None
            if age is not None:
                ages.append(age)
        since = now - min(ages) if ages else None   # all running since the youngest one started (a deploy resets it)
        run = sum(r["processes"].values())
        lines.append(f"{'🟢' if run == len(r['processes']) else '🟠'} Dienste {run}/{len(r['processes'])} laufen"
                     + (f", seit {(now - since) / 3600:.1f} h" if since else ""))
    md = r["feed"].get("Marktdaten")
    lines.append(f"Datenfeed: letzte Marktdaten {ago(md)}")
    p = db_path()
    if p.exists():
        try:
            c = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=10)
            tracked = _q(c, "SELECT COUNT(*) FROM tokens WHERE tracked=1", default=0)
            trades_h = _q(c, "SELECT COUNT(*) FROM trades WHERE ts>?", (now - 3600,), default=0)
            smart = _q(c, "SELECT COUNT(*) FROM wallets WHERE is_smart=1", default=0)
            tiers = {t: _q(c, "SELECT COUNT(*) FROM wallets WHERE tier=?", (t,), default=0)
                     for t in ("ultra", "smart", "good")}
            lines.append(f"Coins beobachtet: {tracked} · neue Trades (1 h): {trades_h:,}".replace(",", "."))
            lines.append(f"Wallets: {smart} smart · Tiers ultra {tiers['ultra']} / smart {tiers['smart']} / "
                         f"good {tiers['good']}")
            s1 = _q(c, "SELECT COUNT(*) FROM signals WHERE ts>?", (now - 3600,), default=0)
            s24 = _q(c, "SELECT COUNT(*) FROM signals WHERE ts>?", (now - 86400,), default=0)
            lines.append(f"Signale: {s1} in 1 h, {s24} in 24 h")
            try:
                best = c.execute("SELECT symbol, score, max_ret_1h FROM signals WHERE ts>? AND max_ret_1h IS NOT NULL "
                                 "ORDER BY max_ret_1h DESC LIMIT 1", (now - 86400,)).fetchone()
                if best:
                    lines.append(f"Bestes Signal 24 h: {best[0]} (Score {best[1]:.0f}) max {best[2] * 100:+.0f}% in 1 h")
            except sqlite3.Error:
                pass
            for mode in ("paper", "live"):
                opn = _q(c, "SELECT COUNT(*) FROM ct_positions WHERE mode=? AND status='open'", (mode,), default=0)
                closed = _q(c, "SELECT COUNT(*) FROM ct_positions WHERE mode=? AND closed_at>?",
                            (mode, now - 86400), default=0)
                pnl = _q(c, "SELECT SUM(pnl_sol) FROM ct_positions WHERE mode=? AND closed_at>?",
                         (mode, now - 86400), default=0.0)
                eq = _q(c, "SELECT equity_sol FROM ct_equity WHERE mode=? ORDER BY ts DESC LIMIT 1", (mode,))
                if opn or closed or eq is not None:
                    lines.append(f"{'Papier' if mode == 'paper' else 'ECHTGELD'}: {opn} offen, {closed} geschlossen (24 h), "
                                 f"PnL {pnl:+.3f} SOL" + (f", Kapital {eq:.3f} SOL" if eq is not None else ""))
            c.close()
        except sqlite3.Error as e:
            lines.append(f"DB nicht lesbar: {e}")
    try:
        c = helius_credits()
        if c:
            lines.append(credits_line(c))
    except Exception as e:  # noqa: BLE001 - never let the report fail on the credit ledger
        lines.append(f"Helius-Credits: nicht lesbar ({type(e).__name__})")
    if r["problems"]:
        lines.append("Probleme: " + "; ".join(r["problems"].values()))
    lines.append(f"Dashboard: {dashboard_url()}")
    return "📊 <b>Stündlicher Bericht</b>\n" + "\n".join(lines)


def summary(args) -> int:
    text = build_summary()
    if args.send:
        return 0 if send_alert(text) else 1
    print(text.replace("<b>", "").replace("</b>", ""))
    return 0


# ---------------------------------------------------------------- status / health
def health(args) -> int:
    r = collect()
    hard = {k: v for k, v in r["problems"].items() if k not in ("backup_old", "on_battery", "disk_low")}
    if args.json:
        print(json.dumps(r, ensure_ascii=False, default=str))
    else:
        print("OK" if not hard else "\n".join(hard.values()))
    return 1 if hard else 0


def status(args) -> int:
    r = collect()
    st = load_state()
    yes = lambda b: "✅" if b else "❌"  # noqa: E731
    print("mayhambot Status", dt.datetime.now().strftime("%d.%m.%Y %H:%M"))
    if r["processes"]:
        print("Prozesse:   " + "  ".join(f"{n} {yes(ok)}" for n, ok in r["processes"].items()))
    print(f"Dashboard:  {yes(r['dashboard'])}  {dashboard_url()}")
    for k, ts in r["feed"].items():
        print(f"{k + ':':22s}{ago(ts)}")
    if IS_MAC:
        for lbl, what in ((APP_LABEL, "App-Autostart"), ("de.mayhambot.watchdog", "Watchdog"),
                          ("de.mayhambot.backup", "Backups"), ("de.mayhambot.keepawake", "Wachhalten")):
            print(f"{what + ':':22s}{yes(launchd_loaded(lbl))}")
        print(f"{'Strom:':22s}{r['power']}")
    print(f"{'Watchdog lief:':22s}{ago(st.get('last_run'))}")
    print(f"{'Letztes Backup:':22s}{r['last_backup'] or 'keins'}"
          + (f" ({ago(Path(r['last_backup']).stat().st_mtime)})" if r["last_backup"] else ""))
    print(f"{'Freier Speicher:':22s}{r['free_gb']:.1f} GB" if r["free_gb"] is not None else "")
    print("Probleme:   " + ("keine" if not r["problems"] else ""))
    for v in r["problems"].values():
        print(f"  - {v}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="mayhambot Betrieb")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    h = sub.add_parser("health")
    h.add_argument("--json", action="store_true")
    w = sub.add_parser("watchdog")
    w.add_argument("--no-restart", action="store_true")
    sub.add_parser("backup")
    sub.add_parser("list-backups")
    r = sub.add_parser("restore")
    r.add_argument("archive")
    r.add_argument("--with-env", action="store_true", help=".env aus dem Backup übernehmen")
    r.add_argument("--no-stop", action="store_true", help="App nicht stoppen/starten (z.B. auf neuem Server)")
    sub.add_parser("rotate-logs")
    sub.add_parser("test-alert")
    sub.add_parser("setup-telegram").add_argument("--neu", action="store_true", help="Chat-ID neu suchen")
    sub.add_parser("report")
    sub.add_parser("db-sizes").add_argument("--profile", action="store_true", help="auch trades aufschlüsseln (langsam)")
    sub.add_parser("retention").add_argument("--db", help="Datenbankdatei (Standard: MAYHAM_DB_PATH)")
    sm = sub.add_parser("summary")
    sm.add_argument("--send", action="store_true", help="per Telegram senden")
    a = ap.parse_args(argv)
    fn = {"status": status, "health": health, "watchdog": watchdog, "backup": backup,
          "list-backups": list_backups, "summary": summary, "report": lambda a: print(ops_report()) or 0, "db-sizes": db_sizes, "retention": retention_run, "setup-telegram": setup_telegram, "restore": restore, "rotate-logs": rotate_logs,
          "test-alert": lambda _: 0 if send_alert("Test: Watchdog-Alarme kommen an ✅") else 1}[a.cmd]
    return fn(a)


if __name__ == "__main__":
    sys.exit(main())
