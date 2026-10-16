"""Tables owned by intel/ (created next to core's schema; columns are only ever added, never renamed).
Other packages read them through the functions in intel/__init__.py."""
import json
import time

from core import db

SCHEMA = """
CREATE TABLE IF NOT EXISTS intel_snapshots (
    kind TEXT, key TEXT, ts INTEGER, coverage REAL, features_json TEXT, meta_json TEXT,
    ret_1h REAL, max_ret_1h REAL, min_ret_1h REAL, labeled_at INTEGER,
    PRIMARY KEY (kind, key, ts)
);
CREATE INDEX IF NOT EXISTS idx_intel_snap_ts ON intel_snapshots(kind, ts);
CREATE TABLE IF NOT EXISTS intel_latest (
    kind TEXT, key TEXT, ts INTEGER, coverage REAL, features_json TEXT, meta_json TEXT,
    PRIMARY KEY (kind, key)
);
CREATE TABLE IF NOT EXISTS intel_holders (
    mint TEXT, safety_ts INTEGER, seen_ts INTEGER, top10_pct REAL, top1_pct REAL, insider_pct REAL,
    dev_pct REAL, bundle_held_pct REAL, launch_pct REAL, roundtrip_share REAL, safety_score REAL, blocked INTEGER,
    PRIMARY KEY (mint, safety_ts)
);
CREATE TABLE IF NOT EXISTS intel_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, kind TEXT, mint TEXT, wallet TEXT, severity INTEGER,
    title TEXT, text TEXT, data_json TEXT, dedup TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_intel_events_ts ON intel_events(ts);
CREATE INDEX IF NOT EXISTS idx_intel_events_mint ON intel_events(mint, ts);
CREATE TABLE IF NOT EXISTS intel_state (key TEXT PRIMARY KEY, value TEXT);
"""

MIGRATIONS = []


def ensure_schema(conn):
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='intel_state'").fetchone() is None:
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


def has(conn, table):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def j(v):
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"), default=str)


def _round(v):
    return round(v, 6) if isinstance(v, float) else v


def save_features(conn, kind, rows, persist):
    """rows: [{key, ts, coverage, features, meta}]. Always updates intel_latest; persist=True also appends the
    point-in-time history row (intel_snapshots) that the evaluation and the brain's walk-forward test read."""
    if not rows:
        return 0
    packed = [(kind, r["key"], r["ts"], r.get("coverage"), j({k: _round(v) for k, v in r["features"].items()}),
               j(r.get("meta") or {})) for r in rows]

    def write():
        conn.executemany("INSERT OR REPLACE INTO intel_latest(kind,key,ts,coverage,features_json,meta_json) "
                         "VALUES (?,?,?,?,?,?)", packed)
        if persist:
            conn.executemany("INSERT OR IGNORE INTO intel_snapshots(kind,key,ts,coverage,features_json,meta_json) "
                             "VALUES (?,?,?,?,?,?)", packed)
        conn.commit()
    db.retry_locked(conn, write)
    return len(rows)


def save_holders(conn, rows):
    if not rows:
        return 0
    cols = list(rows[0].keys())

    def write():
        conn.executemany(f"INSERT OR IGNORE INTO intel_holders({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                         [tuple(r.get(c) for c in cols) for r in rows])
        conn.commit()
    db.retry_locked(conn, write)
    return len(rows)


def add_events(conn, events):
    """events: [{ts, kind, mint, wallet, severity, title, text, data, dedup}] -> number of new ones."""
    if not events:
        return 0
    before = conn.total_changes

    def write():
        conn.executemany(
            "INSERT OR IGNORE INTO intel_events(ts,kind,mint,wallet,severity,title,text,data_json,dedup) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            [(e["ts"], e["kind"], e.get("mint"), e.get("wallet"), e.get("severity", 1), e["title"], e.get("text", ""),
              j(e.get("data") or {}), e["dedup"]) for e in events])
        conn.commit()
    db.retry_locked(conn, write)
    return conn.total_changes - before


def get_state(conn, key):
    r = conn.execute("SELECT value FROM intel_state WHERE key=?", (key,)).fetchone()
    return json.loads(r[0]) if r else None


def set_state(conn, key, value):
    def write():
        conn.execute("INSERT OR REPLACE INTO intel_state(key,value) VALUES (?,?)", (key, j(value)))
        conn.commit()
    db.retry_locked(conn, write)


def prune(conn, keep_days, now=None):
    cut = int(now or time.time()) - keep_days * 86400

    def write():
        conn.execute("DELETE FROM intel_snapshots WHERE ts<?", (cut,))
        conn.execute("DELETE FROM intel_events WHERE ts<?", (cut,))
        conn.execute("DELETE FROM intel_holders WHERE seen_ts<?", (cut - 7 * 86400,))
        conn.execute("DELETE FROM intel_latest WHERE ts<?", (cut,))
        conn.commit()
    db.retry_locked(conn, write)


# Time-range reads of shared tables the Zentrale does every minute. Without them SQLite scans the whole table
# (trades has only (mint, ts) and (wallet, ts)), which is what showed up as CPU spikes on the Mac.
INDEXES = (("trades", "idx_trades_ts", "ts"), ("token_prices", "idx_token_prices_ts", "ts"),
           ("safety_tokens", "idx_safety_tokens_ts", "ts"))


def ensure_indexes(conn):
    made = []
    for table, name, cols in INDEXES:
        if has(conn, table) and not conn.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (name,)).fetchone():
            def write(table=table, name=name, cols=cols):
                conn.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {table}({cols})")
                conn.commit()
            db.retry_locked(conn, write)
            made.append(name)
    return made
