"""Organic exits: sell by the state of the coin, not by fixed percentages.

While a position is open the brain looks at it once a minute and asks a second model what the next FWD_MIN minutes
will bring from here, given the coin's state: how far up or down since the buy, how far below its high, the last
minute and the last five, whether the trading volume still holds up or dries out, how long it is held, whether the
copied whale sold, and whether the other tracked smart wallets are buying into it or selling out. The answer
decides: hold (more to come), sell half (in profit and the outlook turns doubtful) or sell the rest (it is over).

The exit model learns from every opportunity the brain replays, not only from its own trades: each minute of each
replayed coin is one example (its state, and what the price did in the following FWD_MIN minutes), from the very
same minute candles as the plan replays. The same states are built live from the DexScreener snapshot and the
tracker's swaps, so what the model learned is what it sees when it decides.

It only takes over when it earned it: every finished opportunity is also replayed with the organic exit, using the
model as it was BEFORE that coin was learned (out of sample), next to the exit plan the brain chose. Live, the
organic exit decides only while its replays beat the plans (`verdict`); otherwise the plans' target/stop/trail stay.
Hard guards hold either way: a crash stop, the rug check (liquidity pulled) and a maximum hold.
"""
from __future__ import annotations

import math
import time

from .model import Brain

FWD_MIN = 10  # the question each minute: what does the price do in the next 10 minutes
CRASH_STOP = 0.30  # hard floor below the buy price whatever the model says (winner study 17.10.: stop -30 %, trail late)
MAX_HOLD_MIN = 240
RUNNER_UP = 1.0  # winner study 17.10.: once a coin doubled, a 30 % trail from the high took +123 % on the 3x coins
RUNNER_TRAIL = 0.30
CHECK_S = 60  # one decision a minute, like the candles it learned from
GRACE_MIN = 3  # no model decision in the first minutes: at t+0 the spread is the only move (18.10.: sold after 0-2 s at -6..-8 %)
HIST_S = 60  # live: the model decides only once the trader itself watched the coin a minute (after a restart too)
SAMPLE_W = 0.25  # minute-samples of one coin are correlated: each counts a quarter
MIN_N = 400  # weighted samples before the model may decide anything
VERDICT_MIN = 30  # finished out-of-sample replays before the organic exit may take over live
VERDICT_WINDOW = 300


def _b(x, edges, labels):
    for e, lab in zip(edges, labels):
        if x < e:
            return lab
    return labels[-1]


def tokens(kind: str, liq: str | None, up: float, dd: float, mom1: float | None, mom5: float | None,
           volr: float | None, held_min: float, whale_sold: bool | None, smart: str | None) -> list[str]:
    """The coin's state as sparse tokens, plus a few pairs (a pullback means something else at +50 % than at -10 %)."""
    t = {
        "up": _b(up, (-0.2, -0.1, 0, 0.1, 0.3, 0.6), ("<-20%", "-20..-10%", "-10..0%", "0..10%", "10..30%", "30..60%", "60%+")),
        "dd": _b(dd, (0.05, 0.1, 0.2, 0.35), ("<5%", "5-10%", "10-20%", "20-35%", "35%+")),
        "held": _b(held_min, (5, 15, 45, 120), ("<5m", "5-15m", "15-45m", "45-120m", "120m+")),
    }
    if mom1 is not None:
        t["m1"] = _b(mom1, (-0.05, -0.01, 0.01, 0.05), ("<-5%", "-5..-1%", "flat", "1..5%", ">5%"))
    if mom5 is not None:
        t["m5"] = _b(mom5, (-0.15, -0.05, 0.05, 0.15), ("<-15%", "-15..-5%", "flat", "5..15%", ">15%"))
    if volr is not None:
        t["vol"] = _b(volr, (0.3, 0.6, 0.95), ("dried", "fading", "holding", "peak"))
    if whale_sold is not None:
        t["whale"] = "sold" if whale_sold else "holds"
    if smart:
        t["smart"] = smart
    out = [f"kind={kind}"] + ([f"liq={'curve' if liq == 'curve' else 'pool'}"] if liq else [])
    out += [f"{k}={v}" for k, v in t.items()]
    for a, b in (("up", "dd"), ("up", "vol"), ("m5", "vol"), ("up", "smart"), ("dd", "m1")):
        if a in t and b in t:
            out.append(f"{a}={t[a]}|{b}={t[b]}")
    return out


