"""Tables owned by safety/ (created next to core's schema; columns are only ever added)."""
import json
import time

from core import db

SCHEMA = """
CREATE TABLE IF NOT EXISTS safety_tokens (
    mint TEXT PRIMARY KEY, ts INTEGER, score REAL, level TEXT, blocked INTEGER DEFAULT 0,
    coverage REAL, hard_flags_json TEXT, reasons_json TEXT, positives_json TEXT, checks_json TEXT,
    creator TEXT, pair_created_at INTEGER, version TEXT, calls INTEGER, error TEXT
);
CREATE TABLE IF NOT EXISTS safety_history (
    mint TEXT, ts INTEGER, score REAL, level TEXT, PRIMARY KEY (mint, ts)
);
CREATE TABLE IF NOT EXISTS safety_overrides (
    mint TEXT PRIMARY KEY, action TEXT, reason TEXT, ts INTEGER
);
CREATE TABLE IF NOT EXISTS safety_wallets (
    address TEXT PRIMARY KEY, n_txs INTEGER, complete INTEGER, first_ts INTEGER, last_ts INTEGER,
    funder TEXT, funded_sol REAL, checked_at INTEGER
);
CREATE TABLE IF NOT EXISTS safety_risks (
    mint TEXT, ts INTEGER, risks_json TEXT, PRIMARY KEY (mint, ts)
);
CREATE TABLE IF NOT EXISTS safety_outcomes (
    mint TEXT PRIMARY KEY, assessed_ts INTEGER, score REAL, level TEXT, risks_json TEXT, price0 REAL,
    liq0 REAL, min_ret REAL, ret_end REAL, liq_drop REAL, n_points INTEGER, rugged INTEGER, labelled_at INTEGER,
    rug_after_min REAL, max_ret REAL
);
CREATE TABLE IF NOT EXISTS safety_weights (
    check_name TEXT PRIMARY KEY, weight REAL, n_flagged INTEGER, rug_rate_flagged REAL, rug_rate_clean REAL,
    base_rate REAL, lift REAL, ts INTEGER
);
CREATE TABLE IF NOT EXISTS safety_cache (
    key TEXT PRIMARY KEY, ts INTEGER, json TEXT
);
CREATE TABLE IF NOT EXISTS safety_bad_wallets (
    address TEXT PRIMARY KEY, role TEXT, note TEXT, added_at INTEGER
);
CREATE TABLE IF NOT EXISTS safety_launch (
    mint TEXT PRIMARY KEY, creator TEXT, created_slot INTEGER, created_ts INTEGER,
    reachable INTEGER, launch_json TEXT, checked_at INTEGER
);
"""


# columns added after the first release: (table, column, type)
MIGRATIONS = [("safety_tokens", "deep_ts", "INTEGER")]


_repaired = set()   # id() of connections whose pair_created_at rows were repaired


def ensure_schema(conn):
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='safety_bad_wallets'").fetchone() is None:
        db.retry_locked(conn, lambda: conn.executescript(SCHEMA))
    for table, col, typ in MIGRATIONS:
        if col not in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
            def add(table=table, col=col, typ=typ):
                if col not in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
                conn.commit()
            db.retry_locked(conn, add)
    if id(conn) not in _repaired:
        # pair_created_at stored as ms/1000 of a seconds value (January 1970): back to seconds, once, idempotent
        def fix():
            conn.execute("UPDATE safety_tokens SET pair_created_at = pair_created_at * 1000 "
                         "WHERE pair_created_at BETWEEN 1 AND 100000000")
            conn.commit()
        db.retry_locked(conn, fix)
        _repaired.add(id(conn))
    return conn


def connect(path=None):
    return ensure_schema(db.connect(path))


def _j(v):
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"))


