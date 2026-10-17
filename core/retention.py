"""Database size and retention: what each table takes on disk, how fast it grows, and the cleanup that keeps the
file in check without taking data the model, the wallet tiers or the backtests read.

Standard library only (deploy/ops.py imports it, and ops must run when the venv is broken).

  table_sizes(conn)            {table: {"rows": n, "bytes": table + its indexes}} via SQLite's dbstat
  log_sizes(conn, path, now)   hourly/daily entries in data/db_sizes.json, plus growth per table (bytes/day)
  size_report(...)             German text for /betrieb and `ops.py db-sizes`
"""
import json
import sqlite3
import time
from pathlib import Path

SIZE_LOG_DAYS = 60
TIME_COLS = ("ts", "seen_at", "created_ts", "detected_at", "seen_ts", "opened_at", "decided_ts", "first_seen")


def _time_col(conn, table):
    cols = {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}
    return next((c for c in TIME_COLS if c in cols), None)


def _val_len(v) -> int:
    """Approximate stored size of one value in a SQLite record (payload + header byte)."""
    if v is None:
        return 1
    if isinstance(v, (int, float)):
        return 9 if isinstance(v, float) or abs(v) > 2 ** 31 else 5
    return len(v if isinstance(v, bytes) else str(v).encode()) + 2


def _estimate_bytes(conn, table, rows, sample=2000) -> int:
    """Table + index bytes from the newest `sample` rows' value sizes (SQLite builds without dbstat, e.g. the
    Mac's Python). B-trees fill pages to ~75-90 %: count 1.25x. Rough, but ranks the tables right."""
    if not rows:
        return 0
    try:
        cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]
        recs = conn.execute(f'SELECT * FROM "{table}" ORDER BY rowid DESC LIMIT {sample}').fetchall()
    except sqlite3.Error:
        return 0
    if not recs:
        return 0
    lens = [[_val_len(v) for v in r] for r in recs]
    avg = {c: sum(x[i] for x in lens) / len(lens) for i, c in enumerate(cols)}
    per_row = sum(avg.values()) + 6
    for (idx,) in conn.execute(f'SELECT name FROM pragma_index_list("{table}")').fetchall():
        per_row += sum(avg.get(r[2], 5) for r in conn.execute(f'PRAGMA index_info("{idx}")')) + 9
    return int(rows * per_row * 1.25)


def table_sizes(conn, recent_s: int = 86400) -> dict:
    """{table: {"rows", "bytes" (table + its indexes), "recent" (rows with a time column in the last recent_s
    before that table's newest row, None without one), "est" (True: bytes estimated from a sample, no dbstat)}}.
    Read-only; with dbstat it reads every page once."""
    owner = {r[0]: r[1] for r in conn.execute("SELECT name, tbl_name FROM sqlite_master WHERE type IN ('table','index')")}
    est = False
    try:
        pages = conn.execute("SELECT name, SUM(pgsize) FROM dbstat GROUP BY name").fetchall()
    except sqlite3.Error:
        pages, est = [(n, 0) for n, t in owner.items() if n == t], True
    out = {}
    for name, size in pages:
        t = owner.get(name, name)
        out.setdefault(t, {"rows": 0, "bytes": 0, "recent": None, "est": est})["bytes"] += size or 0
    for t in list(out):
        if t.startswith("sqlite_") or t not in owner:
            continue
        try:
            out[t]["rows"] = conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            if est:
                out[t]["bytes"] = _estimate_bytes(conn, t, out[t]["rows"])
            tc = _time_col(conn, t)
            if tc:
                mx = conn.execute(f'SELECT MAX({tc}) FROM "{t}"').fetchone()[0]
                if mx:
                    out[t]["recent"] = conn.execute(f'SELECT COUNT(*) FROM "{t}" WHERE {tc} > ?',
                                                    (mx - recent_s,)).fetchone()[0]
        except sqlite3.Error:
            pass
    return out


def file_stats(conn) -> dict:
    pc, ps, fl = (conn.execute(f"PRAGMA {p}").fetchone()[0] for p in ("page_count", "page_size", "freelist_count"))
    av = conn.execute("PRAGMA auto_vacuum").fetchone()[0]
    return {"bytes": pc * ps, "free_bytes": fl * ps, "auto_vacuum": {0: "none", 1: "full", 2: "incremental"}.get(av, av)}


