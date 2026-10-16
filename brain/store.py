"""The brain's own tables (prefix brain_) in the shared SQLite file. Other packages' tables are only read."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS brain_opps (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, decided_at REAL, kind TEXT, ref TEXT, mint TEXT, symbol TEXT,
    wallet TEXT, entity TEXT, src_ts INTEGER, price_sol REAL, features TEXT, raw TEXT,
    decision TEXT, wanted INTEGER DEFAULT 0, plan TEXT, pred_mean REAL, pred_std REAL, pred_p REAL, sample REAL, explore INTEGER DEFAULT 0,
    size_sol REAL, note TEXT, position_id INTEGER,
    evaluated_at INTEGER, gains TEXT, realized_plan REAL, realized_base REAL, realized_best REAL,
    learned_real INTEGER DEFAULT 0,
    UNIQUE (kind, ref)
);
CREATE INDEX IF NOT EXISTS idx_brain_opps_ts ON brain_opps(ts);
CREATE INDEX IF NOT EXISTS idx_brain_opps_eval ON brain_opps(evaluated_at, ts);
CREATE INDEX IF NOT EXISTS idx_brain_opps_mint ON brain_opps(mint, ts);
CREATE TABLE IF NOT EXISTS brain_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, opp_id INTEGER, kind TEXT, mint TEXT, symbol TEXT, wallet TEXT,
    src_ts INTEGER, opened_at INTEGER, entry_price_sol REAL, size_sol REAL, tokens REAL, decimals INTEGER,
    plan TEXT, take_profit REAL, stop_loss REAL, trail_activate REAL, trail_drawdown REAL, max_hold_min REAL,
    inflow_exit INTEGER, whale_exit INTEGER, entry_liq_usd REAL,
    high_price_sol REAL, last_price_sol REAL, last_seen INTEGER, peak_buys_m5 INTEGER,
    status TEXT DEFAULT 'open', closed_at INTEGER, exit_price_sol REAL, proceeds_sol REAL, pnl_sol REAL,
    pnl_pct REAL, exit_reason TEXT, note TEXT, meta TEXT
);
CREATE INDEX IF NOT EXISTS idx_brain_positions_status ON brain_positions(status);
CREATE TABLE IF NOT EXISTS brain_equity (
    ts INTEGER PRIMARY KEY, balance_sol REAL, open_value_sol REAL, equity_sol REAL
);
CREATE TABLE IF NOT EXISTS brain_journal (
    day TEXT PRIMARY KEY, ts INTEGER, text TEXT, data TEXT, sent INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS brain_state (key TEXT PRIMARY KEY, value TEXT);
"""


def connect(db_path: str | None = None) -> sqlite3.Connection:
    """Shared DB through core.db (WAL, busy timeout, core schema); autocommit so no lock outlives a statement."""
    conn = None
    try:
        from core import db as core_db  # type: ignore

        conn = core_db.connect(db_path) if db_path else core_db.connect()
    except Exception:
        path = db_path or "data/mayhambot.db"
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            pass
    conn.isolation_level = None
    conn.execute("PRAGMA busy_timeout=30000")
    init(conn)
    return conn


def init(conn) -> None:
    conn.executescript(SCHEMA)


def has(conn, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def get(conn, key: str, default=None):
    r = conn.execute("SELECT value FROM brain_state WHERE key=?", (key,)).fetchone()
    if not r:
        return default
    try:
        return json.loads(r[0])
    except (TypeError, ValueError):
        return r[0]


def put(conn, key: str, value) -> None:
    conn.execute("INSERT OR REPLACE INTO brain_state VALUES (?,?)", (key, json.dumps(value)))


def balance(conn, start: float) -> float:
    v = get(conn, "balance_sol")
    if v is None:
        put(conn, "balance_sol", start)
        return float(start)
    return float(v)


def adjust_balance(conn, delta: float, start: float) -> None:
    balance(conn, start)
    conn.execute("UPDATE brain_state SET value = CAST(value AS REAL) + ? WHERE key='balance_sol'", (delta,))


def day_start(now: float | None = None) -> int:
    t = int(now or time.time())
    return t - t % 86400
