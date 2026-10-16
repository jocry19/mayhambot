"""Tables owned by market/ (created next to core's schema; columns are only ever added, never renamed)."""
import json
import time

from core import db

SCHEMA = """
CREATE TABLE IF NOT EXISTS market_snapshots (
    ts INTEGER PRIMARY KEY, score REAL, label TEXT, coverage REAL, components_json TEXT,
    sol_usd REAL, sol_ret_1h REAL, sol_ret_24h REAL, sol_ret_7d REAL, sol_vol_24h REAL,
    meme_mcap_usd REAL, meme_mcap_chg_24h REAL, breadth REAL, universe_n INTEGER, volume_h1_usd REAL,
    volume_ratio REAL, net_flow_usd REAL, launches_1h INTEGER, launch_ratio REAL, launch_hit2x REAL,
    launch_rug REAL, launch_migrated REAL, smart_net_usd REAL, smart_buyers INTEGER,
    top_sector TEXT, top_term TEXT, backfilled INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS market_sectors (
    ts INTEGER, sector TEXT, n_tokens INTEGER, mcap_usd REAL, volume_h1_usd REAL, vol_share REAL,
    vol_share_chg REAL, net_flow_usd REAL, median_ret_h1 REAL, breadth REAL, smart_net_usd REAL,
    smart_buyers INTEGER, launches INTEGER, launch_hit2x REAL, heat REAL, top_json TEXT,
    PRIMARY KEY (ts, sector)
);
CREATE INDEX IF NOT EXISTS idx_market_sectors_sector ON market_sectors(sector, ts);
CREATE TABLE IF NOT EXISTS market_terms (
    ts INTEGER, term TEXT, n_recent INTEGER, n_base INTEGER, lift REAL, volume_h1_usd REAL,
    hit2x REAL, rug REAL, n_outcomes INTEGER, heat REAL, mints_json TEXT, PRIMARY KEY (ts, term)
);
CREATE TABLE IF NOT EXISTS market_buckets (
    ts INTEGER, kind TEXT, bucket TEXT, n INTEGER, volume_h1_usd REAL, vol_share REAL,
    net_flow_usd REAL, median_ret_h1 REAL, smart_net_usd REAL, PRIMARY KEY (ts, kind, bucket)
);
CREATE TABLE IF NOT EXISTS market_tokens (
    mint TEXT PRIMARY KEY, symbol TEXT, name TEXT, description TEXT, sectors_json TEXT, terms_json TEXT,
    n_socials INTEGER, boosted REAL, dex TEXT, pair_created_at INTEGER, mcap_usd REAL, liquidity_usd REAL,
    price_change_h1 REAL, price_change_h24 REAL, volume_h1 REAL, volume_h24 REAL,
    buys_h1 INTEGER, sells_h1 INTEGER, source TEXT, first_seen INTEGER, updated_at INTEGER
);
CREATE TABLE IF NOT EXISTS market_sol (ts INTEGER PRIMARY KEY, price_usd REAL);
CREATE TABLE IF NOT EXISTS market_state (key TEXT PRIMARY KEY, value TEXT);
"""

# columns added after the first release: (table, column, type)
MIGRATIONS = [
    ("market_snapshots", "regime", "TEXT"), ("market_snapshots", "regime_since", "INTEGER"),
    ("market_snapshots", "score_ema", "REAL"), ("market_snapshots", "slope_1h", "REAL"),
    ("market_snapshots", "slope_3h", "REAL"), ("market_snapshots", "trend", "TEXT"),
    ("market_terms", "stage", "TEXT"), ("market_terms", "first_seen", "INTEGER"), ("market_terms", "peak_n", "INTEGER"),
    ("market_terms", "age_h", "REAL"), ("market_terms", "growth_1h", "REAL"),
]


# created on existing databases too (SCHEMA only runs once); prune and the universe filter on updated_at
INDEXES = [
    ("idx_market_tokens_updated", "CREATE INDEX IF NOT EXISTS idx_market_tokens_updated ON market_tokens(updated_at)"),
]


def ensure_schema(conn):
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='market_state'").fetchone() is None:
        db.retry_locked(conn, lambda: conn.executescript(SCHEMA))
    for table, col, typ in MIGRATIONS:
        if col not in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
            def add(table=table, col=col, typ=typ):
                if col not in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
                conn.commit()
            db.retry_locked(conn, add)
    for name, sql in INDEXES:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (name,)).fetchone() is None:
            def mk(sql=sql):
                conn.execute(sql)
                conn.commit()
            db.retry_locked(conn, mk)
    return conn


