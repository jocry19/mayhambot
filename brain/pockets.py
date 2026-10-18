"""Pockets: patterns that earned a real stake.

Every opportunity of every source is booked with its counterfactual result after costs (brain_opps, taken or not:
the shadow book). A pocket is one source alone or one source with one feature value ("market" + "dex=pumpswap"). It is
released only when its record is beyond chance: at least MIN_N cases in the window, and the 95 % lower bound of the
mean is above zero in BOTH halves of that window (older and newer), so a pocket that paid only once does not count.
It falls back the moment its last LAST_N cases no longer pay. Plan "Hundertmal besser" (18.10.): with a negative
expected value bigger stakes only lose faster, so the challenge stakes run on released pockets only."""
from __future__ import annotations

import json
import math

from . import store

WINDOW_S = 7 * 86400
MIN_N = 100
LAST_N = 50
Z = 1.96
EVERY_S = 600
SKIP = ("kind", "strategy", "explore", "gap", "safety")


def lower(xs: list[float]) -> float | None:
    """95 % lower bound of the mean."""
    n = len(xs)
    if n < 2:
        return None
    m = sum(xs) / n
    var = sum((x - m) ** 2 for x in xs) / (n - 1)
    return m - Z * math.sqrt(var / n)


def keys(kind: str, feats: dict) -> list[str]:
    out = [kind]
    for k, v in sorted(feats.items()):
        if k in SKIP or v is None or isinstance(v, (bool, dict, list)):
            continue
        out.append(f"{kind}|{k}={v}")
    return out


def scan(conn, now: float) -> dict:
    """{pocket: {n, mean, lb, lb_old, lb_new, last}} for every pocket with MIN_N cases; released ones flagged."""
    rows = conn.execute("SELECT kind, features, realized_plan FROM brain_opps WHERE realized_plan IS NOT NULL "
                        "AND evaluated_at>=? ORDER BY ts", (int(now - WINDOW_S),)).fetchall()
    by: dict[str, list[float]] = {}
    for kind, f, r in rows:
        try:
            feats = json.loads(f or "{}")
        except ValueError:
            feats = {}
        for k in keys(kind, feats):
            by.setdefault(k, []).append(float(r))
    out = {}
    for k, xs in by.items():
        n = len(xs)
        if n < MIN_N:
            continue
        h = n // 2
        lb, lo, ln = lower(xs), lower(xs[:h]), lower(xs[h:])
        last = sum(xs[-LAST_N:]) / min(n, LAST_N)
        live = lo is not None and ln is not None and lo > 0 and ln > 0 and last > 0
        out[k] = {"n": n, "mean": round(sum(xs) / n, 4), "lb": round(lb, 4), "lb_old": round(lo, 4),
                  "lb_new": round(ln, 4), "last": round(last, 4), "live": live}
    return out


class Book:
    """The released pockets, rescanned every EVERY_S and kept in brain_state for the journal and the dashboard."""

    def __init__(self, conn, clock):
        self.conn, self.clock = conn, clock
        self._at = -1e18
        self.live: set[str] = set()

    def refresh(self) -> set[str]:
        now = self.clock()
        if now - self._at < EVERY_S:
            return self.live
        self._at = now
        try:
            res = scan(self.conn, now)
        except Exception:
            return self.live
        self.live = {k for k, v in res.items() if v["live"]}
        top = sorted(res.items(), key=lambda kv: -kv[1]["lb"])[:20]
        store.put(self.conn, "pockets", {"at": int(now), "live": sorted(self.live), "top": dict(top),
                                         "checked": len(res)})
        return self.live

    def match(self, kind: str, feats: dict) -> str | None:
        live = self.refresh()
        return next((k for k in keys(kind, feats) if k in live), None)
