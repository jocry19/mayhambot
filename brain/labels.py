"""Fixed labels for every opportunity, whatever anyone did with it (plan 05:07, 18.10.).

The learning account's own trades measure its exit mechanics as much as the opportunity, and the candle replays only
reach a few dozen rows an hour. So every row of `opportunities` is followed for an hour from the moment it was
reported: coins on a bonding curve at the sniper's live curve price (a database read), all others with DexScreener
snapshots in batches of 30, each coin every few minutes within a fixed call budget. After the hour the row gets its
labels, all after costs: the best price within 15 and 60 min, where it stood after 60 min, its low, whether it rugged
(-100 %), and what each plan would have made on that path. The pool model learns from them (learner.learn_labels).

Runs in its own thread with its own connection (Service.run); nothing here trades.
"""
from __future__ import annotations

import json
import logging
import time

from . import store
from .plans import PLANS

log = logging.getLogger("brain.labels")

HORIZON_S = 3600
FRESH_S = 600  # rows reported longer ago than this when first seen are not followed (no entry price any more)
NO_MARKET_S = 900  # no price at all this long after the report: dropped, nothing to learn
CURVE_AGE_S = 900  # a curve price stays the price until the next trade
DS_EVERY_S = 180
DS_CALLS = 10  # DexScreener calls per step (30 coins each); a step is a minute
MAX_TRACK = 6000
LONG_S = (4 * 3600, 24 * 3600)  # the longer horizons (overhaul 18.10.: is there a right tail beyond the hour?)
LONG_EVERY_S = ((4 * 3600, 900), (24 * 3600, 3600))  # DexScreener cadence by age after the first hour
MAX_LONG = 12000
DS_MISS_STOP = 3  # DexScreener without a pair this many times in a row (and no curve price): the path ends
STAKE_SOL = 0.1  # the stake the costs are taken at (the lab's)
SAVE_EVERY_S = 600  # the followed paths are kept in brain_state this often (a restart loses at most this much)
CAP = 10.0  # +1000 %: beyond it a label is a price glitch more often than a coin (18.10.: means of +155724 %)
RUG = 0.1  # the low under 10 % of the entry: rugged, counted as -100 %
COLS = (("label_at", "INTEGER"), ("label_horizon_min", "REAL"), ("label_n", "INTEGER"), ("label_entry_sol", "REAL"),
        ("label_max15", "REAL"), ("label_max60", "REAL"), ("label_ret60", "REAL"), ("label_min60", "REAL"),
        ("label_rug", "INTEGER"), ("label_outcomes", "TEXT"), ("label_rules", "TEXT"), ("label_cost", "REAL"),
        ("label_ret240", "REAL"), ("label_max240", "REAL"), ("label_ret1440", "REAL"), ("label_max1440", "REAL"),
        ("label_long_min", "REAL"))
KIND = {"sniper": "launch", "copytrade": "copy", "strategy": "copy", "wallet_model": "copy"}  # else 'market'


def _rules() -> dict:
    """Exit rules beyond the plans, each run on every labelled path (plan 'Hundertmal', midday 18.10.: the exit is
    the lever, mean best-in-60-min +20 % against held -6 %). Stop 50 % stands in for the rug exit."""
    from copytrade import sim

    out = {"hold60": sim.Variant(0, 1e9, 1.0, 0.0, 0.0, 60, 0)}
    for a in (0.2, 0.5, 1.0):
        out[f"trail30@{a:g}"] = sim.Variant(0, 1e9, 0.5, a, 0.3, 60, 0)
        out[f"trail20@{a:g}"] = sim.Variant(0, 1e9, 0.3, a, 0.2, 60, 0)
    out["tp50sl20"] = sim.Variant(0, 0.5, 0.2, 0.0, 0.0, 60, 0)
    out["tp100sl30"] = sim.Variant(0, 1.0, 0.3, 0.0, 0.0, 60, 0)
    out["tp30sl15"] = sim.Variant(0, 0.3, 0.15, 0.0, 0.0, 30, 0)
    return out


def cost(s) -> float:
    from copytrade import sim

    return sim.ROUNDTRIP_COST + s.extra_cost("pool")


def row_cost(s, source: str | None, dex: str | None, liq_usd: float | None) -> float:
    """Round trip after costs for one row: the lab's cost model (fee by venue, impact of the stake against the pool,
    transaction fees, delay by kind of trigger) at the venue and depth the coin had when it was first priced; the old
    flat cost where lab/ is missing."""
    try:
        from lab import costs
    except Exception:
        return cost(s)
    return costs.roundtrip(dex, liq_usd, STAKE_SOL, KIND.get(source or "", "market"))