def smart_flow(swaps, since: float, until: float, wallet: str | None) -> str | None:
    """Net direction of the tracked smart wallets (other than the copied one) in this coin since the buy."""
    buys = sum(1 for ts, w, side in swaps if since < ts <= until and w != wallet and side == "buy")
    sells = sum(1 for ts, w, side in swaps if since < ts <= until and w != wallet and side == "sell")
    if not buys and not sells:
        return "quiet"
    return "in" if buys > sells else "out" if sells > buys else "mixed"


def coin_swaps(conn, mint: str, lo: float, hi: float) -> list:
    try:
        return [(r[0], r[1], r[2]) for r in conn.execute(
            "SELECT ts, wallet, side FROM ct_swaps WHERE mint=? AND ts BETWEEN ? AND ? ORDER BY ts",
            (mint, int(lo), int(hi))).fetchall()]
    except Exception:
        return []


def new_model(half_life_h: float) -> Brain:
    return Brain(1, half_life_h)


def mature(xm) -> bool:
    return xm is not None and xm.n >= MIN_N


def advise(xm, toks, s, halved: bool, up: float) -> tuple[str, float, float]:
    """("hold" | "half" | "sell", expected next-10-min return, P(it is positive))."""
    mean, sd = xm.predict(toks)[0]
    p = 0.5 * (1 + math.erf(mean / sd / math.sqrt(2))) if sd > 0 else float(mean > 0)
    if p < s.exit_sell_p:
        return "sell", mean, p
    if p < s.exit_half_p and not halved and up >= s.exit_half_min_up:
        return "half", mean, p
    return "hold", mean, p


# ---------------------------------------------------------------------------------------------- from minute candles
def _entry_index(cs, t0: float) -> int:
    for i, c in enumerate(cs):
        if c[0] + 60 > t0:
            return i
    return len(cs)


class _Path:
    """State of a replayed position minute by minute, built the way the live trader sees it."""

    def __init__(self, cs, i0, kind, liq, wallet, swaps, whale_sells):
        self.cs, self.i0, self.kind, self.liq, self.wallet = cs, i0, kind, liq, wallet
        self.swaps, self.whale_sells = swaps, whale_sells
        self.entry = cs[i0][4]
        self.t0 = cs[i0][0]

    def state(self, k, high, vpeak):
        c = self.cs
        px = c[k][4]
        end = c[k][0] + 60
        vol5 = sum(x[5] or 0 for x in c[max(0, k - 4):k + 1])
        mom5 = px / c[k - 5][4] - 1 if k >= 5 and c[k - 5][4] > 0 else None
        mom1 = px / c[k - 1][4] - 1 if c[k - 1][4] > 0 else None
        whale = any(self.t0 < e <= end for e in self.whale_sells) if self.kind == "whale" and self.wallet else None
        return tokens(self.kind, self.liq, px / self.entry - 1, 1 - px / high if high > 0 else 0.0, mom1, mom5,
                      vol5 / vpeak if vpeak > 0 else None, (c[k][0] - self.t0) / 60, whale,
                      smart_flow(self.swaps, self.t0, end, self.wallet))

    def walk(self):
        """(candle index, tokens, high so far) for every minute after the entry."""
        c = self.cs
        high = self.entry
        vpeak = sum(x[5] or 0 for x in c[max(0, self.i0 - 4):self.i0 + 1])
        for k in range(self.i0 + 1, len(c)):
            high = max(high, c[k][2] or c[k][4])
            vpeak = max(vpeak, sum(x[5] or 0 for x in c[max(0, k - 4):k + 1]))
            yield k, self.state(k, high, vpeak), high


def _path(cs, t0, kind, liq, wallet, swaps, whale_sells):
    i0 = _entry_index(cs, t0)
    if i0 >= len(cs) - 1 or i0 < 1 or not cs[i0][4] or cs[i0][4] <= 0:
        return None
    return _Path(cs, i0, kind, liq, wallet, swaps, whale_sells)


def learn_path(xm, cs, t0, kind, liq, wallet, swaps, whale_sells, from_min: float, to_min: float,
               w: float = 1.0) -> int:
    """Teach the exit model every minute between from_min and to_min after the entry whose next FWD_MIN minutes are
    in the candles. Returns samples learned."""
    p = _path(cs, t0, kind, liq, wallet, swaps, whale_sells)
    if p is None:
        return 0
    n = 0
    for k, toks, _ in p.walk():
        m = (cs[k][0] - p.t0) / 60
        if m < from_min:
            continue
        if m >= to_min or k + FWD_MIN >= len(cs):
            break
        if cs[k + FWD_MIN][0] - cs[k][0] > (FWD_MIN + 5) * 60 or cs[k][4] <= 0:
            continue  # a hole in the candles: no honest label
        xm.update(toks, {0: cs[k + FWD_MIN][4] / cs[k][4] - 1}, SAMPLE_W * w)
        n += 1
    return n


