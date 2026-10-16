"""Thresholds of the token safety layer. Every value can be overridden in mayhambot/.env (SAFETY_*)."""
import os
from dataclasses import dataclass, field
from pathlib import Path

import core.config  # noqa: F401  (loads mayhambot/.env into the environment)

HERE = Path(__file__).resolve().parent


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
class SafetySettings:
    # final score: 0 = no risk found, 100 = certain rug. level boundaries and actions
    caution_score: float = field(default_factory=lambda: _f("SAFETY_CAUTION_SCORE", 25))   # signal penalty starts
    high_score: float = field(default_factory=lambda: _f("SAFETY_HIGH_SCORE", 50))
    block_score: float = field(default_factory=lambda: _f("SAFETY_BLOCK_SCORE", 75))       # copy trader skips
    hard_flag_floor: float = field(default_factory=lambda: _f("SAFETY_HARD_FLAG_FLOOR", 90))
    min_penalty_factor: float = field(default_factory=lambda: _f("SAFETY_MIN_PENALTY_FACTOR", 0.2))

    # holders (percent of supply, pools/curves/burn excluded)
    top10_warn_pct: float = field(default_factory=lambda: _f("SAFETY_TOP10_WARN_PCT", 30))
    top10_crit_pct: float = field(default_factory=lambda: _f("SAFETY_TOP10_CRIT_PCT", 60))
    top1_warn_pct: float = field(default_factory=lambda: _f("SAFETY_TOP1_WARN_PCT", 8))
    top1_crit_pct: float = field(default_factory=lambda: _f("SAFETY_TOP1_CRIT_PCT", 20))
    holders_profiled: int = field(default_factory=lambda: _i("SAFETY_HOLDERS_PROFILED", 10))
    fresh_wallet_days: float = field(default_factory=lambda: _f("SAFETY_FRESH_WALLET_DAYS", 3))
    fresh_wallet_max_txs: int = field(default_factory=lambda: _i("SAFETY_FRESH_WALLET_MAX_TXS", 25))
    insider_warn_pct: float = field(default_factory=lambda: _f("SAFETY_INSIDER_WARN_PCT", 10))
    insider_crit_pct: float = field(default_factory=lambda: _f("SAFETY_INSIDER_CRIT_PCT", 30))

    # creator / dev wallet
    dev_hold_warn_pct: float = field(default_factory=lambda: _f("SAFETY_DEV_HOLD_WARN_PCT", 5))
    dev_hold_crit_pct: float = field(default_factory=lambda: _f("SAFETY_DEV_HOLD_CRIT_PCT", 15))
    dev_sold_warn_pct: float = field(default_factory=lambda: _f("SAFETY_DEV_SOLD_WARN_PCT", 80))
    dev_txs_scanned: int = field(default_factory=lambda: _i("SAFETY_DEV_TXS_SCANNED", 8))

    # launch bundles (buys in the first slots after creation)
    bundle_slots: int = field(default_factory=lambda: _i("SAFETY_BUNDLE_SLOTS", 3))
    bundle_warn_pct: float = field(default_factory=lambda: _f("SAFETY_BUNDLE_WARN_PCT", 15))
    bundle_crit_pct: float = field(default_factory=lambda: _f("SAFETY_BUNDLE_CRIT_PCT", 35))
    launch_max_pages: int = field(default_factory=lambda: _i("SAFETY_LAUNCH_MAX_PAGES", 15))
    launch_max_txs: int = field(default_factory=lambda: _i("SAFETY_LAUNCH_MAX_TXS", 25))

    # sell simulation (Jupiter round trip SOL -> token -> SOL)
    sim_size_sol: float = field(default_factory=lambda: _f("SAFETY_SIM_SIZE_SOL", 0.05))
    fee_allowance_pct: float = field(default_factory=lambda: _f("SAFETY_FEE_ALLOWANCE_PCT", 3))
    tax_warn_pct: float = field(default_factory=lambda: _f("SAFETY_TAX_WARN_PCT", 5))
    tax_crit_pct: float = field(default_factory=lambda: _f("SAFETY_TAX_CRIT_PCT", 15))
    jupiter_url: str = field(default_factory=lambda: (os.getenv("JUPITER_API_URL") or "https://lite-api.jup.ag").rstrip("/"))
    jupiter_api_key: str = field(default_factory=lambda: os.getenv("JUPITER_API_KEY", ""))

    # liquidity and LP
    min_liquidity_usd: float = field(default_factory=lambda: _f("SAFETY_MIN_LIQUIDITY_USD", 5000))
    min_liq_mcap_ratio: float = field(default_factory=lambda: _f("SAFETY_MIN_LIQ_MCAP_RATIO", 0.03))
    liq_drop_pct: float = field(default_factory=lambda: _f("SAFETY_LIQ_DROP_PCT", 90))   # vs 7-day peak -> hard flag
    lp_locked_ok_pct: float = field(default_factory=lambda: _f("SAFETY_LP_LOCKED_OK_PCT", 90))

    # wash trading (from the pool trades signals/ already stores)
    wash_window_min: int = field(default_factory=lambda: _i("SAFETY_WASH_WINDOW_MIN", 120))
    wash_min_trades: int = field(default_factory=lambda: _i("SAFETY_WASH_MIN_TRADES", 20))
    wash_roundtrip_warn: float = field(default_factory=lambda: _f("SAFETY_WASH_ROUNDTRIP_WARN", 0.35))
    wash_top3_warn: float = field(default_factory=lambda: _f("SAFETY_WASH_TOP3_WARN", 0.5))

    # external keyless cross-check
    use_rugcheck: bool = field(default_factory=lambda: _b("SAFETY_USE_RUGCHECK", True))

    # cost control
    max_calls_per_token: int = field(default_factory=lambda: _i("SAFETY_MAX_CALLS_PER_TOKEN", 70))
    ttl_young_sec: int = field(default_factory=lambda: _i("SAFETY_TTL_YOUNG_SEC", 900))     # pair < 6 h old
    ttl_old_sec: int = field(default_factory=lambda: _i("SAFETY_TTL_OLD_SEC", 7200))
    ttl_blocked_sec: int = field(default_factory=lambda: _i("SAFETY_TTL_BLOCKED_SEC", 21600))
    wallet_profile_ttl_sec: int = field(default_factory=lambda: _i("SAFETY_WALLET_PROFILE_TTL_SEC", 86400))
    refresh_budget_sec: int = field(default_factory=lambda: _i("SAFETY_REFRESH_BUDGET_SEC", 30))
    refresh_every_sec: int = field(default_factory=lambda: _i("SAFETY_REFRESH_EVERY_SEC", 180))
    refresh_max_tokens: int = field(default_factory=lambda: _i("SAFETY_REFRESH_MAX_TOKENS", 8))
    exchanges_file: str = field(default_factory=lambda: os.getenv("SAFETY_EXCHANGES_FILE") or str(HERE / "exchanges.txt"))


def load() -> SafetySettings:
    return SafetySettings()


settings = load()
