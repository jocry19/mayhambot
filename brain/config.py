"""Settings of the decider (BRAIN_* in .env). Paper only: the brain never sends a real transaction."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _f(name: str, default: float) -> float:
    v = os.getenv(name)
    try:
        return float(v) if v not in (None, "") else float(default)
    except ValueError:
        return float(default)


def _s(name: str, default: str) -> str:
    return os.getenv(name, default) or default


@dataclass
class Settings:
    db_path: str = field(default_factory=lambda: _s("MAYHAM_DB_PATH", str(ROOT / "data" / "mayhambot.db")))
    start_balance_sol: float = field(default_factory=lambda: _f("BRAIN_START_SOL", 5.0))
    min_size_sol: float = field(default_factory=lambda: _f("BRAIN_MIN_SIZE_SOL", 0.1))
    max_size_sol: float = field(default_factory=lambda: _f("BRAIN_MAX_SIZE_SOL", 0.5))
    # convinced buys: a quarter of the Kelly stake of the current account (0 = the fixed min..max size above), at
    # most this share of the account in one trade; so the stake grows with the account
    kelly_frac: float = field(default_factory=lambda: _f("BRAIN_KELLY_FRAC", 0.25))
    max_stake: float = field(default_factory=lambda: _f("BRAIN_MAX_STAKE", 0.2))
    unproven_stake: float = field(default_factory=lambda: _f("BRAIN_UNPROVEN_STAKE", 0.03))  # see Trader.settings
    # the user's challenge (10/2026: x2 every hour, "komme was wolle"): bigger stakes, small exploration buys from the
    # side sources right away; switches itself off for 6 h when the last 3 hours lost more than challenge_stop
    challenge: str = field(default_factory=lambda: _s("BRAIN_CHALLENGE", "on").lower())
    challenge_kelly_frac: float = field(default_factory=lambda: _f("BRAIN_CHALLENGE_KELLY_FRAC", 0.5))
    challenge_max_stake: float = field(default_factory=lambda: _f("BRAIN_CHALLENGE_MAX_STAKE", 0.35))
    challenge_explore_share: float = field(default_factory=lambda: _f("BRAIN_CHALLENGE_EXPLORE_SHARE", 0.02))
    challenge_stop: float = field(default_factory=lambda: _f("BRAIN_CHALLENGE_STOP", 0.85))
    explore_share: float = 0.0  # exploration stake as a share of the account (0 = min_size_sol); set by the challenge
    max_open: int = field(default_factory=lambda: int(_f("BRAIN_MAX_OPEN", 100)))
    max_daily_loss_sol: float = field(default_factory=lambda: _f("BRAIN_MAX_DAILY_LOSS_SOL", 0))
    # trades whose expected value is still negative but uncertain enough to be worth a look (smallest size)
    explore_per_day: int = field(default_factory=lambda: int(_f("BRAIN_EXPLORE_PER_DAY", 0)))
    min_edge: float = field(default_factory=lambda: _f("BRAIN_MIN_EDGE", 0.0))
    min_conf: float = field(default_factory=lambda: _f("BRAIN_MIN_CONF", 0.75))  # P(mean > 0) for a real buy
    explore_min_p: float = field(default_factory=lambda: _f("BRAIN_EXPLORE_MIN_P", 0.25))
    # segments the data shows losing at entry (backup 17.10., 44 h, after costs): whale copies detected 30 s+ late
    # -11.2 % (n 22) vs -4 % under 30 s (n 69); bonding-curve coins -17 % (n 418) vs pumpswap -6.5 % (n 449).
    # No exploration buys there; a convinced buy still goes through, and the replay keeps learning every one.
    no_explore: str = field(default_factory=lambda: _s("BRAIN_NO_EXPLORE", "delay:30-90s,delay:>90s,liq:curve"))
    # round-trip costs on top of the simulator's 3 %: copy 5.5 % in total, sniper launches 9 %
    extra_cost_copy: float = field(default_factory=lambda: _f("BRAIN_EXTRA_COST_COPY", 0.025))
    extra_cost_launch: float = field(default_factory=lambda: _f("BRAIN_EXTRA_COST_LAUNCH", 0.06))
    # organic exits (brain/exits.py): sell the rest below this P(next 10 min > 0), half below exit_half_p when up
    exit_sell_p: float = field(default_factory=lambda: _f("BRAIN_EXIT_SELL_P", 0.35))
    exit_half_p: float = field(default_factory=lambda: _f("BRAIN_EXIT_HALF_P", 0.5))
    exit_half_min_up: float = field(default_factory=lambda: _f("BRAIN_EXIT_HALF_MIN_UP", 0.10))
    # auto = organic once its out-of-sample replays beat the plans; on = always (when trained); off = plans only
    organic_exit: str = field(default_factory=lambda: _s("BRAIN_ORGANIC_EXIT", "auto").lower())
    # side source (signals' shadow book): auto = traded once its replays earn it, on / off force it
    market_trade: str = field(default_factory=lambda: _s("BRAIN_MARKET_TRADE", "auto").lower())
    half_life_h: float = field(default_factory=lambda: _f("BRAIN_HALF_LIFE_H", 120.0))
    # what counts as an opportunity
    min_whale_sol: float = field(default_factory=lambda: _f("BRAIN_MIN_WHALE_SOL", 0.3))
    max_whale_delay_s: float = field(default_factory=lambda: _f("BRAIN_MAX_WHALE_DELAY_S", 300))
    max_signal_age_s: float = field(default_factory=lambda: _f("BRAIN_MAX_SIGNAL_AGE_S", 180))
    # the shared opportunities table (every source): its learning buys stake this much, the rest by Kelly
    pool_trade: str = field(default_factory=lambda: _s("BRAIN_POOL_TRADE", "on").lower())
    learn_size_sol: float = field(default_factory=lambda: _f("BRAIN_LEARN_SIZE_SOL", 0.03))
    max_pool_age_s: float = field(default_factory=lambda: _f("BRAIN_MAX_POOL_AGE_S", 180))
    max_launch_age_s: float = field(default_factory=lambda: _f("BRAIN_MAX_LAUNCH_AGE_S", 60))
    min_liquidity_usd: float = field(default_factory=lambda: _f("BRAIN_MIN_LIQ_USD", 3000))
    # pump.fun bonding curve: DexScreener reports no pool liquidity there, the curve itself is the market
    min_curve_mcap_usd: float = field(default_factory=lambda: _f("BRAIN_MIN_CURVE_MCAP_USD", 5000))
    # loop timing
    scan_every_s: float = field(default_factory=lambda: _f("BRAIN_SCAN_SECONDS", 2))
    price_every_s: float = field(default_factory=lambda: _f("BRAIN_PRICE_SECONDS", 5))
    learn_every_s: float = field(default_factory=lambda: _f("BRAIN_LEARN_SECONDS", 60))
    evals_per_round: int = field(default_factory=lambda: int(_f("BRAIN_EVALS_PER_ROUND", 12)))
    # Telegram: trades (buys, sells, diary) | diary | off
    telegram: str = field(default_factory=lambda: _s("BRAIN_TELEGRAM", "trades").lower())
    diary_hour_utc: int = field(default_factory=lambda: int(_f("BRAIN_DIARY_HOUR_UTC", 19)))
    enabled: bool = field(default_factory=lambda: _s("BRAIN_ENABLED", "true").lower() not in ("0", "false", "off"))

    @property
    def no_explore_set(self) -> set:
        return {x.strip() for x in self.no_explore.split(",") if x.strip()}

    @property
    def model_path(self) -> Path:
        return Path(self.db_path).parent / "brain" / "model.npz"

    @property
    def pool_model_path(self) -> Path:
        return Path(self.db_path).parent / "brain" / "pool_model.json"

    @property
    def exit_model_path(self) -> Path:
        return Path(self.db_path).parent / "brain" / "exit_model.npz"

    def extra_cost(self, kind: str) -> float:
        return self.extra_cost_launch if kind == "launch" else self.extra_cost_copy


def load() -> Settings:
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except ImportError:
        pass
    return Settings()