def long_labels(pts: list, t0: float) -> dict:
    """ret and best price at 4 h and 24 h from the first price at or after t0 (before costs; label_cost holds them),
    from the last sample within each horizon; a low under RUG of the entry within it is -100 %. A horizon the path
    did not reach (to 90 %) stays out; label_long_min says how far it reached."""
    pts = sorted(p for p in pts if p[0] >= t0)
    if len(pts) < 2 or pts[0][1] <= 0:
        return {}
    p0, out = pts[0][1], {}
    for h, name in zip(LONG_S, ("240", "1440")):
        if pts[-1][0] < t0 + 0.9 * h:
            continue
        inside = [px for t, px in pts if t <= t0 + h + 600]
        rug = min(inside) <= RUG * p0
        out[f"label_ret{name}"] = -1.0 if rug else round(min(CAP, inside[-1] / p0 - 1), 5)
        out[f"label_max{name}"] = round(min(CAP, max(inside) / p0 - 1), 5)
    out["label_long_min"] = round((min(pts[-1][0], t0 + LONG_S[-1]) - t0) / 60, 1)
    return out


def candles(pts: list) -> list:
    """Minute candles (ts, open, high, low, close, volume 0) from sampled prices, from the first sample on."""
    out: list = []
    t0 = pts[0][0]
    for t, px in pts:
        b = t0 + (t - t0) // 60 * 60
        if out and out[-1][0] == b:
            _, o, h, lo, _, v = out[-1]
            out[-1] = (b, o, max(h, px), min(lo, px), px, v)
        else:
            out.append((b, px, px, px, px, 0.0))
    return out


def label(pts: list, c: float) -> dict | None:
    """Labels of one sampled path [[unix s, price], ...] (at least two points), net of the round trip cost c."""
    from copytrade import sim

    pts = sorted(pts)
    if len(pts) < 2 or pts[0][1] <= 0:
        return None
    t0, p0 = pts[0]
    in15 = [px for t, px in pts if t - t0 <= 900]
    lo = min(px for _, px in pts)
    rug = lo <= RUG * p0
    ret = -1.0 if rug else min(CAP, pts[-1][1] / p0 - 1 - c)
    cs = candles(pts)
    outs = {}
    for p in PLANS:
        r = sim.simulate(cs, t0, p.variant(0), cost=c)
        if r is not None:
            outs[p.name] = round(min(CAP, r[0]), 5)
    rules = {}
    for name, v in _rules().items():
        r = sim.simulate(cs, t0, v, cost=c)
        if r is not None:
            rules[name] = -1.0 if rug and name == "hold60" else round(min(CAP, r[0]), 5)
    return {"label_horizon_min": round((pts[-1][0] - t0) / 60, 1), "label_n": len(pts), "label_entry_sol": p0,
            "label_max15": round(min(CAP, max(in15) / p0 - 1 - c), 5),
            "label_max60": round(min(CAP, max(px for _, px in pts) / p0 - 1 - c), 5),
            "label_ret60": round(ret, 5), "label_min60": round(lo / p0 - 1 - c, 5), "label_rug": int(rug),
            "label_outcomes": json.dumps(outs), "label_rules": json.dumps(rules)}


def _default_curve(conn, mint, now):
    from sniper import store as sn_store  # type: ignore

    return sn_store.curve_now(conn, mint, CURVE_AGE_S, now=now)


def _default_snapshots(mints):
    from copytrade import market

    return market.snapshots(mints)