def log_sizes(conn, path: Path, now: float | None = None) -> tuple:
    """Record the sizes now (hourly entries for 48 h, then the last one per day for SIZE_LOG_DAYS). Returns
    (sizes, file stats, growth), growth = {table: (bytes per day, rows per day)} over up to the last 7 days."""
    now = now or time.time()
    try:
        log = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        log = []
    sizes, fstats = table_sizes(conn), file_stats(conn)
    entry = {"day": time.strftime("%Y-%m-%d %H", time.localtime(now)), "ts": int(now), "file": fstats,
             "tables": {t: [v["rows"], v["bytes"], v.get("recent")] for t, v in sizes.items()}}
    kept, days = [], set()
    for e in reversed([e for e in log if e.get("day") != entry["day"]] + [entry]):   # newest first
        age = now - e.get("ts", 0)
        if age < 48 * 3600:
            kept.append(e)
        elif age < SIZE_LOG_DAYS * 86400 and e["day"][:10] not in days:
            days.add(e["day"][:10])
            kept.append(e)
    log = sorted(kept, key=lambda e: e["ts"])
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(log, separators=(",", ":")))
    tmp.replace(path)
    return sizes, fstats, growth(log)


def growth(log: list, window_days: float = 7) -> dict:
    """{table: (bytes per day, rows per day)} between the oldest entry within window_days and the newest."""
    if len(log) < 2:
        return {}
    new = log[-1]
    # an entry without byte sizes (before the estimate existed) is no base: the whole table would count as growth
    base = next((e for e in log if new["ts"] - e["ts"] <= window_days * 86400 + 3600 and e is not new
                 and sum(v[1] for v in e["tables"].values()) > 0), None)
    if base is None:
        return {}
    days = max((new["ts"] - base["ts"]) / 86400, 1e-9)
    out = {}
    for t, (rows, size, *_) in new["tables"].items():
        r0, b0, *_ = base["tables"].get(t, (0, 0))
        out[t] = ((size - b0) / days, (rows - r0) / days)
    return out


def _n(x) -> str:
    return f"{x:,.0f}".replace(",", ".")


def _mb(b) -> str:
    return f"{b / 1e6:.1f}".replace(".", ",") + " MB"


def size_report(sizes: dict, fstats: dict | None = None, grow: dict | None = None, top: int = 100) -> str:
    grow = grow or {}
    lines = []
    if fstats:
        lines.append(f"Datei {_mb(fstats['bytes'])}, davon frei (wird wiederverwendet) {_mb(fstats['free_bytes'])}, "
                     f"auto_vacuum {fstats['auto_vacuum']}")
    for t, v in sorted(sizes.items(), key=lambda kv: -kv[1]["bytes"])[:top]:
        g = grow.get(t)
        lines.append(f"{t}: {'~' if v.get('est') else ''}{_mb(v['bytes'])}, {_n(v['rows'])} Zeilen"
                     + (f", {_n(v['recent'])} in 24 h" if v.get("recent") is not None else "")
                     + (f", +{_mb(g[0])}/Tag" if g and g[0] > 0 else ""))
    return "\n".join(lines)


# token_prices rows a thinning keeps per coin and hour: highest and lowest price, lowest liquidity, newest row.
# Peaks (wallet scoring's "2x after the buy", signal outcomes) and liquidity drops (safety labels) survive.
PRICE_KEEP_SQL = """SELECT rowid FROM (SELECT rowid,
    ROW_NUMBER() OVER (PARTITION BY mint, ts / 3600 ORDER BY price_usd DESC) AS hi,
    ROW_NUMBER() OVER (PARTITION BY mint, ts / 3600 ORDER BY price_usd ASC) AS lo,
    ROW_NUMBER() OVER (PARTITION BY mint, ts / 3600 ORDER BY liquidity_usd ASC) AS lq,
    ROW_NUMBER() OVER (PARTITION BY mint, ts / 3600 ORDER BY ts DESC) AS la
    FROM token_prices {where}) WHERE hi = 1 OR lo = 1 OR lq = 1 OR la = 1"""


