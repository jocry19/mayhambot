"""Exit/entry simulator on 1-minute candles and the parameter grid the learner searches.

A variant = (entry delay, take profit, stop loss, trailing stop, max hold, sell-at-inflow-peak).
`simulate()` replays one variant on candles after an entry; `grid_gains()` replays the whole
grid at once for one entry, which is what the learner stores per trade / whale buy / signal.
Candles: (ts, o, h, l, c, volume_usd), ascending, 60 s.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from itertools import product
from typing import Sequence

ROUNDTRIP_COST = 0.03  # DEX fees + slippage both ways (paper calibration corrects this per strategy)

GRID_VERSION = 1
DELAYS_S = (15, 60, 180)  # whale mode: wait this long after the whale's buy
TAKE_PROFITS = (0.15, 0.25, 0.4, 0.6, 1.0, 1.5)
STOP_LOSSES = (0.1, 0.2, 0.3)
TRAILS = ((0.0, 0.0), (0.15, 0.08), (0.3, 0.15))  # (activate, drawdown); 0 = off
HOLDS_MIN = (15, 45, 120, 240)
INFLOW_EXIT = (0, 1)


@dataclass(frozen=True)
class Variant:
    delay_s: int
    take_profit: float
    stop_loss: float
    trail_activate: float
    trail_drawdown: float
    max_hold_min: float
    inflow_exit: int

    def key(self) -> str:
        return (f"d{self.delay_s}_tp{self.take_profit:g}_sl{self.stop_loss:g}_tr{self.trail_activate:g}/"
                f"{self.trail_drawdown:g}_h{self.max_hold_min:g}_in{self.inflow_exit}")

    def as_dict(self) -> dict:
        return asdict(self)


def grid(delays: Sequence[int] = DELAYS_S) -> list[Variant]:
    return [Variant(d, tp, sl, ta, td, h, inf)
            for d, tp, sl, (ta, td), h, inf in product(delays, TAKE_PROFITS, STOP_LOSSES, TRAILS, HOLDS_MIN, INFLOW_EXIT)]


GRID = grid()
EXIT_GRID = [v for v in GRID if v.delay_s == DELAYS_S[0]]  # signal mode / evaluated trades: no delay choice


def neighbors(variants: Sequence[Variant], i: int) -> list[int]:
    """Variants one grid step away from variants[i] in exactly one parameter (parameter jitter)."""
    v = variants[i]
    axes = {"delay_s": DELAYS_S, "take_profit": TAKE_PROFITS, "stop_loss": STOP_LOSSES,
            "trail": TRAILS, "max_hold_min": HOLDS_MIN, "inflow_exit": INFLOW_EXIT}

    def val(x: Variant, k: str):
        return (x.trail_activate, x.trail_drawdown) if k == "trail" else getattr(x, k)

    out = []
    for j, w in enumerate(variants):
        diff = [k for k in axes if val(w, k) != val(v, k)]
        if len(diff) == 1:
            k = diff[0]
            seq = list(axes[k])
            if val(v, k) in seq and val(w, k) in seq and abs(seq.index(val(v, k)) - seq.index(val(w, k))) == 1:
                out.append(j)
    return out


def nearest(params: dict, variants: Sequence[Variant] = GRID) -> int:
    """Index of the grid variant closest to a parameter dict (for "what the current settings do")."""
    def dist(v: Variant) -> float:
        return (abs(v.delay_s - params.get("delay_s", DELAYS_S[0])) / 60
                + abs(v.take_profit - params.get("take_profit", 0.4)) * 2
                + abs(v.stop_loss - params.get("stop_loss", 0.2)) * 4
                + abs(v.trail_activate - params.get("trail_activate", 0.15))
                + abs(v.trail_drawdown - params.get("trail_drawdown", 0.08)) * 2
                + abs(v.max_hold_min - params.get("max_hold_min", 120)) / 60
                + abs(v.inflow_exit - params.get("inflow_exit", 1)) * 0.5)
    return min(range(len(variants)), key=lambda i: dist(variants[i]))


def _idx(candles: Sequence, ts: float) -> int:
    for i, c in enumerate(candles):
        if c[0] + 60 > ts:
            return i
    return len(candles)


def simulate(candles: Sequence, entry_ts: float, v: Variant, exit_events: Sequence[float] = (),
             cost: float = ROUNDTRIP_COST) -> tuple[float, float, str] | None:
    """Net gain, minutes held and exit reason for one variant. None if no data after entry.

    Entry at the close of the candle containing entry_ts + delay. Within a candle the stop is
    checked before the target (pessimistic). exit_events: unix times of a forced exit (whale sold).
    """
    i0 = _idx(candles, entry_ts + v.delay_s)
    if i0 >= len(candles) - 1:
        return None
    entry = candles[i0][4]
    if entry <= 0:
        return None
    t0 = candles[i0][0]
    high = entry
    vol5: list[float] = []
    peak5 = 0.0
    forced = min((e for e in exit_events if e > t0), default=None)
    for ts, o, h, l, c, vol in candles[i0 + 1:]:
        mins = (ts - t0) / 60
        trail_on = v.trail_activate > 0 and high / entry - 1 >= v.trail_activate
        stop_px = max(entry * (1 - v.stop_loss), high * (1 - v.trail_drawdown) if trail_on else 0)
        if l <= stop_px:  # a candle that opens below the stop (rug, gap down) fills at the open, not at the stop
            return min(o, stop_px) / entry - 1 - cost, mins, "trail" if trail_on and stop_px > entry * (1 - v.stop_loss) else "stop"
        if h >= entry * (1 + v.take_profit):
            return v.take_profit - cost, mins, "target"
        high = max(high, h)
        vol5.append(vol)
        s5 = sum(vol5[-5:])
        peak5 = max(peak5, s5)
        if (v.inflow_exit and len(vol5) >= 5 and c / entry - 1 > 0.05 and peak5 > 0
                and s5 < 0.6 * peak5):
            return c / entry - 1 - cost, mins, "inflow_peak"
        if forced is not None and ts >= forced:
            return c / entry - 1 - cost, mins, "whale"
        if mins >= v.max_hold_min:
            return c / entry - 1 - cost, mins, "time"
    last = candles[-1]
    return last[4] / entry - 1 - cost, (last[0] - t0) / 60, "end"


def grid_gains(candles: Sequence, entry_ts: float, variants: Sequence[Variant] = GRID,
               exit_events: Sequence[float] = ()) -> list[float | None]:
    out: list[float | None] = []
    for v in variants:
        r = simulate(candles, entry_ts, v, exit_events)
        out.append(round(r[0], 4) if r else None)
    return out
