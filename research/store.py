"""Tables owned by research/ and model/ (added next to core's schema, columns only ever added)."""
import json
import sqlite3
import time

from core import db

SCHEMA = """
CREATE TABLE IF NOT EXISTS research_candles (
    mint TEXT, tf_sec INTEGER, ts INTEGER, o REAL, h REAL, l REAL, c REAL, v REAL,
    PRIMARY KEY (mint, tf_sec, ts)
);
CREATE TABLE IF NOT EXISTS research_tokens (
    mint TEXT PRIMARY KEY, pool TEXT, supply REAL, created_at INTEGER, researched_at INTEGER DEFAULT 0,
    early_fetched_at INTEGER DEFAULT 0, peak_multiple REAL, pumped INTEGER DEFAULT 0, source TEXT
);
CREATE TABLE IF NOT EXISTS research_wallets (
    address TEXT PRIMARY KEY, discovered_via TEXT, priority REAL DEFAULT 0, first_seen INTEGER,
    backfilled_at INTEGER DEFAULT 0, n_backfills INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS research_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, version INTEGER, title TEXT, markdown TEXT, json TEXT
);
CREATE TABLE IF NOT EXISTS model_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, active INTEGER DEFAULT 0, n_events INTEGER,
    n_wallets INTEGER, tier_counts_json TEXT, wallet_weights_json TEXT, token_model_json TEXT,
    tier_rules_json TEXT, metrics_json TEXT, notes TEXT
);
CREATE TABLE IF NOT EXISTS model_wallet_stats (
    address TEXT PRIMARY KEY, version INTEGER, ts INTEGER, tier TEXT, score REAL, n_tokens INTEGER,
    win_rate REAL, early_rate REAL, roi REAL, realized_pnl_usd REAL, total_pnl_usd REAL,
    median_entry_mcap REAL, median_min_to_2x REAL, influence_followers REAL, influence_usd REAL,
    inflow_lift_usd REAL, consistency REAL, rug_rate REAL, components_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_mws_tier ON model_wallet_stats(tier, score);
CREATE TABLE IF NOT EXISTS model_tier_history (
    version INTEGER, address TEXT, tier TEXT, score REAL, PRIMARY KEY (version, address)
);
CREATE TABLE IF NOT EXISTS model_predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, mint TEXT, symbol TEXT, version INTEGER, score REAL,
    p_15m REAL, p_1h REAL, p_6h REAL, exp_ret_1h REAL, price_usd REAL, mcap_usd REAL,
    n_ultra INTEGER, n_smart INTEGER, n_good INTEGER, net_sol REAL,
    features_json TEXT, contributions_json TEXT, wallets_json TEXT,
    ret_15m REAL, ret_1h REAL, ret_6h REAL, max_ret_1h REAL, max_ret_6h REAL
);
CREATE INDEX IF NOT EXISTS idx_mp_mint ON model_predictions(mint, ts);
"""


def ensure_schema(conn):
    conn.execute("PRAGMA busy_timeout=120000")  # the live app writes concurrently: wait instead of failing
    retry(lambda: conn.executescript(SCHEMA))
    return conn


def retry(fn, tries=8, conn=None):
    """Run a DB write, retrying while another process holds the write lock (rolls back a half-done try)."""
    for i in range(tries):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            if "locked" not in str(e) and "busy" not in str(e):
                raise
            if conn is not None:
                conn.rollback()
            time.sleep(min(30, 2 ** i))
    return fn()


def connect(path=None):
    return ensure_schema(db.connect(path))


def store_candles(conn, mint, tf_sec, candles):
    conn.executemany("INSERT OR REPLACE INTO research_candles VALUES (?,?,?,?,?,?,?,?)",
                     [(mint, tf_sec, int(c[0]), c[1], c[2], c[3], c[4], c[5]) for c in candles])
    conn.commit()


def upsert_research_token(conn, mint, **kw):
    conn.execute("INSERT OR IGNORE INTO research_tokens(mint) VALUES (?)", (mint,))
    if kw:
        conn.execute(f"UPDATE research_tokens SET {', '.join(k + '=?' for k in kw)} WHERE mint=?",
                     (*kw.values(), mint))
    conn.commit()


def add_research_wallets(conn, addresses, via, priority=0.0):
    now = int(time.time())
    conn.executemany(
        """INSERT INTO research_wallets(address, discovered_via, priority, first_seen) VALUES (?,?,?,?)
           ON CONFLICT(address) DO UPDATE SET priority=MAX(priority, excluded.priority)""",
        [(a, via, priority, now) for a in addresses])
    conn.commit()


def save_report(conn, title, markdown, data, version=None):
    cur = conn.execute("INSERT INTO research_reports(ts, version, title, markdown, json) VALUES (?,?,?,?,?)",
                       (int(time.time()), version, title, markdown, json.dumps(data, default=float)))
    conn.commit()
    return cur.lastrowid
