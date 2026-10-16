"""Tables owned by sniper/ (prefix sniper_, created next to core's schema; columns are only ever added)."""
import json
import time

from core import db

SCHEMA = """
CREATE TABLE IF NOT EXISTS sniper_launches (
    mint TEXT PRIMARY KEY, source TEXT, pool TEXT, symbol TEXT, name TEXT, uri TEXT, creator TEXT,
    created_ts INTEGER, create_sig TEXT, create_slot INTEGER,
    decided_ts INTEGER, stage INTEGER, decision TEXT, skip_reason TEXT,
    score REAL, momentum REAL, risk REAL, prescore REAL,
    bundle_flag INTEGER DEFAULT 0, insider_flag INTEGER DEFAULT 0, hard_flag INTEGER DEFAULT 0,
    n_trades INTEGER, n_buyers INTEGER, n_sellers INTEGER, buy_sol REAL, sell_sol REAL, net_sol REAL,
    dev_buy_pct REAL, dev_sold_pct REAL, bundle_wallets INTEGER, bundle_pct REAL, unseen_pct REAL,
    top10_pct REAL, fast_buyers INTEGER, shared_funder_groups INTEGER, dev_launches INTEGER,
    dev_launches_24h INTEGER, known_bad INTEGER, n_ultra INTEGER, n_smart INTEGER, n_good INTEGER,
    curve_progress REAL, has_socials INTEGER, safety_risk REAL, safety_level TEXT, model_p REAL,
    mcap_decide_sol REAL, mcap_decide_usd REAL, sol_usd REAL,
    reasons_json TEXT, positives_json TEXT, features_json TEXT, calls_rpc INTEGER, calls_api INTEGER,
    migrated INTEGER DEFAULT 0, migrated_ts INTEGER,
    ret_1m REAL, ret_5m REAL, ret_15m REAL, ret_60m REAL, max_ret_60m REAL, min_ret_60m REAL,
    mcap_max_sol REAL, outcome_src TEXT, rugged INTEGER, done INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_sniper_launches_ts ON sniper_launches(decided_ts);
CREATE INDEX IF NOT EXISTS idx_sniper_launches_creator ON sniper_launches(creator);
CREATE TABLE IF NOT EXISTS sniper_launch_wallets (
    mint TEXT, wallet TEXT, role TEXT, slot INTEGER, ts REAL, tokens REAL, sol REAL, funder TEXT,
    sold_pct REAL, PRIMARY KEY (mint, wallet, role)
);
CREATE INDEX IF NOT EXISTS idx_sniper_lw_wallet ON sniper_launch_wallets(wallet);
CREATE TABLE IF NOT EXISTS sniper_curve (
    mint TEXT, ts INTEGER, mcap_sol REAL, v_sol REAL, n_trades INTEGER, PRIMARY KEY (mint, ts)
);
CREATE TABLE IF NOT EXISTS sniper_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, mint TEXT, symbol TEXT, mode TEXT DEFAULT 'paper',
    opened_ts INTEGER, entry_price_sol REAL, entry_mcap_sol REAL, entry_mcap_usd REAL, size_sol REAL,
    tokens REAL, remaining REAL, fees_sol REAL, fill_src TEXT, score REAL,
    peak_price_sol REAL, last_price_sol REAL, tp1_done INTEGER DEFAULT 0, realized_sol REAL DEFAULT 0,
    status TEXT DEFAULT 'open', closed_ts INTEGER, exit_reason TEXT, pnl_sol REAL, pnl_pct REAL,
    params_json TEXT, events_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_sniper_pos_status ON sniper_positions(status);
CREATE TABLE IF NOT EXISTS sniper_state (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS sniper_paths (
    mint TEXT PRIMARY KEY, created_ts INTEGER, decided_ts INTEGER, base_mcap_sol REAL, n_points INTEGER,
    points_json TEXT
);
"""


ADDED_COLS = {"sniper_launches": [("rug_p", "REAL")]}   # columns added after the first release


