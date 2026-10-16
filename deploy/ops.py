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
  python deploy/ops.py test-alert             # sends a Telegram test message
  python deploy/ops.py summary [--send]       # hourly status report (watchdog sends it every SUMMARY_EVERY_HOURS)

Settings (optional, in .env):
  BACKUP_DIR            where backups go (default: ~/mayhambot-backups); use a 2nd disk or cloud folder
  BACKUP_KEEP_HOURS     keep every backup this long (default 48), then one per day ...
  BACKUP_KEEP_DAYS      ... for this many days (default 30)
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
import os
import platform
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
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
    power = check_power()
    if power == "Akku":
        problems["on_battery"] = "Mac läuft auf Akku: bitte Netzteil anschließen, sonst schläft er ein."
    return {"ts": now, "runtime": runtime, "processes": procs, "dashboard": dash, "feed": feed,
            "free_gb": free_gb, "last_backup": str(lb) if lb else None, "power": power, "problems": problems}


# ---------------------------------------------------------------- alerts
def send_alert(text: str) -> bool:
    """Telegram via the app's notifier when importable, else plain urllib (same bot/chat from .env)."""
    token, chat = cfg("TELEGRAM_BOT_TOKEN", ""), cfg("TELEGRAM_CHAT_ID", "")
    print(f"[alert] {text}")
    if not (token and chat):
        return True   # nothing configured: console only, don't keep retrying
    text = f"🛠 <b>mayhambot Betrieb</b> ({platform.node()})\n{text}"
    try:
        sys.path.insert(0, str(ROOT))
        from signals.notifier import TelegramNotifier  # noqa: PLC0415
        return TelegramNotifier(token, chat).send(text) is not False
    except Exception:  # noqa: BLE001 - app venv missing/broken: fall back to stdlib
        pass
    try:
        data = urllib.parse.urlencode({"chat_id": chat, "text": text, "parse_mode": "HTML"}).encode()
        with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data, timeout=15) as r:
            return r.status == 200
    except Exception as e:  # noqa: BLE001
        print(f"[alert] telegram failed: {e}")
        return False


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
    locks = new_lock_errors(st)
    if locks > cfg("LOCK_ALERT_PER_RUN", 20):
        r["problems"]["db_locked"] = f"Datenbank überlastet: {locks}× 'database is locked' in 2 min."
    seen = st.setdefault("problems", {})     # key -> {first, count, alerted, msg}
    repeat = cfg("ALERT_REPEAT_HOURS", 6) * 3600
    restartable = {"app_down", "service_down", "dashboard_down", "feed_stalled", "copytrade_stalled",
                   "sniper_stalled"}
    need_restart = []

    for key, msg in r["problems"].items():
        p = seen.setdefault(key, {"first": now, "count": 0, "alerted": 0})
        p["count"] += 1
        p["msg"] = msg
        if p["count"] < CONFIRM_RUNS:
            continue
        if key in restartable:
            need_restart.append(key)
        if not p["alerted"] or now - p["alerted"] > repeat:
            if send_alert(f"🔴 {msg}"):
                p["alerted"] = now

    action = None
    if need_restart and not args.no_restart and now - st.get("last_restart", 0) > RESTART_COOLDOWN:
        action = restart_app(", ".join(need_restart))
        st["last_restart"] = now
        send_alert(f"🔁 {action} wegen: {', '.join(seen[k]['msg'] for k in need_restart)}")

    for key in list(seen):
        if key not in r["problems"]:
            if seen[key].get("alerted"):
                send_alert(f"🟢 Wieder OK (war: {seen[key].get('msg', key)})")
            del seen[key]

    try:
        check_credit_alerts(st)
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


def _repair_indexes(path: Path, timeout: float = 60) -> list[str]:
    """REINDEX the database (seconds for ~200k rows, waits for the services' write lock), then re-check."""
    c = sqlite3.connect(path, timeout=timeout)
    try:
        c.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
        c.execute("REINDEX")
        return [r[0] for r in c.execute("PRAGMA integrity_check").fetchall() if r[0] != "ok"]
    finally:
        c.close()


