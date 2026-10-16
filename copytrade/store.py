"""Copy-trading tables (prefix ct_) in the shared mayhambot SQLite database.

Uses core.db.connect() when core is present so both modules share one file;
whale swaps are also written into core's `trades` table so the signal side
profits from what the copy tracker sees.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

from .config import Settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS ct_wallets (
    address TEXT PRIMARY KEY, label TEXT DEFAULT '', source TEXT DEFAULT 'auto',
    added_at INTEGER, last_sig TEXT, last_polled INTEGER DEFAULT 0, active INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS ct_swaps (
    sig TEXT, wallet TEXT, mint TEXT, side TEXT, ts INTEGER, token_amount REAL, sol_amount REAL,
    price_sol REAL, seen_at INTEGER, PRIMARY KEY (sig, wallet, mint)
);
CREATE INDEX IF NOT EXISTS idx_ct_swaps_wallet ON ct_swaps(wallet, ts);
CREATE INDEX IF NOT EXISTS idx_ct_swaps_mint ON ct_swaps(mint, ts);
CREATE TABLE IF NOT EXISTS ct_buy_outcomes (
    sig TEXT PRIMARY KEY, wallet TEXT, mint TEXT, ts INTEGER, sol_amount REAL, pool TEXT,
    entry_price_usd REAL, our_entry_price_usd REAL, first_pump_gain REAL, first_pump_min REAL,
    max_gain REAL, max_gain_min REAL, min_before_peak REAL, inflow_lead_min REAL,
    achievable_gain REAL, pumped INTEGER, candles INTEGER, evaluated_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_ct_outcomes_wallet ON ct_buy_outcomes(wallet);
CREATE TABLE IF NOT EXISTS ct_wallet_rank (
    address TEXT PRIMARY KEY, samples INTEGER, hit_rate REAL, median_first_pump REAL,
    median_pump_min REAL, median_lead_min REAL, median_achievable REAL, avg_loss REAL,
    expected_value REAL, score REAL, take_profit REAL, max_hold_min REAL, updated_at INTEGER
);
CREATE TABLE IF NOT EXISTS ct_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, mode TEXT, wallet TEXT, mint TEXT, symbol TEXT,
    whale_sig TEXT, whale_ts INTEGER, whale_price_sol REAL,
    opened_at INTEGER, entry_price_sol REAL, size_sol REAL, tokens REAL, token_decimals INTEGER,
    entry_cost_sol REAL, take_profit REAL, stop_loss REAL, max_hold_min REAL,
    high_price_sol REAL, last_price_sol REAL, last_buys_m5 INTEGER, peak_buys_m5 INTEGER,
    status TEXT DEFAULT 'open', closed_at INTEGER, exit_price_sol REAL, proceeds_sol REAL,
    pnl_sol REAL, pnl_pct REAL, exit_reason TEXT, entry_tx TEXT, exit_tx TEXT, meta TEXT
);
CREATE INDEX IF NOT EXISTS idx_ct_positions_status ON ct_positions(status);
CREATE TABLE IF NOT EXISTS ct_equity (
    ts INTEGER, mode TEXT, balance_sol REAL, open_value_sol REAL, equity_sol REAL, PRIMARY KEY (ts, mode)
);
CREATE TABLE IF NOT EXISTS ct_events (
    ts INTEGER, level TEXT, kind TEXT, message TEXT, data TEXT
);
CREATE TABLE IF NOT EXISTS ct_state (key TEXT PRIMARY KEY, value TEXT);
"""


def connect(settings: Settings) -> sqlite3.Connection:
    conn: sqlite3.Connection | None = None
    try:
        from core import db as core_db  # type: ignore

        conn = core_db.connect(str(settings.db_path))
    except Exception:
        Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(settings.db_path), timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            pass
    # Autocommit: every statement is its own short transaction, so no write lock is ever held
    # across a network call (four services write this file). Waits up to 30 s for a busy lock.
    conn.isolation_level = None
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(SCHEMA)
    _migrate(conn)
    from . import control

    control.init(conn)
    return conn