class Labeler:
    def __init__(self, conn, s, *, snapshots=None, curve_now=None, clock=time.time):
        self.conn, self.s, self.clock = conn, s, clock
        self.snapshots = snapshots or _default_snapshots
        self.curve_now = curve_now or _default_curve
        st = store.get(conn, "label_track") or {}
        # opportunity id -> [mint, t0, source] for the first hour; older saves hold [mint, t0]
        self.track: dict[str, list] = {k: (v + [""])[:3] for k, v in (st.get("rows") or {}).items()}
        self.long: dict[str, list] = st.get("long") or {}  # id -> [mint, t0] until 24 h (or the path ends)
        self.paths: dict[str, list] = st.get("paths") or {}  # mint -> [[unix s, price SOL], ...]
        self.ds_at: dict[str, float] = st.get("ds_at") or {}  # mint -> last DexScreener sample
        self.venue: dict[str, list] = st.get("venue") or {}  # mint -> [dex, liquidity USD] when first priced
        self.miss: dict[str, int] = st.get("miss") or {}  # mint -> DexScreener answers without a pair in a row
        self.saved_at = 0.0
        self.done = 0

    def _take_new(self, now: float) -> None:
        cur = store.get(self.conn, "label_cursor")
        if cur is None:  # first start: from what comes next
            cur = self.conn.execute("SELECT MAX(id) FROM opportunities").fetchone()[0] or 0
        rows = self.conn.execute("SELECT id, mint, ts, reported_at, source FROM opportunities WHERE id>? ORDER BY id "
                                 "LIMIT 5000", (cur,)).fetchall()
        for oid, mint, ts, rep, src in rows:
            if len(self.track) < MAX_TRACK and mint and now - (rep or ts or 0) <= FRESH_S:
                self.track[str(oid)] = [mint, rep or ts, src or ""]
        if rows:
            cur = rows[-1][0]
        store.put(self.conn, "label_cursor", cur)

    def _every(self, mint_age: float) -> float:
        if mint_age < HORIZON_S:
            return DS_EVERY_S
        return next((e for h, e in LONG_EVERY_S if mint_age < h), LONG_EVERY_S[-1][1])

    def _sample(self, now: float) -> None:
        youngest: dict[str, float] = {}
        for v in list(self.track.values()) + list(self.long.values()):
            youngest[v[0]] = min(youngest.get(v[0], 1e18), now - v[1])
        prices: dict[str, float] = {}
        ds = []
        for mint, age in youngest.items():
            try:
                cv = self.curve_now(self.conn, mint, now)
            except Exception:
                cv = None
            if cv and not cv.get("complete") and cv.get("price_sol"):
                prices[mint] = float(cv["price_sol"])
                self.venue.setdefault(mint, ["pumpfun", None])
                self.miss.pop(mint, None)
            elif self.miss.get(mint, 0) < DS_MISS_STOP and now - self.ds_at.get(mint, 0.0) >= self._every(age):
                ds.append(mint)
        # the first hour first, then the longest overdue
        ds.sort(key=lambda m: (youngest[m] >= HORIZON_S, self.ds_at.get(m, 0.0)))
        ds = ds[:DS_CALLS * 30]
        if ds:
            try:
                snaps = self.snapshots(ds)
            except Exception as e:
                log.warning("label snapshots failed: %s", e)
                snaps = None
            for m in ds:
                self.ds_at[m] = now
                if snaps is None:
                    continue
                sn = snaps.get(m)
                if sn is not None and sn.price_sol:
                    prices[m] = float(sn.price_sol)
                    self.venue.setdefault(m, [sn.dex_id, sn.liquidity_usd or getattr(sn, "curve_liq_usd", 0) or None])
                    self.miss.pop(m, None)
                else:
                    self.miss[m] = self.miss.get(m, 0) + 1
        for mint, px in prices.items():
            path = self.paths.setdefault(mint, [])
            if not path or now > path[-1][0]:
                path.append([round(now, 1), float(f"{px:.6g}")])

    def _write(self, oid: str, lab: dict) -> None:
        keys = list(lab)
        self.conn.execute(f"UPDATE opportunities SET {', '.join(k + '=?' for k in keys)} WHERE id=?",
                          (*[lab[k] for k in keys], int(oid)))

    def _finish(self, now: float) -> int:
        n = 0
        for oid, (mint, t0, src) in list(self.track.items()):
            age = now - t0
            pts = [p for p in self.paths.get(mint, ()) if t0 <= p[0] <= t0 + HORIZON_S + 120]
            if not pts and age > NO_MARKET_S:
                del self.track[oid]
                continue
            if age < HORIZON_S:
                continue
            del self.track[oid]
            dex, liq = (self.venue.get(mint) or [None, None])[:2]
            c = row_cost(self.s, src, dex, liq)
            lab = label(pts, c)
            if lab is None:
                continue
            self._write(oid, {"label_at": int(now), **lab, "label_cost": round(c, 5)})
            n += 1
            if len(self.long) < MAX_LONG:
                self.long[oid] = [mint, t0]
        for oid, (mint, t0) in list(self.long.items()):
            if now - t0 < LONG_S[-1] + 600 and self.miss.get(mint, 0) < DS_MISS_STOP:
                continue
            del self.long[oid]
            pts = list(self.paths.get(mint, []))
            if self.miss.get(mint, 0) >= DS_MISS_STOP and pts:
                # the market is gone before the day was over: no survivors only. A curve coin keeps its last curve
                # price (no trade, no move, still sellable on the curve); a pool that vanished is worth nothing
                curve = (self.venue.get(mint) or [""])[0] == "pumpfun"
                pts.append([t0 + LONG_S[-1], pts[-1][1] if curve else 0.0])
            lab = long_labels(pts, t0)
            if lab:
                self._write(oid, lab)
        first: dict[str, float] = {}
        for v in list(self.track.values()) + list(self.long.values()):
            first[v[0]] = min(first.get(v[0], v[1]), v[1])
        self.ds_at = {m: t for m, t in self.ds_at.items() if m in first}
        self.venue = {m: v for m, v in self.venue.items() if m in first}
        self.miss = {m: v for m, v in self.miss.items() if m in first}
        self.paths = {m: [p for p in pts if p[0] >= first[m]] for m, pts in self.paths.items() if m in first}
        return n

    def step(self) -> int:
        """One round (a minute): take new rows, sample prices, write the labels of rows an hour old and the long
        horizons of rows a day old. Returns the number labelled (first hour)."""
        now = self.clock()
        self._take_new(now)
        self._sample(now)
        n = self._finish(now)
        self.done += n
        if now - self.saved_at >= SAVE_EVERY_S:
            self.save(now)
        return n

    def save(self, now: float | None = None) -> None:
        now = now or self.clock()
        self.saved_at = now
        store.put(self.conn, "label_track", {"at": int(now), "rows": self.track, "long": self.long,
                                             "paths": self.paths, "ds_at": self.ds_at, "venue": self.venue,
                                             "miss": self.miss})
