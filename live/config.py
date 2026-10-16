"""Live settings from mayhambot/.env. Every limit here is a hard ceiling independent of strategy presets:
a preset can trade smaller, never bigger. LIVE_STAGE picks the ceiling set; a .env value can only tighten it."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env", override=False)
except Exception:  # python-dotenv is optional
    pass

TRUE = {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Stage:
    name: str
    max_position_sol: float
    max_open: int
    max_daily_loss_sol: float
    max_exposure_sol: float  # sum of entry costs of open live positions


# Stage 1 sized so fixed costs stay small: 2 x priority fee (<= 0.0003 SOL) + 2 x 5000 lamports on 0.1 SOL is
# about 0.6 % per round trip, on top of DEX fee and slippage. Smaller sizes would be eaten by fixed costs.
STAGES = {
    1: Stage("Stufe 1 · Kleinstbeträge", 0.1, 2, 0.3, 0.2),
    2: Stage("Stufe 2 · klein", 0.25, 3, 0.75, 0.75),
    3: Stage("Stufe 3 · normal", 0.5, 5, 1.5, 2.5),
}

# what a stage needs before the next one: live round trips, net live PnL > 0, cost parity with paper
ADVANCE = {1: {"min_trips": 20}, 2: {"min_trips": 40}}


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


def _opt(name: str) -> float | None:
    v = os.getenv(name, "").strip()
    try:
        return float(v) if v else None
    except ValueError:
        return None


def _b(name: str, default: bool) -> bool:
    v = os.getenv(name)
    return default if v is None else v.strip().lower() in TRUE


def _tight(stage_value: float, env: float | None) -> float:
    return stage_value if env is None or env <= 0 else min(stage_value, env)


@dataclass
class LiveConfig:
    enabled: bool = field(default_factory=lambda: _b("LIVE_TRADING", False))
    # default ON: the first LIVE_TRADING=true only rehearses; real sends need LIVE_DRY_RUN=false by hand
    dry_run: bool = field(default_factory=lambda: _b("LIVE_DRY_RUN", True))
    stage_no: int = field(default_factory=lambda: int(_f("LIVE_STAGE", 1)) if int(_f("LIVE_STAGE", 1)) in STAGES else 1)
    # real sends only once the go-live check (readiness.py) passes; false = you override it knowingly
    require_gate: bool = field(default_factory=lambda: _b("LIVE_REQUIRE_GATE", True))
    # a trading wallet holding more than this is refused: it looks like the main wallet, not a separate one
    max_wallet_sol: float = field(default_factory=lambda: _f("LIVE_MAX_WALLET_SOL", 3.0))
    reserve_sol: float = field(default_factory=lambda: _f("LIVE_RESERVE_SOL", 0.03))  # fees + token-account rent
    max_price_impact: float = field(default_factory=lambda: _f("LIVE_MAX_PRICE_IMPACT", 0.03))
    # Jupiter quote vs DexScreener price: more apart than this = one of the two is stale or wrong, no buy
    max_quote_deviation: float = field(default_factory=lambda: _f("LIVE_MAX_QUOTE_DEVIATION", 0.25))
    max_hidden_tax_pct: float = field(default_factory=lambda: _f("LIVE_MAX_HIDDEN_TAX_PCT", 3))
    max_wash_share: float = field(default_factory=lambda: _f("LIVE_MAX_WASH_SHARE", 0.2))
    max_snapshot_age_s: float = field(default_factory=lambda: _f("LIVE_MAX_SNAPSHOT_AGE_S", 90))
    buy_slippage_bps: int = field(default_factory=lambda: int(_f("LIVE_BUY_SLIPPAGE_BPS", 300)))
    # sells must get out (stop loss): retries double the slippage up to the max
    sell_slippage_bps: int = field(default_factory=lambda: int(_f("LIVE_SELL_SLIPPAGE_BPS", 500)))
    sell_slippage_max_bps: int = field(default_factory=lambda: int(_f("LIVE_SELL_SLIPPAGE_MAX_BPS", 2500)))
    sell_attempts: int = field(default_factory=lambda: int(_f("LIVE_SELL_ATTEMPTS", 3)))
    priority_fee_max_lamports: int = field(default_factory=lambda: int(_f("LIVE_PRIORITY_FEE_MAX_LAMPORTS", 300_000)))
    priority_level: str = field(default_factory=lambda: os.getenv("LIVE_PRIORITY_LEVEL", "veryHigh"))
    max_consecutive_fails: int = field(default_factory=lambda: int(_f("LIVE_MAX_CONSECUTIVE_FAILS", 3)))
    confirm_timeout_s: float = field(default_factory=lambda: _f("LIVE_CONFIRM_TIMEOUT_S", 90))
    dry_start_sol: float = field(default_factory=lambda: _f("LIVE_DRY_START_SOL", 1.0))
    wallet_pubkey: str = field(default_factory=lambda: os.getenv("LIVE_WALLET_PUBKEY", "").strip())
    keychain_service: str = field(default_factory=lambda: os.getenv("LIVE_KEYCHAIN_SERVICE", "").strip())
    # go-live gate on paper results (per strategy with live switched on)
    gate_days: float = field(default_factory=lambda: _f("LIVE_GATE_DAYS", 7))
    gate_min_trades: int = field(default_factory=lambda: int(_f("LIVE_GATE_MIN_TRADES", 30)))
    gate_min_profit_factor: float = field(default_factory=lambda: _f("LIVE_GATE_MIN_PF", 1.2))
    gate_min_dry_buys: int = field(default_factory=lambda: int(_f("LIVE_GATE_MIN_DRY_BUYS", 5)))
    gate_min_dry_sells: int = field(default_factory=lambda: int(_f("LIVE_GATE_MIN_DRY_SELLS", 3)))
    # env overrides that may only tighten the stage
    _pos: float | None = field(default_factory=lambda: _opt("LIVE_MAX_POSITION_SOL"), repr=False)
    _open: float | None = field(default_factory=lambda: _opt("LIVE_MAX_OPEN"), repr=False)
    _loss: float | None = field(default_factory=lambda: _opt("LIVE_MAX_DAILY_LOSS_SOL"), repr=False)
    _expo: float | None = field(default_factory=lambda: _opt("LIVE_MAX_EXPOSURE_SOL"), repr=False)

    @property
    def stage(self) -> Stage:
        return STAGES[self.stage_no]

    @property
    def max_position_sol(self) -> float:
        return _tight(self.stage.max_position_sol, self._pos)

    @property
    def max_open(self) -> int:
        return int(_tight(self.stage.max_open, self._open))

    @property
    def max_daily_loss_sol(self) -> float:
        return _tight(self.stage.max_daily_loss_sol, self._loss)

    @property
    def max_exposure_sol(self) -> float:
        return _tight(self.stage.max_exposure_sol, self._expo)

    def limits(self) -> dict:
        return {"stage": self.stage_no, "stage_name": self.stage.name, "max_position_sol": self.max_position_sol,
                "max_open": self.max_open, "max_daily_loss_sol": self.max_daily_loss_sol,
                "max_exposure_sol": self.max_exposure_sol, "reserve_sol": self.reserve_sol,
                "max_wallet_sol": self.max_wallet_sol, "max_price_impact": self.max_price_impact,
                "buy_slippage_bps": self.buy_slippage_bps, "sell_slippage_bps": self.sell_slippage_bps,
                "priority_fee_max_sol": self.priority_fee_max_lamports / 1e9}


def load() -> LiveConfig:
    return LiveConfig()
