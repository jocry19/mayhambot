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
SAVE_EVERY_S = 600  # the followed paths are kept in brain_state this often (a restart loses at most this much)
RUG = 0.1  # the low under 10 % of the entry: rugged, counted as -100 %
COLS = (("label_at", "INTEGER"), ("label_horizon_min", "REAL"), ("label_n", "INTEGER"), ("label_entry_sol", "REAL"),
        ("label_max15", "REAL"), ("label_max60", "REAL"), ("label_ret60", "REAL"), ("label_min60", "REAL"),
        ("label_rug", "INTEGER"), ("label_outcomes", "TEXT"))


def cost(s) -> float:
    from copytrade import sim

    return sim.ROUNDTRIP_COST + s.extra_cost("pool")


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
    ret = -1.0 if rug else pts[-1][1] / p0 - 1 - c
    cs = candles(pts)
    outs = {}
    for p in PLANS:
        r = sim.simulate(cs, t0, p.variant(0), cost=c)
        if r is not None:
            outs[p.name] = round(r[0], 5)
    return {"label_horizon_min": round((pts[-1][0] - t0) / 60, 1), "label_n": len(pts), "label_entry_sol": p0,
            "label_max15": round(max(in15) / p0 - 1 - c, 5), "label_max60": round(max(px for _, px in pts) / p0 - 1 - c, 5),
            "label_ret60": round(ret, 5), "label_min60": round(lo / p0 - 1 - c, 5), "label_rug": int(rug),
            "label_outcomes": json.dumps(outs)}


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
        self.track: dict[str, list] = st.get("rows") or {}  # opportunity id -> [mint, t0]
        self.paths: dict[str, list] = st.get("paths") or {}  # mint -> [[unix s, price SOL], ...]
        self.ds_at: dict[str, float] = st.get("ds_at") or {}  # mint -> last DexScreener sample
        self.saved_at = 0.0
        self.done = 0

    def _take_new(self, now: float) -> None:
        cur = store.get(self.conn, "label_cursor")
        if cur is None:  # first start: from what comes next
            cur = self.conn.execute("SELECT MAX(id) FROM opportunities").fetchone()[0] or 0
        rows = self.conn.execute("SELECT id, mint, ts, reported_at FROM opportunities WHERE id>? ORDER BY id LIMIT 5000",
                                 (cur,)).fetchall()
        for oid, mint, ts, rep in rows:
            if len(self.track) < MAX_TRACK and mint and now - (rep or ts or 0) <= FRESH_S:
                self.track[str(oid)] = [mint, rep or ts]
        if rows:
            cur = rows[-1][0]
        store.put(self.conn, "label_cursor", cur)

    def _sample(self, now: float) -> None:
        mints = {m for m, _ in self.track.values()}
        prices: dict[str, float] = {}
        ds = []
        for mint in mints:
            try:
                cv = self.curve_now(self.conn, mint, now)
            except Exception:
                cv = None
            if cv and not cv.get("complete") and cv.get("price_sol"):
                prices[mint] = float(cv["price_sol"])
            elif now - self.ds_at.get(mint, 0.0) >= DS_EVERY_S:
                ds.append(mint)
        ds.sort(key=lambda m: self.ds_at.get(m, 0.0))
        ds = ds[:DS_CALLS * 30]
        if ds:
            try:
                snaps = self.snapshots(ds)
            except Exception as e:
                log.warning("label snapshots failed: %s", e)
                snaps = {}
            for m in ds:
                self.ds_at[m] = now
                sn = snaps.get(m)
                if sn is not None and sn.price_sol:
                    prices[m] = float(sn.price_sol)
        for mint, px in prices.items():
            path = self.paths.setdefault(mint, [])
            if not path or now > path[-1][0]:
                path.append([round(now, 1), float(f"{px:.6g}")])

    def _finish(self, now: float) -> int:
        c = cost(self.s)
        n = 0
        for oid, (mint, t0) in list(self.track.items()):
            age = now - t0
            pts = [p for p in self.paths.get(mint, ()) if t0 <= p[0] <= t0 + HORIZON_S + 120]
            if not pts and age > NO_MARKET_S:
                del self.track[oid]
                continue
            if age < HORIZON_S:
                continue
            del self.track[oid]
            lab = label(pts, c)
            if lab is None:
                continue
            keys = list(lab)
            self.conn.execute(f"UPDATE opportunities SET label_at=?, {', '.join(k + '=?' for k in keys)} WHERE id=?",
                              (int(now), *[lab[k] for k in keys], int(oid)))
            n += 1
        first: dict[str, float] = {}
        for m, t0 in self.track.values():
            first[m] = min(first.get(m, t0), t0)
        self.ds_at = {m: t for m, t in self.ds_at.items() if m in first}
        self.paths = {m: [p for p in pts if p[0] >= first[m]] for m, pts in self.paths.items() if m in first}
        return n

    def step(self) -> int:
        """One round (a minute): take new rows, sample prices, write the labels of rows an hour old. Returns the
        number labelled."""
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
        store.put(self.conn, "label_track", {"at": int(now), "rows": self.track, "paths": self.paths,
                                             "ds_at": self.ds_at})
