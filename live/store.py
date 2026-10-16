"""Live tables in the shared database (same file as ct_positions, so live and paper book side by side).

live_orders  every live or dry-run swap attempt: quote, limits, simulation, signature, landed amounts, result
live_state   key/value: kill switch, data health, notes (never keys)
"""
from __future__ import annotations

import json
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS live_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, dry INTEGER, side TEXT, mint TEXT,
    in_raw INTEGER, quote_out_raw INTEGER, min_out_raw INTEGER, price_impact REAL, slippage_bps INTEGER,
    priority_lamports INTEGER, attempt INTEGER DEFAULT 1, status TEXT, sig TEXT, last_valid_height INTEGER,
    sim_err TEXT, sim_units INTEGER, out_raw INTEGER, sol_delta_lamports INTEGER, fee_lamports INTEGER,
    realized_slippage REAL, error TEXT, done_ts INTEGER, data TEXT
);
CREATE INDEX IF NOT EXISTS idx_live_orders_ts ON live_orders(ts);
CREATE TABLE IF NOT EXISTS live_state (key TEXT PRIMARY KEY, value TEXT, updated_at INTEGER);
"""

# status values
OPEN_STATES = ("sending", "unknown")  # an order whose fate is not known yet blocks new buys
FAIL_STATES = ("failed", "expired", "sim_failed", "rejected")


def init(conn) -> None:
    conn.executescript(SCHEMA)


def get(conn, key: str, default: str = "") -> str:
    r = conn.execute("SELECT value FROM live_state WHERE key=?", (key,)).fetchone()
    return r[0] if r else default


def put(conn, key: str, value) -> None:
    conn.execute("INSERT OR REPLACE INTO live_state VALUES (?,?,?)", (key, str(value), int(time.time())))
    conn.commit()


def killed(conn) -> str:
    """Reason text when the live kill switch is on, else ''."""
    return get(conn, "kill_reason") if get(conn, "kill") == "1" else ""


def kill(conn, reason: str) -> None:
    put(conn, "kill", "1")
    put(conn, "kill_reason", reason[:200])


def unkill(conn) -> None:
    put(conn, "kill", "0")
    put(conn, "kill_reason", "")
    put(conn, "fails_reset_at", int(time.time()))


def new_order(conn, *, dry: bool, side: str, mint: str, in_raw: int, attempt: int = 1, **kw) -> int:
    cur = conn.execute("INSERT INTO live_orders(ts, dry, side, mint, in_raw, attempt, status) VALUES (?,?,?,?,?,?,?)",
                       (int(time.time()), int(dry), side, mint, int(in_raw), attempt, "new"))
    conn.commit()
    if kw:
        update(conn, cur.lastrowid, **kw)
    return cur.lastrowid


def update(conn, oid: int, **kw) -> None:
    if "data" in kw and not isinstance(kw["data"], str):
        kw["data"] = json.dumps(kw["data"], ensure_ascii=False)
    if kw.get("status") and kw["status"] not in ("new", "sending"):
        kw.setdefault("done_ts", int(time.time()))
    cols = ", ".join(f"{k}=?" for k in kw)
    conn.execute(f"UPDATE live_orders SET {cols} WHERE id=?", (*kw.values(), oid))
    conn.commit()


def consecutive_fails(conn) -> int:
    """Failed real orders in a row (newest first) since the last reset; a landed order ends the streak."""
    since = int(get(conn, "fails_reset_at") or 0)
    n = 0
    for (st,) in conn.execute("SELECT status FROM live_orders WHERE dry=0 AND ts>=? AND status!='new' "
                              "ORDER BY id DESC LIMIT 20", (since,)):
        if st in FAIL_STATES:
            n += 1
        elif st == "landed":
            break
    return n


def pending(conn) -> list:
    return conn.execute("SELECT * FROM live_orders WHERE dry=0 AND status IN ('sending','unknown') ORDER BY id"
                        ).fetchall()


def recent(conn, limit: int = 50) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM live_orders ORDER BY id DESC LIMIT ?", (limit,))]


def event_safe(conn, text: str) -> None:
    """Into the copy trader's event log (ct_events) when it exists; never raises."""
    try:
        from copytrade import store as ct_store  # type: ignore

        ct_store.event(conn, "live", text)
    except Exception:
        pass
