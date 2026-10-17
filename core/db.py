"""SQLite storage. Schema is created on connect; columns are only ever added, never removed."""
import sqlite3
import time
from pathlib import Path

from core.config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS tokens (
    mint TEXT PRIMARY KEY, symbol TEXT, name TEXT, pool_address TEXT, dex TEXT,
    price_usd REAL, mcap_usd REAL, liquidity_usd REAL, pair_created_at INTEGER,
    price_change_m5 REAL, price_change_h1 REAL, price_change_h24 REAL, volume_h1 REAL,
    first_seen INTEGER, last_updated INTEGER, tracked INTEGER DEFAULT 1, last_trades_fetch INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS token_prices (
    mint TEXT, ts INTEGER, price_usd REAL, mcap_usd REAL, liquidity_usd REAL,
    PRIMARY KEY (mint, ts)
);
CREATE TABLE IF NOT EXISTS trades (
    sig TEXT, wallet TEXT, mint TEXT, side TEXT, ts INTEGER,
    token_amount REAL, sol_amount REAL, usd_amount REAL, price_usd REAL, source TEXT,
    PRIMARY KEY (sig, wallet, mint, side)
);
CREATE INDEX IF NOT EXISTS idx_trades_mint_ts ON trades(mint, ts);
CREATE INDEX IF NOT EXISTS idx_trades_wallet_ts ON trades(wallet, ts);
CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(ts);
CREATE INDEX IF NOT EXISTS idx_token_prices_ts ON token_prices(ts);
CREATE TABLE IF NOT EXISTS wallets (
    address TEXT PRIMARY KEY, label TEXT DEFAULT '', source TEXT DEFAULT 'auto', manual INTEGER DEFAULT 0,
    score REAL DEFAULT 0, is_smart INTEGER DEFAULT 0, is_bot INTEGER DEFAULT 0,
    tokens_traded INTEGER DEFAULT 0, win_rate REAL, early_rate REAL, realized_pnl_usd REAL,
    total_pnl_usd REAL, roi REAL, median_peak_multiple REAL, consistency REAL,
    last_trade_ts INTEGER, backfilled_at INTEGER DEFAULT 0, first_seen INTEGER, updated_at INTEGER
);
CREATE TABLE IF NOT EXISTS wallet_positions (
    wallet TEXT, mint TEXT, buy_usd REAL, sell_usd REAL, buy_tokens REAL, sell_tokens REAL,
    realized_pnl_usd REAL, unrealized_pnl_usd REAL, roi REAL, first_buy_ts INTEGER,
    first_buy_price REAL, peak_multiple REAL, trades INTEGER,
    PRIMARY KEY (wallet, mint)
);
CREATE TABLE IF NOT EXISTS signal_metrics (
    ts INTEGER, mint TEXT, window_min INTEGER, buy_usd REAL, sell_usd REAL, net_usd REAL,
    smart_buyers INTEGER, avg_quality REAL, score REAL, price_usd REAL,
    PRIMARY KEY (ts, mint)
);
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, mint TEXT, symbol TEXT, score REAL,
    net_usd REAL, buy_usd REAL, smart_buyers INTEGER, avg_quality REAL, price_usd REAL, mcap_usd REAL,
    wallets_json TEXT, notified INTEGER DEFAULT 0,
    ret_5m REAL, ret_15m REAL, ret_1h REAL, max_ret_1h REAL
);
CREATE INDEX IF NOT EXISTS idx_signals_mint ON signals(mint, ts);
CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(ts);
CREATE INDEX IF NOT EXISTS idx_signal_metrics_mint_ts ON signal_metrics(mint, ts);
CREATE TABLE IF NOT EXISTS signal_shadow (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, mint TEXT, kind TEXT, reason TEXT, entities INTEGER,
    net_usd REAL, score REAL, min_net REAL, min_score REAL, safety_risk REAL, liquidity_usd REAL, price_usd REAL,
    model_exp_net REAL, ret_1h REAL, max_ret_1h REAL, max_ret_4h REAL, outcome_flag TEXT
);
CREATE INDEX IF NOT EXISTS idx_signal_shadow_ts ON signal_shadow(ts);
CREATE INDEX IF NOT EXISTS idx_signal_shadow_mint ON signal_shadow(mint, reason, ts);
"""

# columns added after the first release: (table, column, type). Applied on connect, idempotent.
MIGRATIONS = [
    ("wallets", "tier_source", "TEXT"),             # 'model' (synced from model_wallet_stats) | 'heuristic'
    ("wallets", "tier_version", "INTEGER"),         # model_versions.id the tier came from
    ("wallets", "model_score", "REAL"),
    ("wallets", "cluster_id", "TEXT"),
    ("wallets", "exit_capture", "REAL"),
    ("signals", "score_raw", "REAL"),               # score before the safety penalty
    ("signals", "safety_factor", "REAL"),           # safety.signal_penalty factor (NULL = safety/ absent)
    ("signals", "safety_risk", "REAL"),             # risk 0-100, NULL = unchecked
    ("signals", "safety_level", "TEXT"),            # low | caution | high | critical | unchecked
    ("signals", "safety_reasons_json", "TEXT"),     # up to 3 German reasons, worst first
    ("tokens", "txns_m5", "INTEGER"),               # DexScreener buys+sells, last 5 min
    ("tokens", "txns_h1", "INTEGER"),
    ("wallets", "tier", "TEXT"),                    # 'ultra' | 'smart' | 'good' | NULL
    ("signal_metrics", "buy_sol", "REAL"),
    ("signal_metrics", "net_sol", "REAL"),
    ("signal_metrics", "mcap_usd", "REAL"),
    ("signal_metrics", "n_ultra", "INTEGER"),
    ("signal_metrics", "n_smart", "INTEGER"),
    ("signal_metrics", "n_good", "INTEGER"),
    ("signal_metrics", "prediction", "REAL"),
    ("signal_metrics", "contributions_json", "TEXT"),
    ("signals", "buy_sol", "REAL"),
    ("signals", "net_sol", "REAL"),
    ("signals", "liquidity_usd", "REAL"),
    ("signals", "n_ultra", "INTEGER"),
    ("signals", "n_smart", "INTEGER"),
    ("signals", "n_good", "INTEGER"),
    ("signals", "prediction", "REAL"),
    ("signals", "prediction_label", "TEXT"),
    ("signals", "contributions_json", "TEXT"),
    ("signals", "model_version", "TEXT"),
    ("signals", "max_mcap_1h", "REAL"),
    ("signals", "ret_6h", "REAL"),
    ("signals", "max_ret_24h", "REAL"),
    ("signals", "ev", "REAL"),                      # expected TP return after costs (signals.ev), NULL = no table
    ("signals", "ev_n", "INTEGER"),                 # replayed setups behind that EV
    ("signals", "reason", "TEXT"),
    ("signals", "outcome_flag", "TEXT"),            # ok | dead (pool died, counted as -95 %) | gap (no price near)                  # German plain-text why / why not
    ("signals", "max_ret_15m", "REAL"),             # peak after the call within 15 min
    ("signals", "max_ret_4h", "REAL"),              # ... within 4 h
    ("signals", "peak_min_1h", "REAL"),             # minutes from the call to the 1 h peak
    ("signals", "dd_before_peak_1h", "REAL"),       # lowest return before that peak (how deep it dipped first)
    ("signals", "call_no", "INTEGER"),              # 1 = first call on this coin, 2 = re-call on fresh smart inflow ...
    ("signals", "target_ret", "REAL"),              # take-profit mark sent with the alert (from past peaks)
    ("signals", "stop_ret", "REAL"),                # stop mark sent with the alert (from past dips)
    ("signals", "target_hit_ts", "INTEGER"),        # first time the price reached the target (follow-up alert sent)
    ("signals", "stop_hit_ts", "INTEGER"),          # first time the price fell to the stop before the target
    ("signals", "lag_first_s", "INTEGER"),          # seconds from the first smart buy in the window to the call
    ("signals", "lag_last_s", "INTEGER"),           # seconds from the newest smart buy to the call (data + cycle lag)
    ("signals", "kind", "TEXT"),
    # signal_shadow: market state at the rejection, so momentum / dip / venue can be checked as a later signal type
    ("signal_shadow", "dex", "TEXT"),
    ("signal_shadow", "mcap_usd", "REAL"),
    ("signal_shadow", "age_min", "REAL"),                # minutes since the pool was created
    ("signal_shadow", "pc_m5", "REAL"),                  # DexScreener price change 5 min / 1 h (percent)
    ("signal_shadow", "pc_h1", "REAL"),
    ("signal_shadow", "txns_m5", "INTEGER"),
    ("signal_shadow", "txns_h1", "INTEGER"),
    ("signal_shadow", "volume_h1", "REAL"),
    ("signal_shadow", "ret_15m_before", "REAL"),         # own prices: move over the 15 min before (dip or run-up)                    # NULL/'v1' = smart buying now, 'v2' = smart money accumulating (signals.accum)
    ("signal_metrics", "ev", "REAL"),
    ("signal_metrics", "model_exp_net", "REAL"),   # model's expected 1h return after costs (for later replays)
]


def _migrate(conn):
    known = {}
    for table, col, typ in MIGRATIONS:
        if table not in known:
            known[table] = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        cols = known[table]
        if col not in cols:
            cols.add(col)
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
            except sqlite3.OperationalError as e:  # ten services migrate at once after a deploy: one of them wins
                if "duplicate column" not in str(e).lower():
                    raise
    conn.commit()


BUSY_TIMEOUT_MS = 30000


def connect(path=None) -> sqlite3.Connection:
    """The one way every mayhambot process (signals, copytrade, model, dashboard) should open the DB:
    WAL (readers never block the writer), 30 s busy timeout, Row factory, schema + migrations applied.
    Keep write transactions short: commit right after writing, never hold one across a network call."""
    path = path or settings.db_path
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.OperationalError:
        pass  # e.g. network filesystems
    conn.executescript(SCHEMA)
    _migrate(conn)
    _optimize(conn)
    return conn


_optimized = {}


def _optimize(conn, every_s=6 * 3600):
    """Keep the query planner's statistics fresh (without them SQLite picked idx_trades_mint_ts for a ts-range
    query and scanned all trades). Bounded work (analysis_limit), at most every 6 h per process and file."""
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    if time.time() - _optimized.get(path, 0) < every_s:
        return
    try:
        conn.execute("PRAGMA analysis_limit=1000")
        conn.execute("PRAGMA optimize=0x10002")  # 0x10000: also tables never analysed (fresh files)
        _optimized[path] = time.time()
    except sqlite3.OperationalError:
        pass  # locked: the next connect tries again


def retry_locked(conn, fn, attempts=4):
    """Run a write step again if another process held the DB longer than the busy timeout,
    instead of losing the whole cycle. fn must be safe to repeat (it is rolled back first)."""
    for i in range(attempts):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            if "locked" not in str(e) and "busy" not in str(e) or i == attempts - 1:
                raise
            conn.rollback()
            time.sleep(2 * (i + 1))


PRICE_OUTLIER_X = 50      # a trade priced this many times off the market price is a parse error
PRICE_FRESH_S = 3600      # ... judged only against a market price this close to the trade


def clean_trades(conn, trades):
    """Drop what is not a real trade: a buy and a sell of the same token by the same wallet in one transaction
    (MEV / volume bot round trip), and a trade whose price is PRICE_OUTLIER_X off the token's market price at
    about the same time (mis-parsed SOL leg, e.g. 9,990 SOL 'buys' on a pump.fun curve)."""
    sides = {}
    for t in trades:
        sides.setdefault((t.sig, t.wallet, t.mint), set()).add(t.side)
    mints = list({t.mint for t in trades})
    ref = {}
    for i in range(0, len(mints), 500):
        chunk = mints[i:i + 500]
        ref.update({r[0]: (r[1], r[2]) for r in conn.execute(
            f"SELECT mint, price_usd, last_updated FROM tokens WHERE mint IN ({','.join('?' * len(chunk))})", chunk)})
    out = []
    for t in trades:
        if len(sides[(t.sig, t.wallet, t.mint)]) > 1:
            continue
        p, at = ref.get(t.mint, (None, None))
        if p and t.price_usd and at and abs(int(t.ts) - at) <= PRICE_FRESH_S \
                and max(t.price_usd / p, p / t.price_usd) > PRICE_OUTLIER_X:
            continue
        out.append(t)
    return out


def insert_trades(conn, trades) -> int:
    trades = clean_trades(conn, list(trades))
    before = conn.total_changes
    conn.executemany(
        "INSERT OR IGNORE INTO trades (sig, wallet, mint, side, ts, token_amount, sol_amount, usd_amount, price_usd, source) VALUES (?,?,?,?,?,?,?,?,?,?)",
        [(t.sig, t.wallet, t.mint, t.side, int(t.ts), t.token_amount, t.sol_amount,
          t.usd_amount, t.price_usd, t.source) for t in trades],
    )
    added = conn.total_changes - before
    now = int(time.time())
    conn.executemany(
        "INSERT OR IGNORE INTO wallets(address, source, first_seen, updated_at) VALUES (?, 'auto', ?, ?)",
        [(w, now, now) for w in {t.wallet for t in trades}],
    )
    conn.commit()
    return added


def upsert_tokens(conn, tokens, tracked=None):
    """tracked=None/True marks tokens as watched; False only stores price data (never un-watches)."""
    now = int(time.time())
    for t in tokens:
        conn.execute(
            """INSERT INTO tokens(mint, symbol, name, pool_address, dex, price_usd, mcap_usd, liquidity_usd,
                   pair_created_at, price_change_m5, price_change_h1, price_change_h24, volume_h1,
                   txns_m5, txns_h1, first_seen, last_updated, tracked)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(mint) DO UPDATE SET
                   symbol=COALESCE(NULLIF(excluded.symbol,''), symbol),
                   name=COALESCE(NULLIF(excluded.name,''), name),
                   pool_address=COALESCE(NULLIF(excluded.pool_address,''), pool_address),
                   dex=COALESCE(NULLIF(excluded.dex,''), dex),
                   price_usd=excluded.price_usd, mcap_usd=excluded.mcap_usd,
                   liquidity_usd=excluded.liquidity_usd,
                   pair_created_at=COALESCE(NULLIF(excluded.pair_created_at,0), pair_created_at),
                   price_change_m5=excluded.price_change_m5, price_change_h1=excluded.price_change_h1,
                   price_change_h24=excluded.price_change_h24, volume_h1=excluded.volume_h1,
                   txns_m5=excluded.txns_m5, txns_h1=excluded.txns_h1,
                   last_updated=excluded.last_updated,
                   tracked=MAX(COALESCE(tracked,0), ?)""",
            (t.mint, t.symbol, t.name, t.pool_address, t.dex, t.price_usd, t.mcap_usd, t.liquidity_usd,
             t.pair_created_at, t.price_change_m5, t.price_change_h1, t.price_change_h24, t.volume_h1,
             t.txns_m5, t.txns_h1, now, now, 1 if tracked is None else int(tracked), 1 if tracked is None else int(tracked)),
        )
        if t.price_usd:
            conn.execute("INSERT OR REPLACE INTO token_prices (mint, ts, price_usd, mcap_usd, liquidity_usd) VALUES (?,?,?,?,?)",
                         (t.mint, now, t.price_usd, t.mcap_usd, t.liquidity_usd))
    conn.commit()


def latest_price(conn, mint):
    r = conn.execute("SELECT price_usd FROM tokens WHERE mint=?", (mint,)).fetchone()
    return r["price_usd"] if r and r["price_usd"] else None


def trades_for_mint(conn, mint, since_ts=None):
    return conn.execute("SELECT * FROM trades WHERE mint=? AND ts>=? ORDER BY ts",
                        (mint, since_ts or 0)).fetchall()


def trades_for_wallet(conn, wallet, since_ts=None):
    return conn.execute("SELECT * FROM trades WHERE wallet=? AND ts>=? ORDER BY ts",
                        (wallet, since_ts or 0)).fetchall()
