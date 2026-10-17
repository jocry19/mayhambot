"""Exit plans the brain chooses between, as part of every entry decision. Each plan is a point of copytrade's
simulator grid (sim.GRID), so the replays copytrade already stored for old whale buys and signals (ct_evals) are
prior knowledge for exactly these plans."""
from __future__ import annotations

from dataclasses import dataclass

from copytrade import sim


@dataclass(frozen=True)
class Plan:
    name: str
    label: str
    take_profit: float
    stop_loss: float
    trail_activate: float
    trail_drawdown: float
    max_hold_min: float
    inflow_exit: int

    def variant(self, delay_s: int = 0) -> sim.Variant:
        return sim.Variant(delay_s, self.take_profit, self.stop_loss, self.trail_activate, self.trail_drawdown,
                           self.max_hold_min, self.inflow_exit)

    def text(self) -> str:
        t = f"Ziel +{self.take_profit:.0%}, Stop −{self.stop_loss:.0%}"
        if self.trail_activate:
            t += f", Trailing ab +{self.trail_activate:.0%}"
        t += f", max. {self.max_hold_min:.0f} min"
        if self.inflow_exit:
            t += ", raus wenn der Ansturm abflaut"
        return t


PLANS = (
    Plan("scalp", "Schnell rein und raus", 0.15, 0.10, 0.0, 0.0, 15, 0),
    Plan("quick", "Erster Pump", 0.25, 0.10, 0.0, 0.0, 15, 1),
    Plan("pump", "Pump mitnehmen", 0.40, 0.20, 0.15, 0.08, 45, 1),
    Plan("swing", "Mittlerer Lauf", 0.60, 0.20, 0.15, 0.08, 120, 1),
    Plan("runner", "Laufen lassen", 1.00, 0.20, 0.30, 0.15, 120, 0),
    Plan("moon", "Mondschuss", 1.50, 0.30, 0.30, 0.15, 240, 0),
)
BASE = "pump"  # the reference plan for "what a plain copy of everything would have made"
NAMES = tuple(p.name for p in PLANS)
BY_NAME = {p.name: p for p in PLANS}


def nearest_delay(delay_s: float) -> int:
    """copytrade's grid delay closest to the brain's own entry delay after the whale's buy."""
    return min(sim.DELAYS_S, key=lambda d: abs(d - delay_s))


def grid_index(plan: Plan, grid=None, delay_s: int | None = None) -> int:
    """Index of the plan in copytrade's grid (whale buys: GRID at the given delay, default 15 s; signals/trades:
    EXIT_GRID)."""
    grid = sim.GRID if grid is None else grid
    return grid.index(plan.variant(sim.DELAYS_S[0] if delay_s is None else delay_s))


def gains_from_grid(gains: list, grid, delay_s: int | None = None) -> list:
    """The plans' gains out of a stored full-grid replay (None where the grid row has none)."""
    if len(gains) != len(grid):
        return [None] * len(PLANS)
    return [gains[grid_index(p, grid, delay_s)] for p in PLANS]
