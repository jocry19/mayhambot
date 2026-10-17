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
    base = next((e for e in log if new["ts"] - e["ts"] <= window_days * 86400 + 3600 and e is not new), None)
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
