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


def label(key: str) -> str:
    from .features import describe

    kind, _, feat = key.partition("|")
    src = {"whale": "Wal-Kopien", "signal": "Signale", "signal_v2": "Signale v2", "launch": "Launches",
           "market": "Markt-Coins"}.get(kind, kind)
    return f"{src}, {describe(feat)}" if feat else f"{src} (alle)"


def _why(n, lo, ln, last) -> str:
    if n < MIN_N:
        return f"erst {n} von {MIN_N} Fällen"
    if lo is None or lo <= 0:
        return f"ältere Hälfte nicht sicher im Plus (Untergrenze {lo:+.1%})" if lo is not None else "ältere Hälfte zu klein"
    if ln is None or ln <= 0:
        return f"neuere Hälfte nicht sicher im Plus (Untergrenze {ln:+.1%})" if ln is not None else "neuere Hälfte zu klein"
    if last <= 0:
        return f"die letzten {LAST_N} Fälle Ø {last:+.1%}: zurückgestuft"
    return "in beiden Hälften sicher im Plus, die letzten 50 tragen"


def scan(conn, now: float) -> dict:
    """{pocket: row} for every pocket with at least MIN_N // 2 cases (so the way to a release is visible)."""
    rows = conn.execute("SELECT kind, features, realized_plan FROM brain_opps WHERE realized_plan IS NOT NULL "
                        "AND evaluated_at>=? ORDER BY ts", (int(now - WINDOW_S),)).fetchall()
    by: dict[str, list[float]] = {}
    for kind, f, r in rows:
        try:
            feats = json.loads(f or "{}")
        except ValueError:
            feats = {}
        if kind == "signal" and feats.get("sig") == "v2":
            kind = "signal_v2"
        for k in keys(kind, feats):
            by.setdefault(k, []).append(float(r))
    out = {}
    for k, xs in by.items():
        n = len(xs)
        if n < MIN_N // 2:
            continue
        h = n // 2
        a, b = xs[:h], xs[h:]
        lb, lo, ln = lower(xs), lower(a), lower(b)
        last = sum(xs[-LAST_N:]) / min(n, LAST_N)
        live = n >= MIN_N and lo is not None and ln is not None and lo > 0 and ln > 0 and last > 0
        out[k] = {"key": k, "label": label(k), "source": k.split("|")[0], "n": n, "mean": round(sum(xs) / n, 4),
                  "lower95": round(lb, 4) if lb is not None else None,
                  "n_first_half": len(a), "mean_first_half": round(sum(a) / len(a), 4) if a else None,
                  "lower95_first_half": round(lo, 4) if lo is not None else None,
                  "n_second_half": len(b), "mean_second_half": round(sum(b) / len(b), 4) if b else None,
                  "lower95_second_half": round(ln, 4) if ln is not None else None,
                  "last50_mean": round(last, 4), "released": live, "reason": _why(n, lo, ln, last)}
    return out


def table(conn) -> dict:
    """What the dashboard reads (read only): {"rule", "at", "pockets": [rows, best lower bound first]}."""
    st = store.get(conn, "pockets") or {}
    return {"rule": {"min_n": MIN_N, "conf": 0.95, "last_n": LAST_N, "window_days": WINDOW_S // 86400},
            "at": st.get("at"), "pockets": st.get("rows") or []}


class Book:
    """The released pockets, rescanned every EVERY_S and kept in brain_state["pockets"] for the journal and the
    dashboard (rows with released_at / demoted_at carried over between scans)."""

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
        prev = {r["key"]: r for r in (store.get(self.conn, "pockets") or {}).get("rows") or []}
        for k, r in res.items():
            p = prev.get(k, {})
            r["released_at"], r["demoted_at"] = p.get("released_at"), p.get("demoted_at")
            if r["released"] and not p.get("released"):
                r["released_at"] = int(now)
            if p.get("released") and not r["released"]:
                r["demoted_at"] = int(now)
        self.live = {k for k, v in res.items() if v["released"]}
        rows = sorted(res.values(), key=lambda r: -(r["lower95"] if r["lower95"] is not None else -9))
        store.put(self.conn, "pockets", {"at": int(now), "live": sorted(self.live), "checked": len(res),
                                         "rows": rows[:300]})
        return self.live

    def match(self, kind: str, feats: dict) -> str | None:
        live = self.refresh()
        if kind == "signal" and feats.get("sig") == "v2":
            kind = "signal_v2"
        return next((k for k in keys(kind, feats) if k in live), None)
