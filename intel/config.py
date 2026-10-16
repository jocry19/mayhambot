"""Settings of the Zentrale (intel/). Every value can be overridden in mayhambot/.env (INTEL_*)."""
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


@dataclass
class IntelSettings:
    # ---- loop (no network: everything comes from the shared DB)
    every_sec: int = field(default_factory=lambda: _i("INTEL_EVERY_SEC", 60))            # events + ticker
    coins_every_sec: int = field(default_factory=lambda: _i("INTEL_COINS_EVERY_SEC", 300))  # coin features (live view)
    persist_every_sec: int = field(default_factory=lambda: _i("INTEL_PERSIST_EVERY_SEC", 900))  # history rows
    wallets_every_sec: int = field(default_factory=lambda: _i("INTEL_WALLETS_EVERY_SEC", 3600))
    label_every_sec: int = field(default_factory=lambda: _i("INTEL_LABEL_EVERY_SEC", 600))
    keep_days: int = field(default_factory=lambda: _i("INTEL_KEEP_DAYS", 21))
    universe_max: int = field(default_factory=lambda: _i("INTEL_UNIVERSE_MAX", 150))
    wallets_max: int = field(default_factory=lambda: _i("INTEL_WALLETS_MAX", 400))
    # ---- outcomes: same pump definition as model/ (max within 1 h >= +50 %)
    hit_ret: float = field(default_factory=lambda: _f("INTEL_HIT_RET", 0.5))
    # ---- events
    whale_usd: float = field(default_factory=lambda: _f("INTEL_WHALE_USD", 1000))       # one trade counts as whale
    ultra_buy_usd: float = field(default_factory=lambda: _f("INTEL_ULTRA_BUY_USD", 150))
    insider_sell_usd: float = field(default_factory=lambda: _f("INTEL_INSIDER_SELL_USD", 300))
    rotation_min_usd: float = field(default_factory=lambda: _f("INTEL_ROTATION_MIN_USD", 50))
    liq_drop_pct: float = field(default_factory=lambda: _f("INTEL_LIQ_DROP_PCT", 30))
    wash_warn: float = field(default_factory=lambda: _f("INTEL_WASH_WARN", 0.4))
    # ---- execution cost model (until stage 3 measures it with Jupiter quotes)
    dex_fee: float = field(default_factory=lambda: _f("INTEL_DEX_FEE", 0.0025))          # per side, AMM pools
    curve_fee: float = field(default_factory=lambda: _f("INTEL_CURVE_FEE", 0.0125))      # per side, pump.fun curve
    net_fee_sol: float = field(default_factory=lambda: _f("INTEL_NET_FEE_SOL", 0.0006))  # priority + base, per side


settings = IntelSettings()
