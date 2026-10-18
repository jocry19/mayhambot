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
    # coverage gate: essential checks (authorities, sell sim) and this share of core checks must answer
    min_coverage: float = field(default_factory=lambda: _f("SAFETY_MIN_COVERAGE", 0.6))
    block_unchecked: bool = field(default_factory=lambda: _b("SAFETY_BLOCK_UNCHECKED", True))
    carry_max_age_sec: int = field(default_factory=lambda: _i("SAFETY_CARRY_MAX_AGE_SEC", 7200))
    unchecked_penalty_factor: float = field(default_factory=lambda: _f("SAFETY_UNCHECKED_PENALTY_FACTOR", 0.5))

    # holders (percent of supply, pools/curves/burn excluded)
    top10_warn_pct: float = field(default_factory=lambda: _f("SAFETY_TOP10_WARN_PCT", 30))
    top10_crit_pct: float = field(default_factory=lambda: _f("SAFETY_TOP10_CRIT_PCT", 60))
    top1_warn_pct: float = field(default_factory=lambda: _f("SAFETY_TOP1_WARN_PCT", 8))
    top1_crit_pct: float = field(default_factory=lambda: _f("SAFETY_TOP1_CRIT_PCT", 20))
    holders_ttl_sec: int = field(default_factory=lambda: _i("SAFETY_HOLDERS_TTL_SEC", 600))
    holders_ttl_hot_sec: int = field(default_factory=lambda: _i("SAFETY_HOLDERS_TTL_HOT_SEC", 120))
    # getTokenLargestAccounts across all processes, per endpoint (public nodes allow little of it)
    largest_per_min: float = field(default_factory=lambda: _f("SAFETY_LARGEST_PER_MIN", 30))
    largest_spacing_sec: float = field(default_factory=lambda: _f("SAFETY_LARGEST_SPACING_SEC", 1.7))
    holders_profiled: int = field(default_factory=lambda: _i("SAFETY_HOLDERS_PROFILED", 6))
    fresh_wallet_days: float = field(default_factory=lambda: _f("SAFETY_FRESH_WALLET_DAYS", 3))
    fresh_wallet_max_txs: int = field(default_factory=lambda: _i("SAFETY_FRESH_WALLET_MAX_TXS", 25))
    insider_warn_pct: float = field(default_factory=lambda: _f("SAFETY_INSIDER_WARN_PCT", 10))
    insider_crit_pct: float = field(default_factory=lambda: _f("SAFETY_INSIDER_CRIT_PCT", 30))

    # creator / dev wallet
    dev_hold_warn_pct: float = field(default_factory=lambda: _f("SAFETY_DEV_HOLD_WARN_PCT", 5))
    dev_hold_crit_pct: float = field(default_factory=lambda: _f("SAFETY_DEV_HOLD_CRIT_PCT", 15))
    dev_sold_warn_pct: float = field(default_factory=lambda: _f("SAFETY_DEV_SOLD_WARN_PCT", 80))
    dev_txs_scanned: int = field(default_factory=lambda: _i("SAFETY_DEV_TXS_SCANNED", 4))

    # launch bundles (buys in the first slots after creation)
    bundle_slots: int = field(default_factory=lambda: _i("SAFETY_BUNDLE_SLOTS", 3))
    bundle_warn_pct: float = field(default_factory=lambda: _f("SAFETY_BUNDLE_WARN_PCT", 15))
    bundle_crit_pct: float = field(default_factory=lambda: _f("SAFETY_BUNDLE_CRIT_PCT", 35))
    launch_max_pages: int = field(default_factory=lambda: _i("SAFETY_LAUNCH_MAX_PAGES", 8))
    launch_max_txs: int = field(default_factory=lambda: _i("SAFETY_LAUNCH_MAX_TXS", 12))

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

    # soft rug in progress (intel features, no network)
    exit_max_age_sec: int = field(default_factory=lambda: _i("SAFETY_EXIT_MAX_AGE_SEC", 900))
    history_fresh_sec: int = field(default_factory=lambda: _i("SAFETY_HISTORY_FRESH_SEC", 300))
    history_max_age_sec: int = field(default_factory=lambda: _i("SAFETY_HISTORY_MAX_AGE_SEC", 21600))
    history_min_funder_launches: int = field(default_factory=lambda: _i("SAFETY_HISTORY_MIN_FUNDER_LAUNCHES", 3))
    history_bot_share_warn: float = field(default_factory=lambda: _f("SAFETY_HISTORY_BOT_SHARE_WARN", 0.4))
    exit_liq_drop_warn: float = field(default_factory=lambda: _f("SAFETY_EXIT_LIQ_DROP_WARN", 0.3))
    exit_insider_sell_warn: float = field(default_factory=lambda: _f("SAFETY_EXIT_INSIDER_SELL_WARN", 0.05))
    deep_young_h: float = field(default_factory=lambda: _f("SAFETY_DEEP_YOUNG_H", 3))   # one deep check per young coin
    # wash trading (from the pool trades signals/ already stores)
    wash_window_min: int = field(default_factory=lambda: _i("SAFETY_WASH_WINDOW_MIN", 120))
    wash_min_trades: int = field(default_factory=lambda: _i("SAFETY_WASH_MIN_TRADES", 20))
    wash_roundtrip_warn: float = field(default_factory=lambda: _f("SAFETY_WASH_ROUNDTRIP_WARN", 0.25))
    wash_block_roundtrip: float = field(default_factory=lambda: _f("SAFETY_WASH_BLOCK_ROUNDTRIP", 0.5))   # hard flag
    wash_top3_warn: float = field(default_factory=lambda: _f("SAFETY_WASH_TOP3_WARN", 0.4))

    # external keyless cross-check
    # creator-controlled launch signature (check block0)
    block0_window_sec: float = field(default_factory=lambda: _f("SAFETY_BLOCK0_WINDOW_SEC", 5))
    block0_max_sol: float = field(default_factory=lambda: _f("SAFETY_BLOCK0_MAX_SOL", 0.03))
    block0_min_wallets: int = field(default_factory=lambda: _i("SAFETY_BLOCK0_MIN_WALLETS", 3))
    block0_max_first_seen_sec: int = field(default_factory=lambda: _i("SAFETY_BLOCK0_MAX_FIRST_SEEN_SEC", 300))
    # staged pump: big market cap or migration within minutes of the launch (check staged)
    staged_window_sec: int = field(default_factory=lambda: _i("SAFETY_STAGED_WINDOW_SEC", 1800))
    staged_confirm_sec: int = field(default_factory=lambda: _i("SAFETY_STAGED_CONFIRM_SEC", 900))
    staged_min_mcap: float = field(default_factory=lambda: _f("SAFETY_STAGED_MIN_MCAP", 250000))
    staged_min_liq: float = field(default_factory=lambda: _f("SAFETY_STAGED_MIN_LIQ", 20000))
    staged_min_snapshots: int = field(default_factory=lambda: _i("SAFETY_STAGED_MIN_SNAPSHOTS", 3))
    staged_band_lo: float = field(default_factory=lambda: _f("SAFETY_STAGED_BAND_LO", 0.10))
    staged_band_hi: float = field(default_factory=lambda: _f("SAFETY_STAGED_BAND_HI", 0.20))
    staged_max_mcap: float = field(default_factory=lambda: _f("SAFETY_STAGED_MAX_MCAP", 1500000))
    # brand scam: creator buys the whole curve at create (check masche), holders spread by the creator (holders)
    masche_dev_buy_pct: float = field(default_factory=lambda: _f("SAFETY_MASCHE_DEV_BUY_PCT", 75))
    masche_spread_dev_pct: float = field(default_factory=lambda: _f("SAFETY_MASCHE_SPREAD_DEV_PCT", 50))
    # creator plus same-slot buyers at launch (share of supply): the curve bought by a second wallet
    masche_launch_share_pct: float = field(default_factory=lambda: _f("SAFETY_MASCHE_LAUNCH_SHARE_PCT", 70))
    masche_verify_max_age_sec: int = field(default_factory=lambda: _i("SAFETY_MASCHE_VERIFY_MAX_AGE_SEC", 21600))
    masche_setup_sol: float = field(default_factory=lambda: _f("SAFETY_MASCHE_SETUP_SOL", 100))
    rugged_block_sec: int = field(default_factory=lambda: _i("SAFETY_RUGGED_BLOCK_SEC", 86400))
    bad_wallets_file: str = field(default_factory=lambda: os.getenv("SAFETY_BAD_WALLETS_FILE", ""))  # "" = safety/bad_wallets.txt
    use_rugcheck: bool = field(default_factory=lambda: _b("SAFETY_USE_RUGCHECK", True))

    # cost control
    max_calls_per_token: int = field(default_factory=lambda: _i("SAFETY_MAX_CALLS_PER_TOKEN", 30))
    ttl_young_sec: int = field(default_factory=lambda: _i("SAFETY_TTL_YOUNG_SEC", 900))     # pair < 6 h old
    ttl_old_sec: int = field(default_factory=lambda: _i("SAFETY_TTL_OLD_SEC", 7200))
    ttl_blocked_sec: int = field(default_factory=lambda: _i("SAFETY_TTL_BLOCKED_SEC", 21600))
    wallet_profile_ttl_sec: int = field(default_factory=lambda: _i("SAFETY_WALLET_PROFILE_TTL_SEC", 86400))
    # fixed RPC budget shared by every process calling safety (token bucket in data/ratelimit/)
    # 60 / 0.5 s since 18.10. (user: Helius as needed): brain's learning account runs several hundred rug checks an
    # hour through this bucket; at 15/min they spilled to the public nodes (getTokenLargestAccounts 429 runs)
    helius_per_min: float = field(default_factory=lambda: _f("SAFETY_HELIUS_PER_MIN", 60))
    helius_spacing_sec: float = field(default_factory=lambda: _f("SAFETY_HELIUS_SPACING_SEC", 0.5))
    helius_overflow_wait_sec: float = field(default_factory=lambda: _f("SAFETY_HELIUS_OVERFLOW_WAIT_SEC", 2.0))
    helius_reserve: float = field(default_factory=lambda: _f("SAFETY_HELIUS_RESERVE", 4))   # kept for live buys/signals
    public_rpc_url: str = field(default_factory=lambda: os.getenv("SAFETY_PUBLIC_RPC_URL") or "https://api.mainnet-beta.solana.com")
    public_per_min: float = field(default_factory=lambda: _f("SAFETY_PUBLIC_PER_MIN", 20))  # 0 = no fallback
    public_spacing_sec: float = field(default_factory=lambda: _f("SAFETY_PUBLIC_SPACING_SEC", 1.0))
    priority_wait_sec: float = field(default_factory=lambda: _f("SAFETY_PRIORITY_WAIT_SEC", 10))
    # deep checks (insiders, launch bundles, dev wallet) only for coins that matter right now
    deep_every_sec: int = field(default_factory=lambda: _i("SAFETY_DEEP_EVERY_SEC", 1800))
    deep_signal_window_sec: int = field(default_factory=lambda: _i("SAFETY_DEEP_SIGNAL_WINDOW_SEC", 7200))
    deep_on_top10_change_pp: float = field(default_factory=lambda: _f("SAFETY_DEEP_ON_TOP10_CHANGE_PP", 10))
    launch_max_age_h: float = field(default_factory=lambda: _f("SAFETY_LAUNCH_MAX_AGE_H", 6))
    # learning from outcomes (safety/learn.py)
    learn_every_sec: int = field(default_factory=lambda: _i("SAFETY_LEARN_EVERY_SEC", 3600))
    outcome_horizon_h: float = field(default_factory=lambda: _f("SAFETY_OUTCOME_HORIZON_H", 24))
    rug_drop_pct: float = field(default_factory=lambda: _f("SAFETY_RUG_DROP_PCT", 90))
    learn_flag_risk: float = field(default_factory=lambda: _f("SAFETY_LEARN_FLAG_RISK", 25))
    learn_min_flagged: int = field(default_factory=lambda: _i("SAFETY_LEARN_MIN_FLAGGED", 15))
    learn_shrink_n: float = field(default_factory=lambda: _f("SAFETY_LEARN_SHRINK_N", 30))
    learn_weight_min: float = field(default_factory=lambda: _f("SAFETY_LEARN_WEIGHT_MIN", 0.5))
    learn_weight_max: float = field(default_factory=lambda: _f("SAFETY_LEARN_WEIGHT_MAX", 1.5))
    refresh_budget_sec: int = field(default_factory=lambda: _i("SAFETY_REFRESH_BUDGET_SEC", 30))
    refresh_every_sec: int = field(default_factory=lambda: _i("SAFETY_REFRESH_EVERY_SEC", 60))
    refresh_max_tokens: int = field(default_factory=lambda: _i("SAFETY_REFRESH_MAX_TOKENS", 8))
    # pre-check coins tracked wallets are buying (copy candidates) so buys find a result
    active_window_sec: int = field(default_factory=lambda: _i("SAFETY_ACTIVE_WINDOW_SEC", 1800))
    active_min_wallets: int = field(default_factory=lambda: _i("SAFETY_ACTIVE_MIN_WALLETS", 2))
    active_max: int = field(default_factory=lambda: _i("SAFETY_ACTIVE_MAX", 30))
    exchanges_file: str = field(default_factory=lambda: os.getenv("SAFETY_EXCHANGES_FILE") or str(HERE / "exchanges.txt"))


def load() -> SafetySettings:
    return SafetySettings()


settings = load()