def ensure_schema(conn):
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='sniper_paths'").fetchone() is None:
        db.retry_locked(conn, lambda: conn.executescript(SCHEMA))
    for table, cols in ADDED_COLS.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, typ in cols:
            if name not in have:
                db.retry_locked(conn, lambda: conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}"))
    db.retry_locked(conn, lambda: conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sniper_launches_created ON sniper_launches(created_ts)"))
    global LAUNCH_COLS
    LAUNCH_COLS = None
    return conn


def connect(path=None):
    return ensure_schema(db.connect(path))


def j(v):
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"), default=str)


LAUNCH_COLS = None


def _cols(conn):
    global LAUNCH_COLS
    if LAUNCH_COLS is None:
        LAUNCH_COLS = {r[1] for r in conn.execute("PRAGMA table_info(sniper_launches)")}
    return LAUNCH_COLS


def save_launch(conn, row):
    """Insert or update one launch by column name (unknown keys are ignored)."""
    cols = _cols(conn)
    row = {k: v for k, v in row.items() if k in cols}
    keys = list(row)

    def write():
        conn.execute(
            f"INSERT INTO sniper_launches({','.join(keys)}) VALUES ({','.join('?' * len(keys))}) "
            f"ON CONFLICT(mint) DO UPDATE SET {','.join(f'{k}=excluded.{k}' for k in keys if k != 'mint')}",
            [row[k] for k in keys])
        conn.commit()
    db.retry_locked(conn, write)


def update_launch(conn, mint, **kw):
    cols = _cols(conn)
    kw = {k: v for k, v in kw.items() if k in cols}
    if not kw:
        return

    def write():
        conn.execute(f"UPDATE sniper_launches SET {','.join(f'{k}=?' for k in kw)} WHERE mint=?", [*kw.values(), mint])
        conn.commit()
    db.retry_locked(conn, write)


def save_wallets(conn, mint, rows):
    if not rows:
        return

    def write():
        conn.executemany(
            "INSERT OR REPLACE INTO sniper_launch_wallets VALUES (?,?,?,?,?,?,?,?,?)",
            [(mint, r["wallet"], r["role"], r.get("slot"), r.get("ts"), r.get("tokens"), r.get("sol"),
              r.get("funder"), r.get("sold_pct")) for r in rows])
        conn.commit()
    db.retry_locked(conn, write)


def save_curve(conn, mint, points):
    if not points:
        return

    def write():
        conn.executemany("INSERT OR REPLACE INTO sniper_curve VALUES (?,?,?,?,?)",
                         [(mint, int(p[0]), p[1], p[2], p[3]) for p in points])
        conn.commit()
    db.retry_locked(conn, write)


def save_path(conn, mint, created_ts, decided_ts, base_mcap_sol, points):
    """Bucketed mcap path ([[sec after creation, last, low, high], ...]) of a finished launch."""
    if not points or len(points) < 2:
        return

    def write():
        conn.execute("INSERT OR REPLACE INTO sniper_paths VALUES (?,?,?,?,?,?)",
                     (mint, int(created_ts or 0), int(decided_ts or 0), base_mcap_sol, len(points),
                      json.dumps(points, separators=(",", ":"))))
        conn.commit()
    db.retry_locked(conn, write)


def load_paths(conn, mints=None):
    """{mint: points} for the given mints (all when None)."""
    if mints is None:
        rows = conn.execute("SELECT mint, points_json FROM sniper_paths").fetchall()
    else:
        rows, mints = [], list(mints)
        for i in range(0, len(mints), 500):
            part = mints[i:i + 500]
            rows += conn.execute(f"SELECT mint, points_json FROM sniper_paths WHERE mint IN ({','.join('?' * len(part))})",
                                 part).fetchall()
    return {r[0]: json.loads(r[1]) for r in rows}


def get_state(conn, key, default=None):
    r = conn.execute("SELECT value FROM sniper_state WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def set_state(conn, key, value):
    def write():
        conn.execute("INSERT OR REPLACE INTO sniper_state VALUES (?,?)", (key, str(value)))
        conn.commit()
    db.retry_locked(conn, write)


def creator_history(conn, creator, now=None):
    """What our own records say about earlier launches of this dev (no network)."""
    now = now or time.time()
    r = conn.execute(
        """SELECT COUNT(*) n, SUM(created_ts >= ?) n24, SUM(COALESCE(rugged,0)) rugs, SUM(COALESCE(migrated,0)) mig,
                  MAX(mcap_max_sol) best, SUM(dev_sold_pct >= 50) dumps
           FROM sniper_launches WHERE creator=?""", (now - 86400, creator)).fetchone()
    return {k: (r[k] or 0) for k in ("n", "n24", "rugs", "mig", "best", "dumps")}


def known_bad(conn, wallets):
    """wallet -> number of earlier rugged launches where it was a bundle/insider/dev wallet."""
    wallets = [w for w in wallets if w]
    if not wallets:
        return {}
    out = {}
    for i in range(0, len(wallets), 400):
        chunk = wallets[i:i + 400]
        q = f"""SELECT w.wallet, COUNT(DISTINCT w.mint) n FROM sniper_launch_wallets w
                JOIN sniper_launches l ON l.mint = w.mint
                WHERE w.wallet IN ({','.join('?' * len(chunk))}) AND w.role IN ('bundle','insider','dev')
                  AND l.rugged = 1 GROUP BY w.wallet"""
        out.update({r["wallet"]: r["n"] for r in conn.execute(q, chunk)})
    return out


def cached_funders(conn, wallets):
    """wallet -> funder from the research funding table and the safety wallet cache (no network)."""
    wallets = [w for w in wallets if w]
    out = {}
    for table, col in (("research_funding", "wallet"), ("safety_wallets", "address")):
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone() is None:
            continue
        for i in range(0, len(wallets), 400):
            chunk = [w for w in wallets[i:i + 400] if w not in out]
            if not chunk:
                continue
            for r in conn.execute(f"SELECT {col} w, funder FROM {table} WHERE {col} IN ({','.join('?' * len(chunk))})"
                                  " AND funder IS NOT NULL AND funder != ''", chunk):
                out[r["w"]] = r["funder"]
    return out


def _has_col(conn, table, col):
    return col in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def wallet_clusters(conn, wallets):
    """wallet -> cluster_id (same funding source = one entity): wallets.cluster_id, else model_wallet_stats."""
    wallets = [w for w in wallets if w]
    out = {}
    for table, col in (("wallets", "address"), ("model_wallet_stats", "address")):
        try:
            if not _has_col(conn, table, "cluster_id"):
                continue
        except Exception:  # noqa: BLE001
            continue
        for i in range(0, len(wallets), 400):
            chunk = [w for w in wallets[i:i + 400] if w not in out]
            if chunk:
                for r in conn.execute(f"SELECT {col} a, cluster_id FROM {table} WHERE {col} IN "
                                      f"({','.join('?' * len(chunk))}) AND cluster_id IS NOT NULL AND cluster_id != ''", chunk):
                    out[r["a"]] = r["cluster_id"]
    return out


def tier_map(conn):
    """wallet -> tier ('ultra'|'smart'|'good'): wallets.tier is the one truth (synced from model/ when trained)."""
    try:
        if _has_col(conn, "wallets", "tier"):
            out = {r[0]: r[1] for r in conn.execute("SELECT address, tier FROM wallets WHERE tier IS NOT NULL")}
            if out:
                return out
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='model_wallet_stats'").fetchone():
            return {r[0]: r[1] for r in conn.execute("SELECT address, tier FROM model_wallet_stats WHERE tier IS NOT NULL")}
    except Exception:  # noqa: BLE001 - missing tables are not an error
        pass
    return {}


def prune(conn, keep_days, now=None):
    cut = int((now or time.time()) - keep_days * 86400)

    def write():
        conn.execute("DELETE FROM sniper_curve WHERE ts < ?", (cut,))
        conn.execute("DELETE FROM sniper_paths WHERE created_ts < ?", (cut,))
        # stage-1 skips are many and small; stage 2 and entries are kept for learning
        conn.execute("DELETE FROM sniper_launches WHERE decided_ts < ? AND stage = 1", (cut,))
        conn.commit()
    db.retry_locked(conn, write)
