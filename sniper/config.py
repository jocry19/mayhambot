"""Settings of the launch sniper. Every value can be overridden in mayhambot/.env (SNIPER_*)."""
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


def _s(name, default):
    return (os.getenv(name) or default).strip()


@dataclass
class SniperSettings:
    # ---- sources
    pumpportal_url: str = field(default_factory=lambda: _s("SNIPER_PUMPPORTAL_URL", "wss://pumpportal.fun/api/data"))
    pumpportal_api_key: str = field(default_factory=lambda: os.getenv("SNIPER_PUMPPORTAL_API_KEY", "").strip())
    # per-coin trades: PumpPortal only with a funded key; without one the free RPC log stream is used
    trade_source: str = field(default_factory=lambda: _s("SNIPER_TRADE_SOURCE", "auto"))   # auto | pumpportal | rpc
    ws_url: str = field(default_factory=lambda: _s("SNIPER_WS_URL", "wss://api.mainnet-beta.solana.com"))
    # second program-wide trade stream on another public RPC: the first one drops its socket up to once a minute and
    # then misses the first seconds of launches; both feed the engine, duplicates are removed by signature. "" = off
    ws_url2: str = field(default_factory=lambda: _s("SNIPER_WS_URL2", "wss://solana-rpc.publicnode.com"))
    # bonk (Raydium LaunchLab): one program-wide stream of its trade events (price, side, size, fee; no trader) and
    # the trader of the first trades of each launch from getTransaction on bonk_rpc_url. "" = no bonk stream
    bonk_ws_url: str = field(default_factory=lambda: _s("SNIPER_BONK_WS_URL", "wss://solana-rpc.publicnode.com"))
    bonk_rpc_url: str = field(default_factory=lambda: _s("SNIPER_BONK_RPC_URL", "https://solana-rpc.publicnode.com"))
    bonk_wallets_per_launch: int = field(default_factory=lambda: _i("SNIPER_BONK_WALLETS_PER_LAUNCH", 30))
    bonk_wallets_per_min: int = field(default_factory=lambda: _i("SNIPER_BONK_WALLETS_PER_MIN", 60))
    # launches: PumpPortal and the RPC create stream run side by side; the first to deliver a mint wins (no gap
    # when one fails). both | pumpportal | rpc
    launch_sources: str = field(default_factory=lambda: _s("SNIPER_LAUNCH_SOURCES", "both"))
    create_mention: str = field(default_factory=lambda: _s("SNIPER_CREATE_MENTION", "TSLvdd1pWpHVjahSpsvCXUbgwsL3JAcvokwaKt1eokM"))
    stale_sec: int = field(default_factory=lambda: _i("SNIPER_STALE_SEC", 180))   # no launch that long -> reconnect
    ws_max_subs: int = field(default_factory=lambda: _i("SNIPER_WS_MAX_SUBS", 150))
    ws_subs_per_sec: float = field(default_factory=lambda: _f("SNIPER_WS_SUBS_PER_SEC", 4.0))   # paced subscribe requests
    rug_p_block: float = field(default_factory=lambda: _f("SNIPER_RUG_P_BLOCK", 0.59))   # safety's launch model
    use_learned_rule: bool = field(default_factory=lambda: _b("SNIPER_USE_LEARNED_RULE", True))  # paper only, if accepted
    research_hours: float = field(default_factory=lambda: _f("SNIPER_RESEARCH_HOURS", 72))       # window of the hourly run
    trade_sub_mode: str = field(default_factory=lambda: _s("SNIPER_TRADE_SUB_MODE", "auto"))  # auto | mints | program
    pump_api_url: str = field(default_factory=lambda: _s("SNIPER_PUMP_API_URL", "https://frontend-api-v3.pump.fun").rstrip("/"))
    use_pump_api: bool = field(default_factory=lambda: _b("SNIPER_USE_PUMP_API", False))  # v3 paths answer 400/404 (10/2026)
    use_geckoterminal: bool = field(default_factory=lambda: _b("SNIPER_USE_GECKOTERMINAL", True))
    gt_every_sec: int = field(default_factory=lambda: _i("SNIPER_GT_EVERY_SEC", 300))
    # public RPC by default: the Helius free plan (credits per month) stays for the other services
    rpc_url: str = field(default_factory=lambda: _s("SNIPER_RPC_URL", "https://api.mainnet-beta.solana.com"))
    jupiter_url: str = field(default_factory=lambda: (os.getenv("JUPITER_API_URL") or "https://lite-api.jup.ag").rstrip("/"))
    jupiter_api_key: str = field(default_factory=lambda: os.getenv("JUPITER_API_KEY", ""))

    # ---- call budgets (Helius free plan is the bottleneck of the whole system)
    rpc_per_min: int = field(default_factory=lambda: _i("SNIPER_RPC_PER_MIN", 10))
    max_rpc_per_launch: int = field(default_factory=lambda: _i("SNIPER_MAX_RPC_PER_LAUNCH", 5))
    pump_api_per_min: int = field(default_factory=lambda: _i("SNIPER_PUMP_API_PER_MIN", 40))
    deep_per_min: int = field(default_factory=lambda: _i("SNIPER_DEEP_PER_MIN", 4))         # launches checked in depth
    max_queue_sec: int = field(default_factory=lambda: _i("SNIPER_MAX_QUEUE_SEC", 45))      # older candidates are dropped
    dex_every_sec: int = field(default_factory=lambda: _i("SNIPER_DEX_EVERY_SEC", 30))

    # ---- watching
    decide_sec: int = field(default_factory=lambda: _i("SNIPER_DECIDE_SEC", 20))            # first look after creation
    recheck_sec: int = field(default_factory=lambda: _i("SNIPER_RECHECK_SEC", 75))          # second look without trades
    quiet_unsub_sec: int = field(default_factory=lambda: _i("SNIPER_QUIET_UNSUB_SEC", 120))
    max_watch_sec: int = field(default_factory=lambda: _i("SNIPER_MAX_WATCH_SEC", 3900))
    max_subscribed: int = field(default_factory=lambda: _i("SNIPER_MAX_SUBSCRIBED", 150))
    keep_days: int = field(default_factory=lambda: _i("SNIPER_KEEP_DAYS", 14))

    # ---- stage 1 (free, from the trade stream)
    prescore_min: float = field(default_factory=lambda: _f("SNIPER_PRESCORE_MIN", 25))
    min_buyers: int = field(default_factory=lambda: _i("SNIPER_MIN_BUYERS", 5))
    min_inflow_sol: float = field(default_factory=lambda: _f("SNIPER_MIN_INFLOW_SOL", 1.0))

    # ---- bundles and insiders
    bundle_slots: int = field(default_factory=lambda: _i("SNIPER_BUNDLE_SLOTS", 1))          # slots after creation that count as bundle
    early_sec: float = field(default_factory=lambda: _f("SNIPER_EARLY_SEC", 0.5))            # arrived together with the creation
    bundle_warn_pct: float = field(default_factory=lambda: _f("SNIPER_BUNDLE_WARN_PCT", 5))
    bundle_block_pct: float = field(default_factory=lambda: _f("SNIPER_BUNDLE_BLOCK_PCT", 15))
    bundle_min_wallets: int = field(default_factory=lambda: _i("SNIPER_BUNDLE_MIN_WALLETS", 3))
    dev_warn_pct: float = field(default_factory=lambda: _f("SNIPER_DEV_WARN_PCT", 5))
    dev_block_pct: float = field(default_factory=lambda: _f("SNIPER_DEV_BLOCK_PCT", 20))
    top10_warn_pct: float = field(default_factory=lambda: _f("SNIPER_TOP10_WARN_PCT", 30))
    top10_block_pct: float = field(default_factory=lambda: _f("SNIPER_TOP10_BLOCK_PCT", 60))
    sniper_bots_warn: int = field(default_factory=lambda: _i("SNIPER_FAST_BUYERS_WARN", 8))  # distinct buyers in the first 2 s
    dev_max_launches_24h: int = field(default_factory=lambda: _i("SNIPER_DEV_MAX_LAUNCHES_24H", 3))
    funders_profiled: int = field(default_factory=lambda: _i("SNIPER_FUNDERS_PROFILED", 6))  # from caches; network only within budget
    bundle_txs_fetched: int = field(default_factory=lambda: _i("SNIPER_BUNDLE_TXS_FETCHED", 2))

    # ---- decision
    min_score: float = field(default_factory=lambda: _f("SNIPER_MIN_SCORE", 40))
    max_risk: float = field(default_factory=lambda: _f("SNIPER_MAX_RISK", 45))
    require_launch_data: bool = field(default_factory=lambda: _b("SNIPER_REQUIRE_LAUNCH_DATA", True))  # no buy without slot data
    insider_block: bool = field(default_factory=lambda: _b("SNIPER_INSIDER_BLOCK", True))
    safety_gate: bool = field(default_factory=lambda: _b("SNIPER_SAFETY_GATE", True))
    safety_deep: bool = field(default_factory=lambda: _b("SNIPER_SAFETY_DEEP", True))       # safety/ budgets its own calls
    trade_external: bool = field(default_factory=lambda: _b("SNIPER_TRADE_EXTERNAL", False))  # GeckoTerminal pools

    # ---- paper trading (fills and fees like the copy trader)
    paper_start_sol: float = field(default_factory=lambda: _f("SNIPER_PAPER_START_SOL", 5.0))
    size_sol: float = field(default_factory=lambda: _f("SNIPER_SIZE_SOL", 0.1))
    # paper has no budget (user, 10/2026): 0 = no limit. max_open stays as a technical ceiling only, every open
    # position holds a PumpPortal trade subscription
    max_open: int = field(default_factory=lambda: _i("SNIPER_MAX_OPEN", 100))
    max_entries_per_hour: int = field(default_factory=lambda: _i("SNIPER_MAX_ENTRIES_PER_HOUR", 0))
    curve_fee_pct: float = field(default_factory=lambda: _f("SNIPER_CURVE_FEE_PCT", 1.25))   # pump.fun protocol + creator fee
    adverse_slippage: float = field(default_factory=lambda: _f("SNIPER_ADVERSE_SLIPPAGE", 0.03))
    priority_fee_sol: float = field(default_factory=lambda: _f("SNIPER_PRIORITY_FEE_SOL", 0.001))
    network_fee_sol: float = field(default_factory=lambda: _f("SNIPER_NETWORK_FEE_SOL", 0.000005))
    slippage_bps: int = field(default_factory=lambda: _i("SNIPER_SLIPPAGE_BPS", 1000))
    use_jupiter: bool = field(default_factory=lambda: _b("SNIPER_USE_JUPITER", True))

    # ---- exits
    tp1_pct: float = field(default_factory=lambda: _f("SNIPER_TP1_PCT", 60))         # first profit push
    tp1_frac: float = field(default_factory=lambda: _f("SNIPER_TP1_FRAC", 0.5))      # share sold there
    trail_pct: float = field(default_factory=lambda: _f("SNIPER_TRAIL_PCT", 25))     # from the peak, after tp1
    stop_pct: float = field(default_factory=lambda: _f("SNIPER_STOP_PCT", 30))
    time_stop_min: float = field(default_factory=lambda: _f("SNIPER_TIME_STOP_MIN", 12))   # no tp1 by then -> out
    max_hold_min: float = field(default_factory=lambda: _f("SNIPER_MAX_HOLD_MIN", 60))
    exit_on_insider_sell: bool = field(default_factory=lambda: _b("SNIPER_EXIT_ON_INSIDER_SELL", True))
    insider_sell_min_pct: float = field(default_factory=lambda: _f("SNIPER_INSIDER_SELL_MIN_PCT", 25))

    horizons_min: tuple = (1, 5, 15, 60)

    def exit_params(self):
        return {k: getattr(self, k) for k in ("tp1_pct", "tp1_frac", "trail_pct", "stop_pct", "time_stop_min",
                                              "max_hold_min", "exit_on_insider_sell", "insider_sell_min_pct",
                                              "size_sol", "min_score", "max_risk")}


def load() -> SniperSettings:
    return SniperSettings()


settings = load()
