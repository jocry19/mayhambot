"""Exit rules replayed on the real calls: which way of selling turns the highs after a call into money.

Every call's price path (DexScreener snapshots + pool trade prints, single bad prints filtered as for the outcomes)
from ENTRY_DELAY_S after the call for HOURS; entry at the worse of the call price and the price then. Each rule walks
the path point by point and books the weighted return of its (partial) sells, minus COST per round trip. A coin
whose prices stop and that died (outcome_flag 'dead') is sold at DEAD_RET at the end. COST only where a call has no
own round trip (signals.cost from lab.costs by venue and depth).

    python -m signals.exits --db <copy of the DB> --days 7
"""
import argparse
import json
import sqlite3
import time

from signals.backtest import DEAD_RET, confirmed, price_near

ENTRY_DELAY_S = 60
HOURS = 4
COST = 0.06
CAP = 3.0  # a single +3000 % print must not carry a mean


def rule(tp=None, sl=None, trail=None, hold_min=HOURS * 60, partial=None, entry_dip=None, entry_wait_min=30):
    """tp / sl as returns; trail=(activate, drawdown); partial=[(ret, share), ...] sold on the way up, then the rest
    runs with a stop at entry (breakeven) and trail if given. entry_dip: no buy at the alert but a limit order
    entry_dip below it, filled if the price gets there within entry_wait_min (else no trade, no cost); tp / sl /
    trail then count from the fill (18.10.: calls run 1.5-10x but the alert price is mostly a local high)."""
    return dict(tp=tp, sl=sl, trail=trail, hold=hold_min * 60, partial=partial or [], entry_dip=entry_dip,
                entry_wait=entry_wait_min * 60)


RULES = {
    "paper now (tp+50 sl-20 trail 20/12)": rule(tp=0.5, sl=-0.2, trail=(0.2, 0.12)),
    "tp+50 sl-35, no trail": rule(tp=0.5, sl=-0.35),
    "tp+50 sl-50, no trail": rule(tp=0.5, sl=-0.5),
    "half at +50, rest stop at entry, trail 25 % from high": rule(sl=-0.35, partial=[(0.5, 0.5)], trail=(0.5, 0.25)),
    "1/3 at +50, 1/3 at +100, rest trail 30 %": rule(sl=-0.35, partial=[(0.5, 1 / 3), (1.0, 1 / 3)], trail=(1.0, 0.3)),
    "half at +50, half at +150": rule(sl=-0.35, partial=[(0.5, 0.5), (1.5, 0.5)]),
    "tp+100 sl-35": rule(tp=1.0, sl=-0.35),
    "hold 1 h (no exits)": rule(hold_min=60),
    # winner study 17.10.: no fixed target under +100 %, trail 30 % once +100 %, stop -30 % (band class goal)
    "sl-30, trail 30 % from +100": rule(sl=-0.3, trail=(1.0, 0.3)),
    # limit entries below the alert price (no fill in 30 min = no trade); -15 / -30 and the x2 target as in the
    # Prüfstand's signal family (18.10.: calls held 4 h lose, limit15 with x2 target was positive on few cases)
    "limit -15 % (30 min), then sl-30, trail 30 % from +100": rule(sl=-0.3, trail=(1.0, 0.3), entry_dip=0.15),
    "limit -30 % (30 min), then sl-30, trail 30 % from +100": rule(sl=-0.3, trail=(1.0, 0.3), entry_dip=0.3),
    "limit -15 % (30 min), then tp+50 sl-35": rule(tp=0.5, sl=-0.35, entry_dip=0.15),
    "limit -15 % (30 min), then tp+100 sl-35": rule(tp=1.0, sl=-0.35, entry_dip=0.15),
    "limit -30 % (30 min), then tp+100 sl-35": rule(tp=1.0, sl=-0.35, entry_dip=0.3),
    "limit -30 % (30 min), then half at +50, rest trail 25 %": rule(sl=-0.35, partial=[(0.5, 0.5)], trail=(0.5, 0.25),
                                                                       entry_dip=0.3),
}


def simulate(path, pe, r, dead=False):
    """path: [(seconds since entry, price)]; returns the realized return before costs."""
    left, booked, high, breakeven = 1.0, 0.0, pe, False
    parts = list(r["partial"])
    for t, p in path:
        if t > r["hold"]:
            break
        ret = p / pe - 1
        high = max(high, p)
        while parts and ret >= parts[0][0] and left > 1e-9:
            lvl, share = parts.pop(0)
            sell = min(share, left)
            booked += sell * lvl  # sold at the level (limit order), not at the overshoot
            left -= sell
            breakeven = True
        if left <= 1e-9:
            return booked
        if r["tp"] is not None and ret >= r["tp"]:
            return booked + left * r["tp"]
        stop = 0.0 if breakeven else r["sl"]
        if stop is not None and ret <= stop:
            return booked + left * stop
        if r["trail"] and (high / pe - 1) >= r["trail"][0] and p <= high * (1 - r["trail"][1]):
            return booked + left * ret
    if dead:
        return booked + left * DEAD_RET
    last = [p for t, p in path if t <= r["hold"]]
    return booked + left * ((last[-1] / pe - 1) if last else 0.0)