def replay(xm, cs, t0, kind, liq, wallet, swaps, whale_sells, s, cost: float) -> tuple[float, float, str] | None:
    """Net gain, minutes held and reason of the organic exit on these candles (partial sells included)."""
    p = _path(cs, t0, kind, liq, wallet, swaps, whale_sells)
    if p is None:
        return None
    entry, left, got = p.entry, 1.0, 0.0
    floor = entry * (1 - CRASH_STOP)
    last = None
    for k, toks, high in p.walk():
        ts, o, h, l, c, _ = cs[k][:6]
        m = (ts - p.t0) / 60
        last = (c, m)
        if l <= floor:  # same fill rule as copytrade.sim's stop
            px = min(o, floor)
            if l < 0.5 * floor:
                px = min(px, c)
            return got + left * (px / entry) - 1 - cost, m, "crash"
        if m >= MAX_HOLD_MIN:
            return got + left * (c / entry) - 1 - cost, m, "time"
        if high / entry - 1 >= RUNNER_UP and c <= high * (1 - RUNNER_TRAIL):
            return got + left * (c / entry) - 1 - cost, m, "trail"
        if m < GRACE_MIN:
            continue
        act, _, _ = advise(xm, toks, s, left < 1.0, c / entry - 1)
        if act == "sell":
            return got + left * (c / entry) - 1 - cost, m, "organic"
        if act == "half":
            got += 0.5 * c / entry
            left = 0.5
    if last is None:
        return None
    return got + left * (last[0] / entry) - 1 - cost, last[1], "end"


# ---------------------------------------------------------------------------------------------- live verdict
def verdict(conn, kind: str | None = None) -> dict:
    """Out-of-sample: the organic exit's replays against the chosen plans' replays of the same opportunities; with
    `kind` ("whale", "signal", "launch", "market") only that kind's coins (another account holding such coins asks
    for its own kind)."""
    try:
        rows = conn.execute("SELECT organic, plan_gain FROM brain_exit_evals WHERE organic IS NOT NULL AND "
                            "plan_gain IS NOT NULL AND (? IS NULL OR kind=?) ORDER BY opp_id DESC LIMIT ?",
                            (kind, kind, VERDICT_WINDOW)).fetchall()
    except Exception:
        rows = []
    n = len(rows)
    org = sum(r[0] for r in rows) / n if n else None
    plan = sum(r[1] for r in rows) / n if n else None
    return {"n": n, "organic": org, "plan": plan, "live": n >= VERDICT_MIN and org is not None and org > plan}


class Live:
    """Per open position: what the trader saw since the buy (minute prices, volume peak, last decision)."""

    def __init__(self):
        self.hist: dict[int, list] = {}
        self.vpeak: dict[int, float] = {}
        self.checked: dict[int, float] = {}
        self.high_at: dict[int, float] = {}  # when the highest price since the buy was seen
        self._high: dict[int, float] = {}

    def observe(self, pid: int, now: float, px: float, vol_m5: float | None) -> None:
        h = self.hist.setdefault(pid, [])
        h.append((now, px))
        while h and h[0][0] < now - 15 * 60:
            h.pop(0)
        if vol_m5:
            self.vpeak[pid] = max(self.vpeak.get(pid, 0.0), vol_m5)
        if px > self._high.get(pid, 0.0):
            self._high[pid], self.high_at[pid] = px, now

    def price_ago(self, pid: int, now: float, secs: float):
        best = None
        for ts, px in self.hist.get(pid, []):
            if ts <= now - secs:
                best = px
        return best

    def seen_for(self, pid: int, now: float) -> float:
        h = self.hist.get(pid)
        return now - h[0][0] if h else 0.0

    def due(self, pid: int, now: float) -> bool:
        if now - self.checked.get(pid, 0) >= CHECK_S:
            self.checked[pid] = now
            return True
        return False

    def drop(self, pid: int) -> None:
        for d in (self.hist, self.vpeak, self.checked, self.high_at, self._high):
            d.pop(pid, None)


def now() -> float:
    return time.time()
