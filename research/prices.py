"""Point-in-time price index per coin from trades, DexScreener snapshots and OHLCV candles.

price_at(mint, ts)        last known price at or before ts (never looks ahead)
max_between(mint, a, b)   highest confirmed price in (a, b]
min_between(mint, a, b)   lowest price in (a, b]
"""
import bisect
from collections import defaultdict

from research.config import rsettings


class _Sparse:
    """O(1) range max/min over a fixed array."""

    def __init__(self, vals, fn):
        self.fn = fn
        self.t = [list(vals)]
        j = 1
        while (1 << j) <= len(vals):
            prev = self.t[-1]
            half = 1 << (j - 1)
            self.t.append([fn(prev[i], prev[i + half]) for i in range(len(vals) - (1 << j) + 1)])
            j += 1

    def query(self, i, j):  # inclusive i..j
        if i > j:
            return None
        k = (j - i + 1).bit_length() - 1
        return self.fn(self.t[k][i], self.t[k][j - (1 << k) + 1])


class PriceIndex:
    def __init__(self):
        self.ts, self.px, self.hi_ts, self.hi, self.lo_ts, self.lo = {}, {}, {}, {}, {}, {}
        self._max, self._min = {}, {}
        self.supply = {}

    @classmethod
    def from_db(cls, conn, mints=None):
        pts, highs, lows = defaultdict(list), defaultdict(list), defaultdict(list)
        cond, args = "", ()
        if mints is not None:
            mints = list(mints)
            cond, args = f" AND mint IN ({','.join('?' * len(mints))})", tuple(mints)
        trade_pts = defaultdict(list)
        for r in conn.execute("SELECT mint, ts, price_usd, source FROM trades WHERE price_usd > 0" + cond, args):
            trade_pts[r[0]].append((r[1], r[2], r[3]))
        for r in conn.execute("SELECT mint, ts, price_usd FROM token_prices WHERE price_usd > 0" + cond, args):
            pts[r[0]].append((r[1], r[2]))
        cand = defaultdict(list)
        for r in conn.execute("SELECT mint, tf_sec, ts, h, l, c FROM research_candles WHERE c > 0" + cond, args):
            cand[r[0]].append((r[1], r[2], r[3], r[4], r[5]))
        # wallet-history prices are derived (SOL price today, multi-hop swaps) and can be far off: when a coin
        # has candles, only candles + pool-feed trades count; otherwise all trades are used
        for mint, tp in trade_pts.items():
            pool_only = mint in cand
            pts[mint].extend((t, p) for t, p, src in tp if not pool_only or src in ("geckoterminal", "test_pool"))
        idx = cls()
        for mint, cs in cand.items():
            for tf, ts, h, l, c in cs:
                pts[mint].append((ts + tf - 1, c))
                highs[mint].append((ts + tf - 1, h))
                lows[mint].append((ts + tf - 1, l))
        for mint, p in pts.items():
            idx._add(mint, p, highs.get(mint, []), lows.get(mint, []))
        for r in conn.execute("SELECT mint, price_usd, mcap_usd FROM tokens WHERE price_usd > 0 AND mcap_usd > 0"
                              + cond, args):
            idx.supply[r[0]] = r[2] / r[1]
        for r in conn.execute("SELECT mint, supply FROM research_tokens WHERE supply > 0" + cond, args):
            idx.supply[r[0]] = r[1]
        return idx

    def _add(self, mint, pts, highs=(), lows=()):
        pts = sorted(pts)
        prices = [p for _, p in pts]
        # a print only counts as a high if a neighbour confirms it within 3x (filters single bad prints)
        conf = []
        for i, pr in enumerate(prices):
            nb = [prices[j] for j in (i - 1, i + 1) if 0 <= j < len(prices)]
            conf.append(pr if not nb or any(pr <= 3 * x for x in nb) else min(nb))
        hp = sorted([(t, c) for (t, _), c in zip(pts, conf)] + list(highs))
        lp = sorted([(t, p) for t, p in pts] + list(lows))
        self.ts[mint] = [t for t, _ in pts]
        self.px[mint] = prices
        self.hi_ts[mint] = [t for t, _ in hp]
        self.hi[mint] = [p for _, p in hp]
        self.lo_ts[mint] = [t for t, _ in lp]
        self.lo[mint] = [p for _, p in lp]
        self._max.pop(mint, None)
        self._min.pop(mint, None)

    def has(self, mint):
        return mint in self.ts

    def price_at(self, mint, ts):
        tss = self.ts.get(mint)
        if not tss:
            return None
        i = bisect.bisect_right(tss, ts) - 1
        return self.px[mint][i] if i >= 0 else None

    def last_ts(self, mint):
        tss = self.ts.get(mint)
        return max(tss[-1], self.hi_ts[mint][-1]) if tss else None

    def first_ts(self, mint):
        tss = self.ts.get(mint)
        return min(tss[0], self.hi_ts[mint][0]) if tss else None

    def max_between(self, mint, a, b):
        tss = self.hi_ts.get(mint)
        if not tss:
            return None
        if mint not in self._max:
            self._max[mint] = _Sparse(self.hi[mint], max)
        return self._max[mint].query(bisect.bisect_right(tss, a), bisect.bisect_right(tss, b) - 1)

    def min_between(self, mint, a, b):
        tss = self.lo_ts.get(mint)
        if not tss:
            return None
        if mint not in self._min:
            self._min[mint] = _Sparse(self.lo[mint], min)
        return self._min[mint].query(bisect.bisect_right(tss, a), bisect.bisect_right(tss, b) - 1)

    def first_time_reaching(self, mint, a, b, level):
        """First ts in (a, b] whose high >= level, else None."""
        tss = self.hi_ts.get(mint)
        if not tss:
            return None
        for i in range(bisect.bisect_right(tss, a), bisect.bisect_right(tss, b)):
            if self.hi[mint][i] >= level:
                return tss[i]
        return None

    def mcap(self, mint, price):
        if not price:
            return None
        return price * self.supply.get(mint, rsettings.default_supply)

    def coverage_until(self, mint):
        return self.last_ts(mint) or 0