def trade(path, pe, r, dead=False):
    """Return of one call under rule r, None when its limit entry never filled (no trade, no cost)."""
    if not r.get("entry_dip"):
        return simulate(path, pe, r, dead)
    lim = pe * (1 - r["entry_dip"])
    for i, (t, p) in enumerate(path):
        if t > r["entry_wait"]:
            return None
        if p <= lim:
            return simulate([(t2 - t, p2) for t2, p2 in path[i + 1:]], lim, r, dead)
    return None if not dead else simulate([], lim, r, dead)  # died before any later print: the order filled into it


def _net(p, r):
    x = trade(p["path"], p["pe"], r, p["dead"])
    return None if x is None else min(x, CAP) - p.get("cost", COST)


def paths(conn, days=7, now=None, kind=None, table="signals", reason_like=None):
    """table 'signal_shadow' replays shadow rows (no symbol there), optionally only reasons LIKE reason_like."""
    now = now or int(time.time())
    sym = "symbol" if table == "signals" else "NULL"
    q = (f"SELECT id, ts, mint, {sym}, price_usd, outcome_flag, cost FROM {table} WHERE ts>=? AND ts<=? AND price_usd>0"
         + (" AND COALESCE(kind,'v1')=?" if kind else "") + (" AND reason LIKE ?" if reason_like else ""))
    args = (now - days * 86400, now - HOURS * 3600) + ((kind,) if kind else ()) + ((reason_like,) if reason_like else ())
    out = []
    for s in conn.execute(q + " ORDER BY ts", args).fetchall():
        t0, p0 = s[1], s[4]
        snap = conn.execute("SELECT ts, price_usd FROM token_prices WHERE mint=? AND ts>? AND ts<=? AND price_usd>0",
                            (s[2], t0, t0 + HOURS * 3600)).fetchall()
        prints = conn.execute("SELECT ts, price_usd FROM trades WHERE mint=? AND ts>? AND ts<=? AND price_usd>0",
                              (s[2], t0, t0 + HOURS * 3600)).fetchall()
        pts = sorted([(r[0], r[1], True) for r in snap] + [(r[0], r[1], False) for r in prints])
        clean = list(zip([x[0] for x in pts], confirmed([x[1] for x in pts], [x[2] for x in pts])))
        pe = max(p0, price_near(clean, t0 + ENTRY_DELAY_S, 120) or p0)
        path = [(t - t0 - ENTRY_DELAY_S, p) for t, p in clean if t >= t0 + ENTRY_DELAY_S]
        if path or s[5] == "dead":
            out.append(dict(id=s[0], symbol=s[3], pe=pe, path=path, dead=s[5] == "dead",
                            cost=s[6] if s[6] is not None else COST))
    return out


MIN_N_DECIDE = 30
CURRENT = "paper now (tp+50 sl-20 trail 20/12)"


def _score(ps, r):
    xs = [_net(p, r) or 0.0 for p in ps]  # an unfilled limit is a trade not made: 0, not a loss
    return sum(xs) / len(xs) if xs else None


def verdict(conn, days=14, now=None, kind=None, min_n=MIN_N_DECIDE):
    """Which rule to switch to, only when it is decidable: at least min_n calls, the same rule best in the older
    and the newer half (time split), and better than the current paper exit by 5+ points in both halves."""
    ps = paths(conn, days, now, kind)
    out = dict(n=len(ps), min_n=min_n, switch_to=None)
    if len(ps) < min_n:
        out["why"] = f"zu wenige Calls ({len(ps)} < {min_n})"
        return out
    halves = (ps[:len(ps) // 2], ps[len(ps) // 2:])
    best = []
    for h in halves:
        sc = {k: _score(h, r) for k, r in RULES.items()}
        b = max(sc, key=sc.get)
        best.append((b, sc[b] - sc[CURRENT]))
    out["best_per_half"] = [(b, round(d, 3)) for b, d in best]
    if best[0][0] == best[1][0] and best[0][0] != CURRENT and min(d for _, d in best) >= 0.05:
        out["switch_to"] = best[0][0]
    else:
        out["why"] = "keine Regel in beiden Hälften klar besser als der jetzige Ausstieg"
    return out


def compare(conn, days=7, now=None, rules=None, kind=None, table="signals", reason_like=None):
    ps = paths(conn, days, now, kind, table, reason_like)
    res = {}
    for name, r in (rules or RULES).items():
        nets = [_net(p, r) for p in ps]
        xs = sorted(x for x in nets if x is not None)
        if not ps:
            continue
        res[name] = dict(n=len(ps), filled=len(xs), mean_after_cost=round(sum(xs) / len(ps), 3),
                         mean_per_trade=round(sum(xs) / len(xs), 3) if xs else None,
                         median=round(xs[len(xs) // 2], 3) if xs else None,
                         win_rate=round(sum(x > 0 for x in xs) / len(xs), 3) if xs else None,
                         sum_per_0_3_sol=round(0.3 * sum(xs), 3))
    return res


def main():
    ap = argparse.ArgumentParser(description="exit rules replayed on the real calls")
    ap.add_argument("--db", required=True, help="a copy of the DB (opened read-only)")
    ap.add_argument("--days", type=float, default=7)
    ap.add_argument("--kind", default=None)
    a = ap.parse_args()
    conn = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True, timeout=30)
    print(json.dumps(compare(conn, a.days, kind=a.kind), indent=1))


if __name__ == "__main__":
    main()
