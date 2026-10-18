"""New-narrative signal: the leading tradable coin of a word that suddenly shows up in many launches.

market/ finds the emergent terms (market_terms, stage 'new' / 'growing' from market/dynamics.py). Here each such
term's coins are put on the signal watchlist (so prices are tracked), and per term the coin with the most hour
volume and enough liquidity is booked in the shadow book as kind 'narrative', reason 'narrative <stage> leader'
(the others 'narrative <stage>'). Measured like every call: ret after 1 h / 4 h / 24 h minus the coin's own round
trip, plus the exit / limit-entry replay. No alert and no brain opportunity until ready() says the leaders made
money after costs in both halves of the data (18.10.: user asked for a new-narrative signal; the walk-forward
found brand-new terms worse for sniper launches, so it has to prove itself first).
"""
import json
import os
import sqlite3
import time

from market import narratives

STAGES = ("new", "growing")
MIN_HEAT = float(os.getenv("SIGNAL_NARRATIVE_MIN_HEAT", 40))
MAX_TERMS = 10
MAX_PER_TERM = 5
SNAP_MAX_AGE_S = 1800
FRESH_S = 900          # a token row older than this has no price to book
ONCE_S = 24 * 3600     # one booking per coin and day: the first time it leads a fresh narrative is the event
MIN_N_READY = 30


def hot_terms(conn, now=None):
    """[{term, stage, heat, mints}] of the newest market_terms snapshot, if it is fresh."""
    now = now or int(time.time())
    try:
        ts = conn.execute("SELECT MAX(ts) FROM market_terms WHERE ts<=?", (now,)).fetchone()[0]
        if not ts or now - ts > SNAP_MAX_AGE_S:
            return []
        rows = conn.execute("SELECT term, stage, heat, mints_json FROM market_terms WHERE ts=? AND heat>=? "
                            "ORDER BY heat DESC", (ts, MIN_HEAT)).fetchall()
    except sqlite3.OperationalError:  # market/ not set up or older schema without stage
        return []
    out = []
    for term, stage, heat, mj in rows:
        if stage not in STAGES:
            continue
        try:
            mints = json.loads(mj or "[]")
        except ValueError:
            mints = []
        out.append(dict(term=term, stage=stage, heat=heat, mints=list(mints)))
    return out[:MAX_TERMS]


def term_mints(conn, t, now):
    """The term's launch mints plus traded coins carrying it (market_tokens, most hour volume first)."""
    mints = list(t["mints"])
    try:
        for (m,) in conn.execute("SELECT mint FROM market_tokens WHERE updated_at>? AND terms_json LIKE ? "
                                 "ORDER BY COALESCE(volume_h1,0) DESC LIMIT 20", (now - 3600, f'%"{t["term"]}"%')):
            if m not in mints:
                mints.append(m)
    except sqlite3.OperationalError:
        pass
    return mints[:MAX_PER_TERM + len(t["mints"])]


def watch_mints(conn, now=None):
    """Coins of the hot narratives, for the signal watchlist (token info and price snapshots)."""
    now = now or int(time.time())
    out = []
    for t in hot_terms(conn, now):
        out += [m for m in term_mints(conn, t, now) if m not in out]
    return out


def scan(conn, min_liq, now=None):
    """Book each hot narrative's coins in the shadow book; returns the number of new rows."""
    from signals import shadow
    now = now or int(time.time())
    n = 0
    for t in hot_terms(conn, now):
        ms = term_mints(conn, t, now)
        if not ms:
            continue
        q = ",".join("?" * len(ms))
        rows = conn.execute(f"SELECT mint, name, symbol, price_usd, liquidity_usd, volume_h1 FROM tokens "
                            f"WHERE mint IN ({q}) AND last_updated>? AND price_usd>0 AND liquidity_usd>=?",
                            (*ms, now - FRESH_S, min_liq)).fetchall()
        rows = [r for r in rows if t["term"] in narratives.terms_of(r[1] or "", r[2] or "")]
        rows.sort(key=lambda r: -(r[5] or 0))
        for i, r in enumerate(rows[:MAX_PER_TERM]):
            if conn.execute("SELECT 1 FROM signal_shadow WHERE mint=? AND kind='narrative' AND ts>?",
                            (r[0], now - ONCE_S)).fetchone():
                continue
            reason = f"narrative {t['stage']}" + (" leader" if i == 0 else "")
            m = dict(liquidity_usd=r[4], score=t["heat"])
            n += bool(shadow.record(conn, r[0], reason, m, r[3], now, kind="narrative", report=False))
    if n:
        conn.commit()
    return n


def ready(conn, now=None, days=14, min_n=MIN_N_READY):
    """Alert-ready only when the leaders were decidably positive after costs: at least min_n measured, and the
    best exit rule (immediate or limit entry) above zero in the older and the newer half with the same rule."""
    from signals import exits
    ps = exits.paths(conn, days, now, kind="narrative", table="signal_shadow", reason_like="% leader")
    out = dict(n=len(ps), min_n=min_n, ready=False)
    if len(ps) < min_n:
        out["why"] = f"zu wenige Fälle ({len(ps)} < {min_n})"
        return out
    best = []
    for h in (ps[:len(ps) // 2], ps[len(ps) // 2:]):
        sc = {k: exits._score(h, r) for k, r in exits.RULES.items()}
        b = max(sc, key=sc.get)
        best.append((b, round(sc[b], 3)))
    out["best_per_half"] = best
    out["ready"] = best[0][0] == best[1][0] and min(x for _, x in best) > 0
    if not out["ready"]:
        out["why"] = "in beiden Hälften nicht mit derselben Regel im Plus"
    return out
