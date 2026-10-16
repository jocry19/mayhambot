"""Settings of the market analysis. Every value can be overridden in mayhambot/.env (MARKET_*)."""
import os
from dataclasses import dataclass, field

import core.config  # noqa: F401  (loads mayhambot/.env into the environment)


def _f(name, default):
    v = os.getenv(name)
    try:
        return float(v) if v not in (None, "") else float(default)
    except ValueError:
        return float(default)


def _i(name, default):
    return int(_f(name, default))


def _b(name, default):
    v = os.getenv(name)
    return default if v in (None, "") else v.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class MarketSettings:
    # ---- loop
    every_sec: int = field(default_factory=lambda: _i("MARKET_EVERY_SEC", 300))
    keep_days: int = field(default_factory=lambda: _i("MARKET_KEEP_DAYS", 60))
    universe_max: int = field(default_factory=lambda: _i("MARKET_UNIVERSE_MAX", 300))   # tokens refreshed per round
    universe_hours: float = field(default_factory=lambda: _f("MARKET_UNIVERSE_HOURS", 24))
    # ---- external sources (all keyless). 0 disables a source.
    use_dexscreener: bool = field(default_factory=lambda: _b("MARKET_USE_DEXSCREENER", True))
    use_coingecko: bool = field(default_factory=lambda: _b("MARKET_USE_COINGECKO", True))
    coingecko_every_sec: int = field(default_factory=lambda: _i("MARKET_COINGECKO_EVERY_SEC", 900))
    boosts_every_sec: int = field(default_factory=lambda: _i("MARKET_BOOSTS_EVERY_SEC", 600))
    # this service's own per-minute budgets (core.http), well below the shared limits of the hosts
    dexscreener_per_min: int = field(default_factory=lambda: _i("MARKET_DEXSCREENER_PER_MIN", 20))
    coingecko_per_min: int = field(default_factory=lambda: _i("MARKET_COINGECKO_PER_MIN", 4))
    # ---- phase
    riskoff_below: float = field(default_factory=lambda: _f("MARKET_RISKOFF_BELOW", 35))
    riskon_above: float = field(default_factory=lambda: _f("MARKET_RISKON_ABOVE", 55))
    hot_above: float = field(default_factory=lambda: _f("MARKET_HOT_ABOVE", 75))
    # ---- narratives
    term_recent_hours: float = field(default_factory=lambda: _f("MARKET_TERM_RECENT_HOURS", 3))
    term_base_hours: float = field(default_factory=lambda: _f("MARKET_TERM_BASE_HOURS", 24))
    term_min_count: int = field(default_factory=lambda: _i("MARKET_TERM_MIN_COUNT", 8))
    terms_kept: int = field(default_factory=lambda: _i("MARKET_TERMS_KEPT", 40))
    # ---- consumers (off until measured: market.evaluate shows whether they help)
    signal_weight: bool = field(default_factory=lambda: _b("MARKET_SIGNAL_WEIGHT", False))
    signal_max_adj: float = field(default_factory=lambda: _f("MARKET_SIGNAL_MAX_ADJ", 0.15))  # score x (1 +- this)
    sniper_gate: bool = field(default_factory=lambda: _b("MARKET_SNIPER_GATE", False))
    sniper_riskoff_bump: float = field(default_factory=lambda: _f("MARKET_SNIPER_RISKOFF_BUMP", 10))  # + min score
    sniper_hot_term_bonus: float = field(default_factory=lambda: _f("MARKET_SNIPER_HOT_TERM_BONUS", 5))  # - min score
    # a launch riding a brand-new narrative (stage 'new'): + min score; own switch, walk-forward decides
    sniper_new_term: bool = field(default_factory=lambda: _b("MARKET_SNIPER_NEW_TERM", False))
    sniper_new_term_bump: float = field(default_factory=lambda: _f("MARKET_SNIPER_NEW_TERM_BUMP", 5))
    learned_weights: bool = field(default_factory=lambda: _b("MARKET_LEARNED_WEIGHTS", False))  # adopt OOS-better weights
    dynamics_every_sec: int = field(default_factory=lambda: _i("MARKET_DYNAMICS_EVERY_SEC", 3600))
    max_age_sec: int = field(default_factory=lambda: _i("MARKET_MAX_AGE_SEC", 1800))  # older snapshot = no effect


def load() -> MarketSettings:
    return MarketSettings()


settings = load()