def connect(path=None):
    return ensure_schema(db.connect(path))


def has(conn, table):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def j(v):
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"))


def _insert(conn, table, rows, replace=True):
    rows = [r for r in rows if r]
    if not rows:
        return 0
    cols = list(rows[0].keys())
    sql = f"INSERT OR {'REPLACE' if replace else 'IGNORE'} INTO {table}({','.join(cols)}) VALUES ({','.join('?' * len(cols))})"

    def write():
        conn.executemany(sql, [tuple(r.get(c) for c in cols) for r in rows])
        conn.commit()
    db.retry_locked(conn, write)
    return len(rows)


def save_snapshot(conn, snap, sectors=(), terms=(), buckets=()):
    """One analysis round in one go: the snapshot row plus its sector, term and bucket rows."""
    _insert(conn, "market_snapshots", [snap])
    _insert(conn, "market_sectors", list(sectors))
    _insert(conn, "market_terms", list(terms))
    _insert(conn, "market_buckets", list(buckets))


def save_tokens(conn, rows):
    """Upsert token metadata; keeps first_seen and the description/boost of earlier rounds when the new row has none."""
    rows = [r for r in rows if r and r.get("mint")]
    if not rows:
        return 0
    old = {}
    mints = [r["mint"] for r in rows]
    for i in range(0, len(mints), 400):
        part = mints[i:i + 400]
        for r in conn.execute(f"SELECT * FROM market_tokens WHERE mint IN ({','.join('?' * len(part))})", part):
            old[r["mint"]] = dict(r)
    merged = []
    for r in rows:
        o = old.get(r["mint"]) or {}
        m = dict(o)
        m.update({k: v for k, v in r.items() if v is not None})
        m["first_seen"] = o.get("first_seen") or r.get("first_seen") or int(time.time())
        merged.append(m)
    cols = ["mint", "symbol", "name", "description", "sectors_json", "terms_json", "n_socials", "boosted", "dex",
            "pair_created_at", "mcap_usd", "liquidity_usd", "price_change_h1", "price_change_h24", "volume_h1",
            "volume_h24", "buys_h1", "sells_h1", "source", "first_seen", "updated_at"]
    return _insert(conn, "market_tokens", [{c: m.get(c) for c in cols} for m in merged])


def save_sol(conn, points):
    return _insert(conn, "market_sol", [{"ts": int(t), "price_usd": float(p)} for t, p in points if p])


def sol_series(conn, since, until=None):
    until = until or int(time.time()) + 1
    return [(r[0], r[1]) for r in conn.execute(
        "SELECT ts, price_usd FROM market_sol WHERE ts>=? AND ts<=? ORDER BY ts", (since, until))]


def set_state(conn, key, value):
    def write():
        conn.execute("INSERT OR REPLACE INTO market_state VALUES (?,?)", (key, value if isinstance(value, str) else j(value)))
        conn.commit()
    db.retry_locked(conn, write)


def get_state(conn, key, default=None):
    if not has(conn, "market_state"):
        return default
    r = conn.execute("SELECT value FROM market_state WHERE key=?", (key,)).fetchone()
    if not r:
        return default
    try:
        return json.loads(r[0])
    except (TypeError, ValueError):
        return r[0]


def snapshot_at(conn, ts=None, max_age=None):
    """Latest snapshot at or before `ts` (point in time: never a later one), None if missing or older than max_age."""
    if not has(conn, "market_snapshots"):
        return None
    ts = int(ts or time.time())
    r = conn.execute("SELECT * FROM market_snapshots WHERE ts<=? ORDER BY ts DESC LIMIT 1", (ts,)).fetchone()
    if not r or (max_age is not None and ts - r["ts"] > max_age):
        return None
    d = dict(r)
    d["components"] = json.loads(d.pop("components_json") or "{}")
    return d


def prune(conn, keep_days):
    cut = int(time.time()) - keep_days * 86400

    def write():
        for t in ("market_snapshots", "market_sectors", "market_terms", "market_buckets", "market_sol"):
            conn.execute(f"DELETE FROM {t} WHERE ts<?", (cut,))
        conn.execute("DELETE FROM market_tokens WHERE updated_at<?", (cut,))
        conn.commit()
    db.retry_locked(conn, write)
