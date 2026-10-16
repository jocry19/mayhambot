"""Real-money path for mayhambot: guarded execution, hard risk limits, kill switch, go-live gate (README.md).

The copy trader (copytrade/execution.make_executors) asks `make_executor` for its live executor. Without
LIVE_TRADING=true nothing here trades; with it, LIVE_DRY_RUN (default on) builds, signs and simulates every swap
but never sends one.
"""
from .executor import GuardedLiveExecutor, make_executor  # noqa: F401