def trades_profile(conn) -> dict:
    """What the trades table is made of, for the retention decision (read-only, on the backup's copy):
    rows per source, rows of one-off wallets (one coin ever and only seen in a pool feed, never backfilled, so never scored: tiers need 2+ coins), and rows on
    coins where every wallet is one-off (no scored wallet ever touched them, so no training event or influence)."""
    out = {"rows": conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]}
    out["by_source"] = dict(conn.execute("SELECT COALESCE(source,''), COUNT(*) FROM trades GROUP BY 1").fetchall())
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS _ret_oneoff(wallet TEXT PRIMARY KEY)")
    conn.execute("DELETE FROM _ret_oneoff")
    conn.execute("""INSERT INTO _ret_oneoff SELECT wallet FROM trades GROUP BY wallet
                    HAVING COUNT(DISTINCT mint) = 1
                       AND SUM(COALESCE(source, '') NOT IN ('geckoterminal', 'rpc_early', 'test_pool')) = 0""")
    out["oneoff_wallets"] = conn.execute("SELECT COUNT(*) FROM _ret_oneoff").fetchone()[0]
    out["oneoff_rows"] = conn.execute(
        "SELECT COUNT(*) FROM trades WHERE wallet IN (SELECT wallet FROM _ret_oneoff)").fetchone()[0]
    out["oneoff_coin_rows"] = conn.execute(
        """SELECT COUNT(*) FROM trades WHERE mint NOT IN
               (SELECT DISTINCT mint FROM trades WHERE wallet NOT IN (SELECT wallet FROM _ret_oneoff))""").fetchone()[0]
    out["wallets"] = conn.execute("SELECT COUNT(DISTINCT wallet) FROM trades").fetchone()[0]
    out["mints"] = conn.execute("SELECT COUNT(DISTINCT mint) FROM trades").fetchone()[0]
    conn.execute("DROP TABLE _ret_oneoff")
    try:
        out["prices_rows"] = conn.execute("SELECT COUNT(*) FROM token_prices").fetchone()[0]
        out["prices_thin_rows"] = conn.execute(f"SELECT COUNT(*) FROM ({PRICE_KEEP_SQL.format(where='')})").fetchone()[0]
    except sqlite3.Error:
        pass
    return out


def profile_report(p: dict) -> str:
    pct = lambda n: f"{100 * n / p['rows']:.0f} %" if p["rows"] else "-"  # noqa: E731
    return (f"trades: {_n(p['rows'])} Zeilen, {_n(p['wallets'])} Wallets, {_n(p['mints'])} Coins; Quellen "
            + ", ".join(f"{k or '?'} {pct(v)}" for k, v in sorted(p["by_source"].items(), key=lambda kv: -kv[1]))
            + f"\nEinmal-Wallets (nur 1 Coin): {_n(p['oneoff_wallets'])} mit {pct(p['oneoff_rows'])} der Zeilen; "
            f"Coins nur mit Einmal-Wallets: {pct(p['oneoff_coin_rows'])} der Zeilen"
            + (f"\ntoken_prices: {_n(p['prices_rows'])} Zeilen, nach Ausdünnen auf Stundenextreme "
               f"{_n(p['prices_thin_rows'])}" if "prices_rows" in p else ""))


# ---------------------------------------------------------------- cleanup (deleting needs RETENTION_DELETE=1)
TRADES_HOURLY_SCHEMA = """CREATE TABLE IF NOT EXISTS trades_hourly (
    mint TEXT, hour INTEGER, side TEXT, n INTEGER, usd REAL, sol REAL, price_lo REAL, price_hi REAL,
    PRIMARY KEY (mint, hour, side))"""
COMPACT_FLAG = "COMPACT_DB"   # in data/: the launcher converts the file to incremental auto_vacuum on its next start
POOL_FEEDS = "('geckoterminal', 'rpc_early', 'test_pool')"


