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
CREATE TABLE IF NOT EXISTS safety_launch (
    mint TEXT PRIMARY KEY, creator TEXT, created_slot INTEGER, created_ts INTEGER,
    reachable INTEGER, launch_json TEXT, checked_at INTEGER
);
"""


# columns added after the first release: (table, column, type)
MIGRATIONS = [("safety_tokens", "deep_ts", "INTEGER")]


def ensure_schema(conn):
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='safety_launch'").fetchone() is None:
        db.retry_locked(conn, lambda: conn.executescript(SCHEMA))
    for table, col, typ in MIGRATIONS:
        if col not in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
            def add(table=table, col=col, typ=typ):
                if col not in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
                conn.commit()
            db.retry_locked(conn, add)
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
        conn.execute("INSERT OR REPLACE INTO safety_history VALUES (?,?,?,?)", (a.mint, a.ts, a.score, a.level))
        conn.commit()
    db.retry_locked(conn, write)


def load_row(conn, mint):
    return conn.execute("SELECT * FROM safety_tokens WHERE mint=?", (mint,)).fetchone()


def set_override(conn, mint, action, reason=""):
    def write():
        if action is None:
            conn.execute("DELETE FROM safety_overrides WHERE mint=?", (mint,))
        else:
            conn.execute("INSERT OR REPLACE INTO safety_overrides VALUES (?,?,?,?)",
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
        conn.execute("INSERT OR REPLACE INTO safety_wallets VALUES (?,?,?,?,?,?,?,?)",
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
        conn.execute("INSERT OR REPLACE INTO safety_launch VALUES (?,?,?,?,?,?,?)",
                     (mint, d.get("creator"), d.get("created_slot"), d.get("created_ts"),
                      int(bool(d.get("reachable"))), _j(rest), int(time.time())))
        conn.commit()
    db.retry_locked(conn, write)
