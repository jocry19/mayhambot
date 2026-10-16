"""Database access for the web API and the terminal.

Reads the shared mayhambot SQLite file. Every table of the other packages is optional:
a table or column that does not exist yet simply yields empty results, so the dashboard
works from the first minute and grows as signals/, copytrade/ and model/ fill the DB.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def db_path() -> str:
    p = os.getenv("MAYHAM_DB_PATH")
    if p:
        return p if os.path.isabs(p) else str(ROOT / p)
    try:
        from core.config import settings  # type: ignore

        return settings.db_path
    except Exception:
        return str(ROOT / "data" / "mayhambot.db")


_local = threading.local()


def read_conn() -> sqlite3.Connection:
    """Per-thread read connection (query_only, never creates schema)."""
    path = db_path()
    c = getattr(_local, "conn", None)
    if c is not None and getattr(_local, "path", None) == path:
        return c
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path, timeout=30, check_same_thread=False)
    c.row_factory = sqlite3.Row
    try:
        c.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        pass
    c.execute("PRAGMA query_only=ON")
    _local.conn, _local.path = c, path
    return c


def write_conn() -> sqlite3.Connection:
    """Write connection for user actions (modes, presets, buy/sell requests)."""
    try:
        from core import db as core_db  # type: ignore

        return core_db.connect(db_path())
    except Exception:
        c = sqlite3.connect(db_path(), timeout=30, check_same_thread=False)
        c.row_factory = sqlite3.Row
        return c


_schema_cache: dict = {"ts": 0, "tables": {}}


def tables(conn) -> dict[str, set[str]]:
    """{table: {columns}} cached for 20 s (other modules add tables while we run)."""
    now = time.time()
    if now - _schema_cache["ts"] > 20 or _schema_cache.get("path") != db_path():
        out = {}
        for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
            out[name] = {r[1] for r in conn.execute(f"PRAGMA table_info('{name}')").fetchall()}
        _schema_cache.update(ts=now, tables=out, path=db_path())
    return _schema_cache["tables"]


def has(conn, table: str, *cols: str) -> bool:
    t = tables(conn).get(table)
    return t is not None and all(c in t for c in cols)


def rows(conn, sql: str, args=()) -> list[dict]:
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    except sqlite3.OperationalError:
        return []


def one(conn, sql: str, args=()) -> dict | None:
    try:
        r = conn.execute(sql, args).fetchone()
        return dict(r) if r else None
    except sqlite3.OperationalError:
        return None


def scalar(conn, sql: str, args=(), default=None):
    try:
        r = conn.execute(sql, args).fetchone()
        return r[0] if r and r[0] is not None else default
    except sqlite3.OperationalError:
        return default


class TTLCache:
    """Tiny time-based memo for expensive aggregates (tier map, influence, flows)."""

    def __init__(self):
        self._d: dict = {}
        self._busy: set = set()
        self._lock = threading.Lock()

    def get(self, key, ttl: float, fn):
        now = time.time()
        with self._lock:
            hit = self._d.get(key)
            if hit and now - hit[0] < ttl:
                return hit[1]
        val = fn()
        with self._lock:
            self._d[key] = (now, val)
        return val

    def get_bg(self, key, ttl: float, fn):
        """Never blocks: returns the cached value (or None the first time) and computes in a background thread.
        For slow extras on a page (the live model prognosis), so the page itself loads at once."""
        now = time.time()
        with self._lock:
            hit = self._d.get(key)
            if hit and now - hit[0] < ttl:
                return hit[1]
            start = key not in self._busy
            if start:
                self._busy.add(key)
        if start:
            def run():
                try:
                    val = fn()
                    with self._lock:
                        self._d[key] = (time.time(), val)
                except Exception:
                    pass
                finally:
                    with self._lock:
                        self._busy.discard(key)
            threading.Thread(target=run, daemon=True, name="bg-cache").start()
        return hit[1] if hit else None

    def get_swr(self, key, ttl: float, fn, stale: float = 120):
        """Like get, but a value older than ttl (and younger than stale) is returned at once while one background
        thread recomputes it: pages stay instant even when the aggregate takes a second on a busy database."""
        now = time.time()
        with self._lock:
            hit = self._d.get(key)
            if hit and now - hit[0] < ttl:
                return hit[1]
            refresh = hit and now - hit[0] < stale and key not in self._busy
            if refresh:
                self._busy.add(key)
        if not refresh:
            return self.get(key, 0, fn)

        def run():
            try:
                val = fn()
                with self._lock:
                    self._d[key] = (time.time(), val)
            except Exception:  # keep the old value; the next request tries again
                pass
            finally:
                with self._lock:
                    self._busy.discard(key)
        threading.Thread(target=run, daemon=True, name=f"swr-{key[0] if key else ''}").start()
        return hit[1]

    def clear(self):
        with self._lock:
            self._d.clear()


cache = TTLCache()
