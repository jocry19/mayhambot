"""Trade plan for following a signal by hand: target and stop from what past calls really did after the call.

Target: the 1 h peak that TARGET_SHARE of the past calls reached (quantile of max_ret_1h), so it is a level most
calls touched, not the best case. Stop: deep enough that STOP_KEEP of the calls that did reach the target survived
their dip before the peak (dd_before_peak_1h), plus a small margin. Until MIN_N calls have results the defaults
below hold (+50 % / -30 %, the copytrade paper preset).

check_levels() runs every cycle: the first time the price crosses the target, the trail or the emergency stop
after a call, it stores the time and returns a follow-up alert, so someone trading by hand knows when to sell.

19.10. (all data since 16.10.): calls held 4 h lost (-59 % net), a tight fixed stop sold the dip before the run.
So: target or trail (once +TRAIL_FROM, out TRAIL_DD below the high, as the brain's trail20), stop only as an
emergency at EMERGENCY_STOP, never tighter.
"""
import os
import time

DEFAULT_TARGET = float(os.getenv("SIGNAL_TARGET_DEFAULT", 0.5))
DEFAULT_STOP = float(os.getenv("SIGNAL_STOP_DEFAULT", -0.5))
EMERGENCY_STOP = -0.5     # the stop is never tighter than this (a dip stop sold the calls before their run)
TRAIL_FROM = 0.2
TRAIL_DD = 0.2
TARGET_SHARE = float(os.getenv("SIGNAL_TARGET_SHARE", 0.6))
STOP_KEEP = 0.9
MIN_N = int(os.getenv("SIGNAL_PLAN_MIN_N", 8))
DAYS = 14
FOLLOW_HOURS = 4          # a call is watched for its target / stop this long
FOLLOW_FRESH_S = 900      # crossings older than this (e.g. after a restart) are stored without an alert


def _q(vals, q):
    vals = sorted(vals)
    return vals[min(len(vals) - 1, max(0, int(q * len(vals))))]


def levels(conn, now=None, days=DAYS, min_n=MIN_N, kind="v1"):
    """dict(target, stop, n, n_unmeasured, hit_rate (in sample), hit_rate_oos, peak_min, source) for the next alert,
    from the past calls of the same kind (v1 burst / v2 accumulation move differently)."""
    now = now or int(time.time())
    rows = conn.execute("SELECT max_ret_1h, dd_before_peak_1h, peak_min_1h FROM signals "
                        "WHERE ts>=? AND max_ret_1h IS NOT NULL AND COALESCE(kind,'v1')=? ORDER BY ts",
                        (now - days * 86400, kind)).fetchall()
    missing = conn.execute("SELECT COUNT(*) FROM signals WHERE ts>=? AND ts<=? AND max_ret_1h IS NULL "
                           "AND COALESCE(kind,'v1')=?", (now - days * 86400, now - 7200, kind)).fetchone()[0]
    mx = [r[0] for r in rows]
    out = dict(kind=kind, target=DEFAULT_TARGET, stop=DEFAULT_STOP, n=len(rows), n_unmeasured=missing, hit_rate=None,
               hit_rate_oos=None, peak_min=None, source="default")
    if len(rows) >= min_n:
        out.update(target=round(min(1.5, max(0.15, _q(mx, 1 - TARGET_SHARE))), 2), source="history")
        # honest check: a target set on the older two thirds, scored on the newest third it never saw
        cut = len(mx) * 2 // 3
        t_old = min(1.5, max(0.15, _q(mx[:cut], 1 - TARGET_SHARE)))
        out["hit_rate_oos"] = round(sum(1 for x in mx[cut:] if x >= t_old) / len(mx[cut:]), 2)
    winners = [r for r in rows if r[0] >= out["target"]]
    dips = [r[1] for r in winners if r[1] is not None]
    if len(rows) >= min_n and len(dips) >= 5:  # from past dips, but never tighter than the emergency stop
        out["stop"] = round(min(EMERGENCY_STOP, _q(dips, 1 - STOP_KEEP) - 0.05), 2)
    out.update(trail_from=TRAIL_FROM, trail_dd=TRAIL_DD)
    if rows:  # in sample, so ~TARGET_SHARE by construction once from history; unmeasured calls count as misses
        out["hit_rate"] = round(sum(1 for x in mx if x >= out["target"]) / (len(rows) + missing), 2)
    mins = [r[2] for r in winners if r[2] is not None]
    if mins:
        out["peak_min"] = round(_q(mins, 0.5))
    return out