POSITION_COLUMNS = {
    "strategy": "TEXT DEFAULT 'whale'",  # whale | signal | manual
    "managed": "TEXT DEFAULT 'auto'",  # auto: bot sells | semi: you sell
    "signal_id": "INTEGER",
    "entry_price_usd": "REAL", "entry_mcap_usd": "REAL",
    "trail_activate": "REAL", "trail_drawdown": "REAL",
    "last_price_usd": "REAL", "last_mcap_usd": "REAL", "exit_mcap_usd": "REAL",
    "exit_hint": "TEXT", "exit_hint_at": "INTEGER",
}


def _migrate(conn: sqlite3.Connection) -> None:
    have = {r[1] for r in conn.execute("PRAGMA table_info(ct_positions)")}
    for col, typ in POSITION_COLUMNS.items():
        if col not in have:
            conn.execute(f"ALTER TABLE ct_positions ADD COLUMN {col} {typ}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ct_positions_strategy ON ct_positions(strategy, status)")
    conn.commit()


def event(conn: sqlite3.Connection, kind: str, message: str, level: str = "info", data: Any = None) -> None:
    from .http import redact

    conn.execute("INSERT INTO ct_events VALUES (?,?,?,?,?)",
                 (int(time.time()), level, kind, redact(message),
                  redact(json.dumps(data)) if data is not None else None))
    conn.commit()


def get_state(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    r = conn.execute("SELECT value FROM ct_state WHERE key=?", (key,)).fetchone()
    return r[0] if r else default


def set_state(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute("INSERT OR REPLACE INTO ct_state VALUES (?,?)", (key, str(value)))
    conn.commit()


def add_wallets(conn: sqlite3.Connection, addresses: Iterable[str], source: str, label: str = "") -> int:
    now = int(time.time())
    before = conn.total_changes
    conn.executemany(
        "INSERT OR IGNORE INTO ct_wallets(address, label, source, added_at) VALUES (?,?,?,?)",
        [(a, label, source, now) for a in addresses],
    )
    conn.commit()
    return conn.total_changes - before


def save_swaps(conn: sqlite3.Connection, swaps: list, sol_usd: float = 0.0) -> int:
    now = int(time.time())
    before = conn.total_changes
    conn.executemany(
        "INSERT OR IGNORE INTO ct_swaps VALUES (?,?,?,?,?,?,?,?,?)",
        [(s.signature, s.wallet, s.mint, s.side, s.block_time, s.token_amount, s.sol_amount, s.price_sol, now)
         for s in swaps],
    )
    added = conn.total_changes - before
    conn.commit()
    _share_with_core(conn, swaps, sol_usd)
    return added


def _share_with_core(conn: sqlite3.Connection, swaps: list, sol_usd: float) -> None:
    try:
        from core import db as core_db  # type: ignore
        from core.models import Trade  # type: ignore
    except Exception:
        return
    try:
        core_db.insert_trades(conn, [
            Trade(sig=s.signature, wallet=s.wallet, mint=s.mint, side=s.side, ts=s.block_time,
                  token_amount=s.token_amount, sol_amount=s.sol_amount, usd_amount=s.sol_amount * sol_usd,
                  price_usd=s.price_sol * sol_usd, source="copytrade")
            for s in swaps
        ])
    except Exception:
        pass


def is_locked(e: Exception) -> bool:
    return isinstance(e, sqlite3.OperationalError) and "locked" in str(e).lower()


def retry_locked(fn, *args, **kw):
    """Run fn; if SQLite was still locked after the busy timeout, wait briefly and retry once."""
    try:
        return fn(*args, **kw)
    except sqlite3.OperationalError as e:
        if not is_locked(e):
            raise
        time.sleep(2)
        return fn(*args, **kw)
