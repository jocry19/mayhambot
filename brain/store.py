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
CREATE TABLE IF NOT EXISTS brain_exit_evals (
    opp_id INTEGER PRIMARY KEY, ts INTEGER, kind TEXT, plan TEXT, plan_gain REAL, organic REAL, organic_reason TEXT,
    organic_min REAL, hold_1h REAL, max_up REAL, evaluated_at INTEGER
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
    _add_columns(conn, "brain_equity", EQUITY_COLS)
    from . import labels, opportunities

    opportunities.ensure(conn)
    _add_columns(conn, "opportunities", labels.COLS)
    try:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_opportunities_label ON opportunities(label_at, id)")
    except sqlite3.OperationalError:
        pass


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


PERFORM, LEARN = "balance_sol", "learn_balance_sol"  # the brain's own account; the learning account (pool, from 0)


def account(kind: str) -> str:
    """Which cash account a position of this kind books to: the pooled opportunities are the learning account."""
    return LEARN if kind == "pool" else PERFORM


def balance(conn, start: float, key: str = PERFORM) -> float:
    v = get(conn, key)
    if v is None:
        put(conn, key, start)
        return float(start)
    return float(v)


def adjust_balance(conn, delta: float, start: float, key: str = PERFORM) -> None:
    balance(conn, start, key)
    conn.execute("UPDATE brain_state SET value = CAST(value AS REAL) + ? WHERE key=?", (delta, key))


EQUITY_COLS = (("learn_balance_sol", "REAL"), ("learn_open_sol", "REAL"), ("learn_equity_sol", "REAL"))


def _add_columns(conn, table: str, cols) -> None:
    have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    for name, typ in cols:
        if name not in have:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")
            except sqlite3.OperationalError:  # a read-only reader (dashboard): the service adds them
                return


def split_learning(conn, start: float, now: float) -> float | None:
    """Once: what the pooled learning buys did to the brain's own balance before the accounts were split moves to the
    learning account (cash out minus cash in of every pool position so far). Returns the SOL moved, None if done
    already. The move is recorded in brain_state["equity_transfers"] so hour factors do not read it as a gain."""
    if get(conn, "learn_split") is not None:
        return None
    rows = conn.execute("SELECT status, size_sol, proceeds_sol, meta, tokens, last_price_sol FROM brain_positions "
                        "WHERE kind='pool'").fetchall()
    flow = held = 0.0
    for st, size, proceeds, meta, tokens, px in rows:
        flow -= size or 0.0
        if st == "closed":
            flow += proceeds or 0.0
        else:
            held += (tokens or 0.0) * (px or 0.0)
            try:
                flow += (json.loads(meta or "{}") or {}).get("partial_sol", 0.0) or 0.0
            except (TypeError, ValueError):
                pass
    balance(conn, start)
    balance(conn, 0.0, LEARN)
    if flow:
        adjust_balance(conn, -flow, start)
        adjust_balance(conn, flow, 0.0, LEARN)
    if flow or held:  # the brain's equity changes by the cash moved less the open learning positions it no longer holds
        tr = get(conn, "equity_transfers") or []
        tr.append([int(now) // 60 * 60, round(-flow - held, 9)])  # on the minute, like brain_equity
        put(conn, "equity_transfers", tr[-50:])
    put(conn, "learn_split", {"at": int(now), "moved_sol": round(flow, 9), "open_value_sol": round(held, 9),
                              "positions": len(rows)})
    return flow


def day_start(now: float | None = None) -> int:
    t = int(now or time.time())
    return t - t % 86400