def _prices(conn, mint, since, until):
    """[(ts, price)] in time order with single bad trade prints replaced (signals.backtest.confirmed, as the outcome
    measurement does), so one stray print is never a target or stop alert. DexScreener snapshots always stand; a
    trade print that is the newest point has no later neighbour to confirm it yet and waits for the next cycle."""
    from signals.backtest import confirmed
    snap = conn.execute("SELECT ts, price_usd FROM token_prices WHERE mint=? AND ts>? AND ts<=? AND price_usd>0",
                        (mint, since, until)).fetchall()
    prints = conn.execute("SELECT ts, price_usd FROM trades WHERE mint=? AND ts>? AND ts<=? AND price_usd>0",
                          (mint, since, until)).fetchall()
    pts = sorted([(r[0], r[1], True) for r in snap] + [(r[0], r[1], False) for r in prints])
    if pts and not pts[-1][2]:
        pts = pts[:-1]
    clean = confirmed([p for _, p, _ in pts], [tr for _, _, tr in pts])
    return [(t, p) for (t, _, _), p in zip(pts, clean)]


def first_cross(pts, p0, target, stop, trail=(TRAIL_FROM, TRAIL_DD)):
    """('target'|'trail'|'stop', ts, ret) for the first crossing in time order, else None. trail=(from, dd):
    once the high is +from, out at dd below the high; None = no trail."""
    high = p0
    for t, p in pts:
        r = p / p0 - 1
        high = max(high, p)
        if r >= target:
            return "target", t, r
        if r <= stop:
            return "stop", t, r
        if trail and high / p0 - 1 >= trail[0] and p <= high * (1 - trail[1]):
            return "trail", t, r
    return None


def _staged(conn, mint):
    """safety's staged-pump finding (checks['staged'].data.pattern): >= $250k mcap within 30 min on a thin pool,
    25 of 34 such coins drained within 60 min after the high (safety 15784c6). An exit signal, not an entry veto."""
    try:
        from safety import engine as safety_engine
        a = safety_engine.get(conn, mint)
    except Exception:  # noqa: BLE001 - safety/ absent or not set up
        return False
    st = (getattr(a, "checks", None) or {}).get("staged") if a else None
    return bool(st and (st.get("data") or {}).get("pattern"))


def check_staged(conn, now=None):
    """Calls still followed whose coin turned out to be a staged pump: one exit alert each."""
    now = now or int(time.time())
    out = []
    for s in conn.execute("SELECT id, ts, mint, symbol, price_usd, call_no, kind FROM signals WHERE ts>? AND staged_ts IS NULL "
                          "AND target_hit_ts IS NULL AND stop_hit_ts IS NULL AND trail_hit_ts IS NULL", (now - FOLLOW_HOURS * 3600,)).fetchall():
        if not _staged(conn, s["mint"]):
            continue
        conn.execute("UPDATE signals SET staged_ts=? WHERE id=?", (now, s["id"]))
        p = conn.execute("SELECT price_usd FROM tokens WHERE mint=?", (s["mint"],)).fetchone()
        p = p[0] if p and p[0] else s["price_usd"]
        out.append(dict(kind="staged", ts=now, ret=(p / s["price_usd"] - 1) if s["price_usd"] else 0.0,
                        minutes=round((now - s["ts"]) / 60), symbol=s["symbol"], mint=s["mint"], price=p,
                        call_no=s["call_no"], signal_kind=s["kind"] or "v1"))
    conn.commit()
    return out


def check_levels(conn, now=None):
    """Store the first target / stop crossing of the calls of the last FOLLOW_HOURS; return the fresh ones as
    dicts for the follow-up alert."""
    now = now or int(time.time())
    out = []
    for s in conn.execute("SELECT id, ts, mint, symbol, price_usd, target_ret, stop_ret, call_no, kind FROM signals "
                          "WHERE ts>? AND target_ret IS NOT NULL AND target_hit_ts IS NULL AND stop_hit_ts IS NULL "
                          "AND trail_hit_ts IS NULL AND price_usd>0", (now - FOLLOW_HOURS * 3600,)).fetchall():
        hit = first_cross(_prices(conn, s["mint"], s["ts"], min(now, s["ts"] + FOLLOW_HOURS * 3600)),
                          s["price_usd"], s["target_ret"],
                          min(EMERGENCY_STOP, s["stop_ret"]) if s["stop_ret"] is not None else EMERGENCY_STOP)
        if not hit:
            continue
        kind, t, r = hit
        conn.execute(f"UPDATE signals SET {kind}_hit_ts=? WHERE id=?", (t, s["id"]))
        if now - t <= FOLLOW_FRESH_S:
            out.append(dict(kind=kind, ts=t, ret=r, minutes=round((t - s["ts"]) / 60), symbol=s["symbol"],
                            mint=s["mint"], price=s["price_usd"] * (1 + r), target=s["target_ret"],
                            stop=s["stop_ret"], call_no=s["call_no"], signal_kind=s["kind"] or "v1"))
    conn.commit()
    return out