def _mark_oneoff(conn):
    """Temp tables: _ret_oneoff = one-off wallets (one coin ever, only seen in a pool feed), _ret_keep = coins
    any other wallet traded."""
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS _ret_oneoff(wallet TEXT PRIMARY KEY)")
    conn.execute("DELETE FROM _ret_oneoff")
    conn.execute(f"""INSERT INTO _ret_oneoff SELECT wallet FROM trades GROUP BY wallet
                     HAVING COUNT(DISTINCT mint) = 1 AND SUM(COALESCE(source, '') NOT IN {POOL_FEEDS}) = 0""")
    # coins any other wallet traded: their whole tape stays (training's crowd, structure and influence features)
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS _ret_keep(mint TEXT PRIMARY KEY)")
    conn.execute("DELETE FROM _ret_keep")
    conn.execute("INSERT INTO _ret_keep SELECT DISTINCT mint FROM trades WHERE wallet NOT IN (SELECT wallet FROM _ret_oneoff)")


def _batches(conn, select_sql, args, apply, batch, pause_s, deadline):
    """Run `apply(rowids)` in its own short write transaction per batch until select_sql finds nothing or the
    deadline passes. Every batch commits, so the bot's writers wait at most one batch."""
    done = 0
    while time.time() < deadline:
        ids = [r[0] for r in conn.execute(select_sql + f" LIMIT {int(batch)}", args)]
        if not ids:
            break
        conn.execute("BEGIN IMMEDIATE")
        try:
            apply(ids)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        done += len(ids)
        time.sleep(pause_s)
    return done