def _snapshot_db(src: Path, dst: Path):
    """Online-consistent copy via SQLite's backup API (safe while the app writes).

    Broken indexes (10/2026: rows missing from idx_trades_wallet_ts and ct_swaps' key) are rebuilt in the copy
    and, natively on this machine, in the live database (user's choice); damaged tables still abort."""
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=60)
    d = sqlite3.connect(dst)
    s.backup(d, pages=4096, sleep=0.05)
    s.close()
    # full integrity_check: quick_check does not compare indexes with their tables on every SQLite version
    problems = [r[0] for r in d.execute("PRAGMA integrity_check").fetchall() if r[0] != "ok"]
    ok = problems[0] if problems else "ok"
    if ok != "ok":
        d.close()
        if not _index_only(problems):
            raise RuntimeError(f"Integritätsprüfung der Kopie fehlgeschlagen: {ok}")
        left = _repair_indexes(dst)
        if left:
            raise RuntimeError(f"Integritätsprüfung der Kopie fehlgeschlagen nach REINDEX: {left[0]}")
        live_left = _repair_indexes(src)
        msg = (f"🛠️ Datenbank: {len(problems)} kaputte Index-Einträge neu aufgebaut "
               f"({'Live-DB jetzt ok' if not live_left else 'Live-DB noch: ' + live_left[0]}), keine Daten verloren.")
        print(msg)
        try:
            send_alert(msg)
        except Exception:  # noqa: BLE001
            pass
        d = sqlite3.connect(dst)
    d.execute("PRAGMA journal_mode=DELETE")
    d.close()


def _redacted_env() -> str:
    lines = []
    for line in (ROOT / ".env").read_text().splitlines():
        k = line.split("=", 1)[0].strip()
        lines.append(f"{k}=<entfernt: nicht im Backup>" if k in SECRET_KEYS and "=" in line
                     and line.split("=", 1)[1].strip() else line)
    return "\n".join(lines) + "\n"


def backup(args) -> int:
    out = backup_dir()
    out.mkdir(parents=True, exist_ok=True)
    os.chmod(out, 0o700)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    final = out / f"mayhambot-{stamp}.tar.gz"
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        tar_tmp = td / "b.tar.gz"
        with tarfile.open(tar_tmp, "w:gz", compresslevel=6) as tar:
            src = db_path()
            if src.exists():
                snap = td / "mayhambot.db"
                _snapshot_db(src, snap)
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
    print(f"Backup: {final} ({final.stat().st_size / 1e6:.1f} MB), {removed} alte entfernt")
    return 0


def prune_backups(out: Path) -> int:
    keep_h, keep_d = cfg("BACKUP_KEEP_HOURS", 48), cfg("BACKUP_KEEP_DAYS", 30)
    now = time.time()
    days_kept = set()
    removed = 0
    for f in sorted(out.glob("mayhambot-*.tar.gz"), reverse=True):   # newest first
        age = now - f.stat().st_mtime
        day = f.name[10:18]
        if age < keep_h * 3600:
            continue
        if age < keep_d * 86400 and day not in days_kept:
            days_kept.add(day)
            continue
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
            dst.parent.mkdir(parents=True, exist_ok=True)
            for suffix in ("", "-wal", "-shm"):
                old = Path(str(dst) + suffix)
                if old.exists():
                    old.rename(f"{old}.vor-restore-{stamp}")
            shutil.copy2(td / "data" / "mayhambot.db", dst)
        for f in (td / "data").rglob("*") if (td / "data").exists() else []:
            if f.is_file() and f.name != "mayhambot.db":
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
    api = f"https://api.telegram.org/bot{token}"
    try:
        with urllib.request.urlopen(f"{api}/getMe", timeout=15) as r:
            me = json.loads(r.read())["result"]
        with urllib.request.urlopen(f"{api}/getUpdates", timeout=15) as r:
            ups = json.loads(r.read()).get("result", [])
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
    stats, led, status = None, {}, None
    try:
        sys.path.insert(0, str(ROOT))
        from core import http as core_http  # noqa: PLC0415
        stats = core_http.credits()
        if hasattr(core_http, "credits_status"):
            status = core_http.credits_status()
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
    return {"month": month, "month_used": month_used, "month_limit": limit, "today": today,
            "day_budget": budget, "key": str(key), "warn_level": (status or {}).get("warn_level")}


def _fmt_int(x) -> str:
    return f"{x:,.0f}".replace(",", ".")


def credits_line(c: dict) -> str:
    day = f"heute {_fmt_int(c['today'])}" + (f" von {_fmt_int(c['day_budget'])}" if c["day_budget"] else "")
    return f"Helius-Credits: {day}, Monat {_fmt_int(c['month_used'])} von {_fmt_int(c['month_limit'])}"


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
        up = [f for f in (pids / f"{n}.pid" for n in r["processes"]) if f.exists()]
        since = min((f.stat().st_mtime for f in up), default=None)
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
    sub.add_parser("setup-telegram")
    sm = sub.add_parser("summary")
    sm.add_argument("--send", action="store_true", help="per Telegram senden")
    a = ap.parse_args(argv)
    fn = {"status": status, "health": health, "watchdog": watchdog, "backup": backup,
          "list-backups": list_backups, "summary": summary, "setup-telegram": setup_telegram, "restore": restore, "rotate-logs": rotate_logs,
          "test-alert": lambda _: 0 if send_alert("Test: Watchdog-Alarme kommen an ✅") else 1}[a.cmd]
    return fn(a)


if __name__ == "__main__":
    sys.exit(main())