def save_assessment(conn, a):
    def write():
        conn.execute(
            """INSERT OR REPLACE INTO safety_tokens(mint, ts, score, level, blocked, coverage, hard_flags_json,
                   reasons_json, positives_json, checks_json, creator, pair_created_at, version, calls, error, deep_ts)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (a.mint, a.ts, a.score, a.level, int(a.blocked), a.coverage, _j(a.hard_flags), _j(a.reasons),
             _j(a.positives), _j(a.checks), a.creator, a.pair_created_at, a.version, a.calls, a.error, a.deep_ts))
        conn.execute("INSERT OR REPLACE INTO safety_history (mint, ts, score, level) VALUES (?,?,?,?)", (a.mint, a.ts, a.score, a.level))
        # per-check risks at that time, for learning (own table: others insert into safety_history positionally)
        conn.execute("INSERT OR REPLACE INTO safety_risks (mint, ts, risks_json) VALUES (?,?,?)", (a.mint, a.ts, _j(
            {n: c.get("risk") for n, c in a.checks.items() if c.get("status") == "ok"})))
        conn.commit()
    db.retry_locked(conn, write)


def load_row(conn, mint):
    return conn.execute("SELECT * FROM safety_tokens WHERE mint=?", (mint,)).fetchone()


def set_override(conn, mint, action, reason=""):
    def write():
        if action is None:
            conn.execute("DELETE FROM safety_overrides WHERE mint=?", (mint,))
        else:
            conn.execute("INSERT OR REPLACE INTO safety_overrides (mint, action, reason, ts) VALUES (?,?,?,?)",
                         (mint, action, reason, int(time.time())))
        conn.commit()
    db.retry_locked(conn, write)


def override(conn, mint):
    r = conn.execute("SELECT action FROM safety_overrides WHERE mint=?", (mint,)).fetchone()
    return r["action"] if r else None


def wallet_profile(conn, address, max_age):
    r = conn.execute("SELECT * FROM safety_wallets WHERE address=?", (address,)).fetchone()
    if r and r["checked_at"] and time.time() - r["checked_at"] < max_age:
        return dict(r)
    return None


def save_wallet_profile(conn, p):
    def write():
        conn.execute("INSERT OR REPLACE INTO safety_wallets (address, n_txs, complete, first_ts, last_ts, funder, funded_sol, checked_at) "
                     "VALUES (?,?,?,?,?,?,?,?)",
                     (p["address"], p["n_txs"], int(p["complete"]), p["first_ts"], p["last_ts"], p["funder"],
                      p["funded_sol"], int(time.time())))
        conn.commit()
    db.retry_locked(conn, write)


def launch(conn, mint):
    r = conn.execute("SELECT * FROM safety_launch WHERE mint=?", (mint,)).fetchone()
    if not r:
        return None
    d = json.loads(r["launch_json"] or "{}")
    d.update(creator=r["creator"], created_slot=r["created_slot"], created_ts=r["created_ts"],
             reachable=bool(r["reachable"]))
    return d


def save_launch(conn, mint, d):
    rest = {k: v for k, v in d.items() if k not in ("creator", "created_slot", "created_ts", "reachable")}

    def write():
        conn.execute("INSERT OR REPLACE INTO safety_launch (mint, creator, created_slot, created_ts, reachable, launch_json, checked_at) "
                     "VALUES (?,?,?,?,?,?,?)",
                     (mint, d.get("creator"), d.get("created_slot"), d.get("created_ts"),
                      int(bool(d.get("reachable"))), _j(rest), int(time.time())))
        conn.commit()
    db.retry_locked(conn, write)


def cache_get(conn, key, max_age):
    r = conn.execute("SELECT ts, json FROM safety_cache WHERE key=?", (key,)).fetchone()
    if r and time.time() - r["ts"] < max_age:
        return json.loads(r["json"])
    return None


def cache_put(conn, key, value):
    def write():
        conn.execute("INSERT OR REPLACE INTO safety_cache (key, ts, json) VALUES (?,?,?)", (key, int(time.time()), _j(value)))
        conn.execute("DELETE FROM safety_cache WHERE ts < ?", (int(time.time()) - 86400,))
        conn.commit()
    db.retry_locked(conn, write)


def weights(conn):
    """Learned multiplier per check (1.0 = untouched), see safety/learn.py."""
    try:
        return {r["check_name"]: r["weight"] for r in conn.execute("SELECT check_name, weight FROM safety_weights")}
    except Exception:  # noqa: BLE001
        return {}
