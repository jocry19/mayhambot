"""brain: one learning decider for every opportunity (whale buy, smart-money signal, sniper launch).

Read-only interface for web/, cli/ and Telegram (see brain/README.md). Nothing here trades; the service does.
"""
import json

from brain import store as _store
from brain.learner import honest  # noqa: F401
from brain.plans import PLANS  # noqa: F401


def status(conn):
    """{balance_sol, equity_sol, open, closed, pnl_sol, win_rate, honest: {...}, model: {...}, started_at}"""
    _store.init(conn)
    from brain import config

    s = config.load()
    bal = _store.balance(conn, s.start_balance_sol)
    eq = conn.execute("SELECT equity_sol FROM brain_equity ORDER BY ts DESC LIMIT 1").fetchone()
    closed = conn.execute("SELECT COUNT(*), COALESCE(SUM(pnl_sol),0), SUM(pnl_pct>0) FROM brain_positions "
                          "WHERE status='closed'").fetchone()
    n_open = conn.execute("SELECT COUNT(*) FROM brain_positions WHERE status='open'").fetchone()[0]
    n_opps = conn.execute("SELECT COUNT(*), SUM(decision='enter') FROM brain_opps").fetchone()
    return {"balance_sol": bal, "equity_sol": eq[0] if eq else bal, "start_sol": s.start_balance_sol,
            "open": n_open, "closed": closed[0], "pnl_sol": closed[1],
            "win_rate": (closed[2] or 0) / closed[0] if closed[0] else None,
            "opportunities": n_opps[0], "entered": n_opps[1] or 0,
            "honest": honest(conn), "prior_check": _store.get(conn, "prior_check", {}),
            "prior_rows": _store.get(conn, "prior_rows", 0), "started_at": _store.get(conn, "started_at"),
            "model_saved_at": _store.get(conn, "model_saved_at")}


def decisions(conn, limit=50, kind=None, entered_only=False):
    """Newest decisions with their note (2-4 lines), expectation and, once known, the replayed outcome."""
    _store.init(conn)
    q = "SELECT * FROM brain_opps WHERE 1=1"
    args = []
    if kind:
        q += " AND kind=?"
        args.append(kind)
    if entered_only:
        q += " AND decision='enter'"
    rows = conn.execute(q + " ORDER BY ts DESC LIMIT ?", (*args, int(limit))).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        for k in ("features", "raw", "gains"):
            try:
                d[k] = json.loads(d[k]) if d.get(k) else None
            except ValueError:
                pass
        out.append(d)
    return out


def positions(conn, status=None, limit=200):
    _store.init(conn)
    q, args = "SELECT * FROM brain_positions", []
    if status:
        q += " WHERE status=?"
        args.append(status)
    return [dict(r) for r in conn.execute(q + " ORDER BY opened_at DESC LIMIT ?", (*args, int(limit))).fetchall()]


def diary(conn, limit=14):
    """Diary entries, newest first: [{day, ts, text, data}]."""
    _store.init(conn)
    out = []
    for r in conn.execute("SELECT day, ts, text, data FROM brain_journal ORDER BY day DESC LIMIT ?", (int(limit),)):
        out.append({"day": r[0], "ts": r[1], "text": r[2], "data": json.loads(r[3] or "{}")})
    return out


def equity(conn, since=0):
    _store.init(conn)
    return [dict(r) for r in conn.execute("SELECT * FROM brain_equity WHERE ts>=? ORDER BY ts", (int(since),))]
