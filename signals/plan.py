"""Trade plan for following a signal by hand: target and stop from what past calls really did after the call.

Target: the 1 h peak that TARGET_SHARE of the past calls reached (quantile of max_ret_1h), so it is a level most
calls touched, not the best case. Stop: deep enough that STOP_KEEP of the calls that did reach the target survived
their dip before the peak (dd_before_peak_1h), plus a small margin. Until MIN_N calls have results the defaults
below hold (+50 % / -30 %, the copytrade paper preset).

check_levels() runs every cycle: the first time the price crosses the target (or the stop before the target)
after a call, it stores the time and returns a follow-up alert, so someone trading by hand knows when to sell.
"""
import os
import time

DEFAULT_TARGET = float(os.getenv("SIGNAL_TARGET_DEFAULT", 0.5))
DEFAULT_STOP = float(os.getenv("SIGNAL_STOP_DEFAULT", -0.3))
TARGET_SHARE = float(os.getenv("SIGNAL_TARGET_SHARE", 0.6))
STOP_KEEP = 0.9
MIN_N = int(os.getenv("SIGNAL_PLAN_MIN_N", 8))
DAYS = 14
FOLLOW_HOURS = 4          # a call is watched for its target / stop this long
FOLLOW_FRESH_S = 900      # crossings older than this (e.g. after a restart) are stored without an alert


def _q(vals, q):
    vals = sorted(vals)
    return vals[min(len(vals) - 1, max(0, int(q * len(vals))))]


def levels(conn, now=None, days=DAYS, min_n=MIN_N):
    """dict(target, stop, n, n_unmeasured, hit_rate (in sample), hit_rate_oos, peak_min, source) for the next alert."""
    now = now or int(time.time())
    rows = conn.execute("SELECT max_ret_1h, dd_before_peak_1h, peak_min_1h FROM signals "
                        "WHERE ts>=? AND max_ret_1h IS NOT NULL ORDER BY ts", (now - days * 86400,)).fetchall()
    missing = conn.execute("SELECT COUNT(*) FROM signals WHERE ts>=? AND ts<=? AND max_ret_1h IS NULL",
                           (now - days * 86400, now - 7200)).fetchone()[0]
    mx = [r[0] for r in rows]
    out = dict(target=DEFAULT_TARGET, stop=DEFAULT_STOP, n=len(rows), n_unmeasured=missing, hit_rate=None,
               hit_rate_oos=None, peak_min=None, source="default")
    if len(rows) >= min_n:
        out.update(target=round(min(1.5, max(0.15, _q(mx, 1 - TARGET_SHARE))), 2), source="history")
        # honest check: a target set on the older two thirds, scored on the newest third it never saw
        cut = len(mx) * 2 // 3
        t_old = min(1.5, max(0.15, _q(mx[:cut], 1 - TARGET_SHARE)))
        out["hit_rate_oos"] = round(sum(1 for x in mx[cut:] if x >= t_old) / len(mx[cut:]), 2)
    winners = [r for r in rows if r[0] >= out["target"]]
    dips = [r[1] for r in winners if r[1] is not None]
    if len(rows) >= min_n and len(dips) >= 5:
        out["stop"] = round(min(-0.1, max(-0.5, _q(dips, 1 - STOP_KEEP) - 0.05)), 2)
    if rows:  # in sample, so ~TARGET_SHARE by construction once from history; unmeasured calls count as misses
        out["hit_rate"] = round(sum(1 for x in mx if x >= out["target"]) / (len(rows) + missing), 2)
    mins = [r[2] for r in winners if r[2] is not None]
    if mins:
        out["peak_min"] = round(_q(mins, 0.5))
    return out


def _prices(conn, mint, since, until):
    snap = conn.execute("SELECT ts, price_usd FROM token_prices WHERE mint=? AND ts>? AND ts<=? AND price_usd>0",
                        (mint, since, until)).fetchall()
    prints = conn.execute("SELECT ts, price_usd FROM trades WHERE mint=? AND ts>? AND ts<=? AND price_usd>0",
                          (mint, since, until)).fetchall()
    return sorted((r[0], r[1]) for r in list(snap) + list(prints))


def first_cross(pts, p0, target, stop):
    """('target'|'stop', ts, ret) for the first crossing in time order, else None."""
    for t, p in pts:
        r = p / p0 - 1
        if r >= target:
            return "target", t, r
        if r <= stop:
            return "stop", t, r
    return None


def check_levels(conn, now=None):
    """Store the first target / stop crossing of the calls of the last FOLLOW_HOURS; return the fresh ones as
    dicts for the follow-up alert."""
    now = now or int(time.time())
    out = []
    for s in conn.execute("SELECT id, ts, mint, symbol, price_usd, target_ret, stop_ret, call_no FROM signals "
                          "WHERE ts>? AND target_ret IS NOT NULL AND target_hit_ts IS NULL AND stop_hit_ts IS NULL "
                          "AND price_usd>0", (now - FOLLOW_HOURS * 3600,)).fetchall():
        hit = first_cross(_prices(conn, s["mint"], s["ts"], min(now, s["ts"] + FOLLOW_HOURS * 3600)),
                          s["price_usd"], s["target_ret"], s["stop_ret"] if s["stop_ret"] is not None else -1.0)
        if not hit:
            continue
        kind, t, r = hit
        conn.execute(f"UPDATE signals SET {kind}_hit_ts=? WHERE id=?", (t, s["id"]))
        if now - t <= FOLLOW_FRESH_S:
            out.append(dict(kind=kind, ts=t, ret=r, minutes=round((t - s["ts"]) / 60), symbol=s["symbol"],
                            mint=s["mint"], price=s["price_usd"] * (1 + r), target=s["target_ret"],
                            stop=s["stop_ret"], call_no=s["call_no"]))
    conn.commit()
    return out
