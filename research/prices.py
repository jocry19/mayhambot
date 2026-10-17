"""Point-in-time price index per coin from trades, DexScreener snapshots and OHLCV candles.

price_at(mint, ts)        last known price at or before ts (never looks ahead)
max_between(mint, a, b)   highest confirmed price in (a, b]
min_between(mint, a, b)   lowest price in (a, b]

A candle's high and low can lie anywhere inside the candle, so they only count for a window (a, b] when the
whole candle lies inside it (start after a, end at or before b). Otherwise a high from before an entry inside
the entry candle would count as reached after the entry. Candle closes count as prints at the candle end.
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


MIN_LIQ_SHARE = 0.02  # snapshot prints need liquidity >= 2 % of the market cap
SPIKE = 5.0  # a single print 5x above (or below) both neighbours is a bad print, not a price


def _drop_spikes(pts):
    """Drop isolated bad prints: a price SPIKE times higher or lower than both the print before and the one
    after. A real move keeps the new level on the next print; three such prints alone turned a backtest from
    -14 % to +1 % (10/2026). Uses the next print only to reject a value, never to create one."""
    if len(pts) < 3:
        return pts
    out = [pts[0]]
    for i in range(1, len(pts) - 1):
        a, p, b = pts[i - 1][1], pts[i][1], pts[i + 1][1]
        if a > 0 and b > 0 and ((p > SPIKE * a and p > SPIKE * b) or (p * SPIKE < a and p * SPIKE < b)):
            continue
        out.append(pts[i])
    out.append(pts[-1])
    return out


class PriceIndex:
    def __init__(self):
        self.ts, self.px, self.hi_ts, self.hi, self.lo_ts, self.lo = {}, {}, {}, {}, {}, {}
        self._max, self._min = {}, {}
        self.cand = {}  # mint -> {tf: (starts, highs, lows)}
        self._cmax, self._cmin = {}, {}
        self.supply = {}
        self.fetched = {}  # mint -> time the newest candles were fetched (no candle after it = no trades)

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
        # snapshots whose pool is far too thin for the quoted market cap (liquidity < 2 % of it) show prices nobody
        # can trade at (e.g. "264 M$" at 2-6 k$ liquidity); swaps and candles stay, they are real trades
        for r in conn.execute("SELECT mint, ts, price_usd, mcap_usd, liquidity_usd FROM token_prices WHERE price_usd > 0"
                              + cond, args):
            if r[3] and r[4] is not None and r[4] < MIN_LIQ_SHARE * r[3]:
                continue
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
        for mint, p in pts.items():
            idx._add(mint, p, candles=[(tf, ts, h, l) for tf, ts, h, l, _ in cand.get(mint, [])])
        for r in conn.execute("SELECT mint, price_usd, mcap_usd FROM tokens WHERE price_usd > 0 AND mcap_usd > 0"
                              + cond, args):
            idx.supply[r[0]] = r[2] / r[1]
        for r in conn.execute("SELECT mint, supply FROM research_tokens WHERE supply > 0" + cond, args):
            idx.supply[r[0]] = r[1]
        try:
            for r in conn.execute("SELECT mint, candles_fetched_at FROM research_tokens WHERE candles_fetched_at > 0"
                                  + cond, args):
                if r[0] in cand:
                    idx.fetched[r[0]] = r[1]
        except Exception:  # noqa: BLE001  (column added by ensure_schema)
            pass
        return idx

    def _add(self, mint, pts, highs=(), lows=(), candles=()):
        """pts: (ts, price) prints; highs/lows: extra point extremes at their ts; candles: (tf, start, high, low)."""
        pts = _drop_spikes(sorted(pts))
        by_tf = {}
        for tf, st, h, l in sorted(candles, key=lambda c: (c[0], c[1])):
            by_tf.setdefault(tf, ([], [], []))
            by_tf[tf][0].append(st)
            by_tf[tf][1].append(h)
            by_tf[tf][2].append(l)
        if by_tf:
            self.cand[mint] = by_tf
        else:
            self.cand.pop(mint, None)
        self._cmax.pop(mint, None)
        self._cmin.pop(mint, None)
        prices = [p for _, p in pts]
        # a print only counts as a high if the previous print confirms it within 3x (filters single bad prints;
        # only the earlier neighbour, so a high at t never depends on a later price)
        conf = []
        for i, pr in enumerate(prices):
            nb = [prices[i - 1]] if i > 0 else []
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

    def last_time_before(self, mint, ts):
        """Time of the last price print at or before ts (None if none)."""
        tss = self.ts.get(mint)
        if not tss:
            return None
        i = bisect.bisect_right(tss, ts) - 1
        return tss[i] if i >= 0 else None

    def last_ts(self, mint):
        tss = self.ts.get(mint)
        if not tss:
            return None
        ends = [st[-1] + tf - 1 for tf, (st, _, _) in self.cand.get(mint, {}).items()]
        return max([tss[-1], self.hi_ts[mint][-1]] + ends)

    def first_ts(self, mint):
        tss = self.ts.get(mint)
        if not tss:
            return None
        starts = [st[0] for st, _, _ in self.cand.get(mint, {}).values()]
        return min([tss[0], self.hi_ts[mint][0]] + starts)

    def _candle_range(self, mint, a, b):
        """Per timeframe: (tf, i, j) index range of candles lying fully inside (a, b]."""
        for tf, (st, _, _) in self.cand.get(mint, {}).items():
            i, j = bisect.bisect_right(st, a), bisect.bisect_right(st, b - tf + 1) - 1
            if i <= j:
                yield tf, i, j

    def _candle_ext(self, mint, a, b, high):
        cache, fn, k = (self._cmax, max, 1) if high else (self._cmin, min, 2)
        if mint not in cache:
            cache[mint] = {tf: _Sparse(v[k], fn) for tf, v in self.cand[mint].items()}
        vals = [cache[mint][tf].query(i, j) for tf, i, j in self._candle_range(mint, a, b)]
        return fn(vals) if vals else None

    def max_between(self, mint, a, b):
        tss = self.hi_ts.get(mint)
        if not tss:
            return None
        if mint not in self._max:
            self._max[mint] = _Sparse(self.hi[mint], max)
        v = self._max[mint].query(bisect.bisect_right(tss, a), bisect.bisect_right(tss, b) - 1)
        c = self._candle_ext(mint, a, b, True) if mint in self.cand else None
        return v if c is None else c if v is None else max(v, c)

    def min_between(self, mint, a, b):
        tss = self.lo_ts.get(mint)
        if not tss:
            return None
        if mint not in self._min:
            self._min[mint] = _Sparse(self.lo[mint], min)
        v = self._min[mint].query(bisect.bisect_right(tss, a), bisect.bisect_right(tss, b) - 1)
        c = self._candle_ext(mint, a, b, False) if mint in self.cand else None
        return v if c is None else c if v is None else min(v, c)

    def _first(self, mint, a, b, hit, k):
        """Earliest time in (a, b] a print or a whole candle meets hit(value); a candle counts at its end."""
        best = None
        tss, vals = (self.hi_ts, self.hi) if k == 1 else (self.lo_ts, self.lo)
        t_ = tss.get(mint)
        if t_:
            for i in range(bisect.bisect_right(t_, a), bisect.bisect_right(t_, b)):
                if hit(vals[mint][i]):
                    best = t_[i]
                    break
        for tf, i, j in self._candle_range(mint, a, b):
            st, v = self.cand[mint][tf][0], self.cand[mint][tf][k]
            for x in range(i, j + 1):
                end = st[x] + tf - 1
                if best is not None and end >= best:
                    break
                if hit(v[x]):
                    best = end
                    break
        return best

    def first_time_reaching(self, mint, a, b, level):
        """First ts in (a, b] whose high >= level, else None."""
        return self._first(mint, a, b, lambda v: v >= level, 1)

    def first_time_below(self, mint, a, b, level):
        return self._first(mint, a, b, lambda v: v <= level, 2)

    def mcap(self, mint, price):
        if not price:
            return None
        return price * self.supply.get(mint, rsettings.default_supply)

    def coverage_until(self, mint):
        """Prices are known up to here. A candle fetch returns everything up to the fetch time, so a coin that
        stopped trading (dead, rugged) is still covered until then at its last price; without this only coins
        that kept trading would ever get a label (survivorship bias)."""
        return max(self.last_ts(mint) or 0, self.fetched.get(mint, 0))