def cleanup(conn, now=None, delete=False, trades_days=30, prices_days=7, batch=2000, pause_s=0.2,
            budget_s=900, vacuum_pages=2000) -> dict:
    """Nightly retention. conn: autocommit (isolation_level=None) on the live file.

    trades older than trades_days of coins that only one-off wallets ever traded (one coin ever, only seen in
    the pool feed) are summed into trades_hourly per coin, hour and side first, then deleted, together with
    those wallets' rows in `wallets`. research/data.load trains on the whole tape of every coin a scored or
    multi-coin wallet touched (crowd, top10/bundle/early_sell, influence), so those coins keep every trade. token_prices older than prices_days keep the hour's highest and lowest
    price, lowest liquidity and newest row. With delete=False only the counts are returned."""
    now = int(now or time.time())
    deadline = time.time() + budget_s
    out = {"delete": bool(delete)}
    _mark_oneoff(conn)
    t_cut, p_cut = now - trades_days * 86400, now - prices_days * 86400
    t_sel = ("SELECT rowid FROM trades WHERE ts < ? AND wallet IN (SELECT wallet FROM _ret_oneoff) "
             "AND mint NOT IN (SELECT mint FROM _ret_keep)")
    w_sel = """SELECT rowid FROM wallets WHERE address IN (SELECT wallet FROM _ret_oneoff)
               AND COALESCE(manual, 0) = 0 AND tier IS NULL AND COALESCE(is_smart, 0) = 0
               AND NOT EXISTS (SELECT 1 FROM trades t WHERE t.wallet = wallets.address)"""
    # prices of coins a scored wallet traded stay whole too: they are the outcome labels (PriceIndex.max_between)
    p_sel = (f"SELECT rowid FROM token_prices WHERE ts < ? AND mint NOT IN (SELECT mint FROM _ret_keep) "
             f"AND rowid NOT IN ({PRICE_KEEP_SQL.format(where='WHERE ts < ?')})")
    if not delete:
        out["trades"] = conn.execute(t_sel.replace("rowid", "COUNT(*)", 1), (t_cut,)).fetchone()[0] if trades_days > 0 else 0
        out["prices"] = conn.execute(p_sel.replace("rowid", "COUNT(*)", 1), (p_cut, p_cut)).fetchone()[0]
        return out
    conn.execute(TRADES_HOURLY_SCHEMA)

    def drop_trades(ids):
        q = ",".join("?" * len(ids))
        conn.execute(f"""INSERT INTO trades_hourly(mint, hour, side, n, usd, sol, price_lo, price_hi)
            SELECT mint, ts / 3600 * 3600, side, COUNT(*), SUM(usd_amount), SUM(sol_amount),
                   MIN(NULLIF(price_usd, 0)), MAX(price_usd) FROM trades WHERE rowid IN ({q}) GROUP BY 1, 2, 3
            ON CONFLICT(mint, hour, side) DO UPDATE SET n = n + excluded.n, usd = usd + excluded.usd,
                sol = sol + excluded.sol, price_lo = MIN(COALESCE(price_lo, excluded.price_lo), COALESCE(excluded.price_lo, price_lo)),
                price_hi = MAX(COALESCE(price_hi, excluded.price_hi), COALESCE(excluded.price_hi, price_hi))""", ids)
        conn.execute(f"DELETE FROM trades WHERE rowid IN ({q})", ids)

    def drop(table):
        return lambda ids: conn.execute(f"DELETE FROM {table} WHERE rowid IN ({','.join('?' * len(ids))})", ids)

    if trades_days > 0:   # 0 = keep every trade, thin only the prices
        out["trades"] = _batches(conn, t_sel, (t_cut,), drop_trades, batch, pause_s, deadline)
        out["wallets"] = _batches(conn, w_sel, (), drop("wallets"), batch, pause_s, deadline)
    # prices hour by hour from the oldest one not yet thinned, so each pass reads one hour's rows
    done_until = int((conn.execute("SELECT value FROM retention_state WHERE key='prices_done_until'").fetchone()
                      or [0])[0]) if _has(conn, "retention_state") else 0
    first = conn.execute("SELECT MIN(ts) FROM token_prices WHERE ts >= ?", (done_until,)).fetchone()[0]
    n_p, hour = 0, (first // 3600 * 3600) if first else p_cut
    while hour + 3600 <= p_cut and time.time() < deadline:
        sel = (f"SELECT rowid FROM token_prices WHERE ts >= ? AND ts < ? AND mint NOT IN (SELECT mint FROM _ret_keep) "
               f"AND rowid NOT IN "
               f"({PRICE_KEEP_SQL.format(where='WHERE ts >= ? AND ts < ?')})")
        n_p += _batches(conn, sel, (hour, hour + 3600) * 2, drop("token_prices"), 10 ** 6, 0, deadline)
        hour += 3600
        conn.execute("CREATE TABLE IF NOT EXISTS retention_state (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT OR REPLACE INTO retention_state(key, value) VALUES ('prices_done_until', ?)", (str(hour),))
    out["prices"] = n_p
    out.update(reclaim(conn, deadline, vacuum_pages, pause_s))
    return out


def _has(conn, table) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone() is not None


def reclaim(conn, deadline, pages=2000, pause_s=0.2) -> dict:
    """Give free pages back to the file system in small steps (incremental auto_vacuum only; the launcher converts
    the file once, see COMPACT_FLAG). Each step is one short write; the bot keeps running."""
    fs = file_stats(conn)
    freed = 0
    if fs["auto_vacuum"] == "incremental":
        while time.time() < deadline:
            left = conn.execute("PRAGMA freelist_count").fetchone()[0]
            if not left:
                break
            conn.execute(f"PRAGMA incremental_vacuum({min(pages, left)})")
            freed += min(pages, left)
            time.sleep(pause_s)
    return {"freed_mb": round(freed * conn.execute("PRAGMA page_size").fetchone()[0] / 1e6, 1),
            "auto_vacuum": fs["auto_vacuum"], "free_mb_before": round(fs["free_bytes"] / 1e6, 1)}


def compact(path, min_free_factor=2.2, timeout_s=15) -> str:
    """One-time switch to incremental auto_vacuum: PRAGMA + VACUUM rewrites the whole file, so it runs only while
    no service is up (the launcher calls it before starting them) and with room for the copy."""
    import shutil
    p = Path(path)
    size = p.stat().st_size
    if shutil.disk_usage(p.parent).free < size * min_free_factor:
        return f"übersprungen: zu wenig freier Platz für {size / 1e6:.0f} MB"
    c = sqlite3.connect(str(p), timeout=timeout_s, isolation_level=None)
    try:
        t0 = time.time()
        c.execute("PRAGMA auto_vacuum=INCREMENTAL")
        c.execute("VACUUM")
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return (f"verdichtet: {size / 1e6:.0f} -> {p.stat().st_size / 1e6:.0f} MB in {time.time() - t0:.0f} s, "
                f"auto_vacuum {file_stats(c)['auto_vacuum']}")
    finally:
        c.close()
