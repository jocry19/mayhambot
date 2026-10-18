"""Copy-trading settings, read from mayhambot/.env (or the process environment).

Every value has a safe default. Paper mode is the default; live trading needs
LIVE_TRADING=true AND a signer key (SOLANA_PRIVATE_KEY or SOLANA_KEYPAIR_PATH).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # mayhambot/

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env", override=False)
except Exception:  # python-dotenv is optional
    pass

SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
QUOTE_MINTS = {SOL_MINT, USDC_MINT, USDT_MINT}


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


def _i(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, default)))
    except ValueError:
        return default


def _b(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    # --- data sources -------------------------------------------------------
    rpc_url: str = field(default_factory=lambda: os.getenv("SOLANA_RPC_URL") or (
        f"https://mainnet.helius-rpc.com/?api-key={os.getenv('HELIUS_API_KEY')}" if os.getenv("HELIUS_API_KEY")
        else "https://api.mainnet-beta.solana.com"))
    rpc_max_rps: float = field(default_factory=lambda: _f("SOLANA_RPC_MAX_RPS", 4))
    helius_api_key: str = field(default_factory=lambda: os.getenv("HELIUS_API_KEY", ""))
    jupiter_url: str = field(default_factory=lambda: os.getenv("JUPITER_API_URL", "https://lite-api.jup.ag").rstrip("/"))
    jupiter_api_key: str = field(default_factory=lambda: os.getenv("JUPITER_API_KEY", ""))
    birdeye_api_key: str = field(default_factory=lambda: os.getenv("BIRDEYE_API_KEY", ""))
    db_path: Path = field(default_factory=lambda: Path(os.getenv("MAYHAM_DB_PATH", str(ROOT / "data" / "mayhambot.db"))))

    # --- wallets ------------------------------------------------------------
    watch_wallets: list[str] = field(
        default_factory=lambda: [w.strip() for w in os.getenv("CT_WATCH_WALLETS", "").split(",") if w.strip()]
    )
    max_tracked_wallets: int = field(default_factory=lambda: _i("CT_MAX_TRACKED_WALLETS", 40))
    wallet_poll_seconds: float = field(default_factory=lambda: _f("CT_WALLET_POLL_SECONDS", 8))
    # quiet wallets are polled less often (each poll = 1 RPC credit): last swap < 6 h -> every
    # wallet_poll_seconds, < 48 h -> every CT_POLL_WARM_SECONDS, older/never -> every CT_POLL_COLD_SECONDS
    # "is there anything new?" polls (getSignaturesForAddress) go to a free RPC so a paid/credit RPC (Helius)
    # is only used for real transactions. Default: public mainnet when SOLANA_RPC_URL is Helius; "off" = same RPC.
    poll_rpc_url: str = field(default_factory=lambda: os.getenv("CT_POLL_RPC_URL", ""))
    # real-time detection: logsSubscribe per hot wallet on this websocket ("off" = polling only)
    # CT_WS_URLS: comma list, tried in order, next one after a drop; CT_WS_URL (single) still works; "off" = none
    ws_urls: list[str] = field(default_factory=lambda: [u.strip() for u in (
        os.getenv("CT_WS_URLS") or os.getenv("CT_WS_URL")
        or "wss://api.mainnet-beta.solana.com,wss://solana-rpc.publicnode.com,wss://solana.drpc.org").split(",")
        if u.strip()])

    @property
    def ws_url(self) -> str:
        return "off" if not self.ws_urls or self.ws_urls[0] == "off" else self.ws_urls[0]

    # every active wallet up to this many gets a subscription (manual, Ultra/Smart, ranked first)
    ws_max_wallets: int = field(default_factory=lambda: _i("CT_WS_MAX_WALLETS", 150))
    # independent websocket connections on different endpoints watching the same wallets (one drop is no gap)
    ws_parallel: int = field(default_factory=lambda: _i("CT_WS_PARALLEL", 2))
    ws_fetchers: int = field(default_factory=lambda: _i("CT_WS_FETCHERS", 4))  # parallel transaction reads
    # websocket reads of any wallet go to the main RPC right after one failed public try while more than this share
    # of the day's Helius budget is left (below it: only Ultra/Smart/manual wallets, on the last try)
    ws_main_reserve_pct: float = field(default_factory=lambda: _f("CT_WS_MAIN_RESERVE_PCT", 25.0))
    # one more wallet stream on Helius (HELIUS_API_KEY set): the public nodes drop notifications or cap at 100
    # subscriptions; Helius bills 2 credits per 0.1 MB streamed and pauses below the same reserve
    ws_helius: bool = field(default_factory=lambda: _b("CT_WS_HELIUS", True))
    ws_helius_max: int = field(default_factory=lambda: _i("CT_WS_HELIUS_MAX", 150))
    # the "block 0" group (Erkenntnisse 17.10., 1d): tiny buys in a coin's very first moment, together within 5 s.
    # Watched live and kept active (never pruned as bots); "prefix…suffix" or a bare prefix is resolved against the
    # wallets the bot has seen (unique match only), full addresses are taken as they are
    block0_wallets: list[str] = field(default_factory=lambda: [w.strip() for w in os.getenv(
        "CT_BLOCK0_WALLETS", "52bZXsA9XHvWP5JYgmt1r8GQJ5bse3FK5ALf6j3pVu4e,5kGadrQbtuyi5KHDM69oVuFsUwsw98xopsRsRF3QnuFR,"
        "3dy8rcMdXBRQP2GLf2LgMhy9L9a2mEbsjQNv8EZ9gBWe,2XMRHnX9u1jYW8EardqAgtDPgrz89xLBTnDc35wV8LSS,"
        "9ZYMMXvVPSfq6JdUjXBv9yCo1J1XbphPQMbWS5oDThJ,7nZn5LKSzqh39Xx62ju6x8DCJ9ZaJHR3h254bTiJWiuF,"
        "Gygj9QQby4j2jryqyqBHvLP7ctv2SaANgh4sCb69BUpA,AopdiFdCvoP84YYGzfnLcmtehtiWKet3fzxTZbq9Qtke").split(",") if w.strip()])
    discover_every_s: float = field(default_factory=lambda: _f("CT_DISCOVER_EVERY_SECONDS", 1800))
    poll_warm_seconds: float = field(default_factory=lambda: _f("CT_POLL_WARM_SECONDS", 60))
    poll_cold_seconds: float = field(default_factory=lambda: _f("CT_POLL_COLD_SECONDS", 300))
    min_whale_buy_sol: float = field(default_factory=lambda: _f("CT_MIN_WHALE_BUY_SOL", 2.0))

    # --- ranking ------------------------------------------------------------
    pump_min_gain: float = field(default_factory=lambda: _f("CT_PUMP_MIN_GAIN", 0.25))  # +25% counts as pump
    pump_retrace: float = field(default_factory=lambda: _f("CT_PUMP_RETRACE", 0.15))  # 15% off high ends first pump
    pump_window_min: int = field(default_factory=lambda: _i("CT_PUMP_WINDOW_MIN", 120))
    inflow_spike_factor: float = field(default_factory=lambda: _f("CT_INFLOW_SPIKE_FACTOR", 3.0))
    min_wallet_samples: int = field(default_factory=lambda: _i("CT_MIN_WALLET_SAMPLES", 4))
    min_wallet_score: float = field(default_factory=lambda: _f("CT_MIN_WALLET_SCORE", 0.05))
    assumed_latency_s: float = field(default_factory=lambda: _f("CT_ASSUMED_LATENCY_S", 15))

    # --- entry filters ------------------------------------------------------
    min_liquidity_usd: float = field(default_factory=lambda: _f("CT_MIN_LIQUIDITY_USD", 15000))
    max_token_age_h: float = field(default_factory=lambda: _f("CT_MAX_TOKEN_AGE_H", 72))
    # 30 s: copies later than 30 s after the whale lost 12 % per trade on paper (10/2026), within 30 s 3 %
    max_entry_delay_s: float = field(default_factory=lambda: _f("CT_MAX_ENTRY_DELAY_S", 15))
    # wash trading (safety/ check "wash": share of volume bought AND sold by the same wallets): no copy above
    max_wash_share: float = field(default_factory=lambda: _f("CT_MAX_WASH_SHARE", 0.30))
    # at most this many whale-mode copies per coin within 6 h (all wallets together; 0 = no limit)
    max_copies_per_coin: int = field(default_factory=lambda: _i("CT_MAX_COPIES_PER_COIN", 2))
    # paper only: copy unranked whales at half size so the ranking learns faster (off: every whale buy is
    # still measured and replayed by learn.py from ct_buy_outcomes, so learning does not need these trades)
    explore_unranked: bool = field(default_factory=lambda: _b("CT_EXPLORE_UNRANKED", False))
    # second paper account 'explore' (Erkundung): the strict account ('paper') mirrors the live rules, so it may go
    # hours without a trade; this one copies every ultra/smart whale buy >= CT_EXPLORE_MIN_SOL inside the entry
    # window (rug check, market and wash filters still apply) at a small fixed size with its own balance and PnL,
    # so learning, fill calibration and the exit study keep getting real trades. Never live.
    explore: bool = field(default_factory=lambda: _b("CT_EXPLORE", True))
    explore_size_sol: float = field(default_factory=lambda: _f("CT_EXPLORE_SIZE_SOL", 0.1))
    explore_min_sol: float = field(default_factory=lambda: _f("CT_EXPLORE_MIN_SOL", 0.5))
    explore_max_open: int = field(default_factory=lambda: _i("CT_EXPLORE_MAX_OPEN", 5))
    explore_start_balance_sol: float = field(default_factory=lambda: _f("CT_EXPLORE_START_BALANCE_SOL", 5.0))
    explore_min_liq_usd: float = field(default_factory=lambda: _f("CT_EXPLORE_MIN_LIQ_USD", 5000))
    explore_tiers: str = field(default_factory=lambda: os.getenv("CT_EXPLORE_TIERS", "ultra,smart"))
    # copy a ranked whale only if its past buys (ct_evals, our current exits, calibrated costs) made money
    require_edge: bool = field(default_factory=lambda: _b("CT_REQUIRE_EDGE", True))
    # strict whale copies decided by a draw from each wallet's edge belief (learn.thompson) instead of the
    # samples/score/edge gates, from the exploration minimum on; CT_THOMPSON=false brings the gates back
    thompson: bool = field(default_factory=lambda: _b("CT_THOMPSON", True))
    edge_min_n: int = field(default_factory=lambda: _i("CT_EDGE_MIN_N", 8))
    max_price_run_since_whale: float = field(default_factory=lambda: _f("CT_MAX_RUN_SINCE_WHALE", 0.15))

    # --- exits ----------------------------------------------------------------
    # exit_mode: first_pump (per-wallet target learned from history), target (fixed TP), inflow_peak
    exit_mode: str = field(default_factory=lambda: os.getenv("CT_EXIT_MODE", "first_pump"))
    take_profit: float = field(default_factory=lambda: _f("CT_TAKE_PROFIT", 0.35))
    tp_min: float = field(default_factory=lambda: _f("CT_TP_MIN", 0.15))
    tp_max: float = field(default_factory=lambda: _f("CT_TP_MAX", 1.5))
    tp_fraction_of_median_pump: float = field(default_factory=lambda: _f("CT_TP_FRACTION", 0.7))
    stop_loss: float = field(default_factory=lambda: _f("CT_STOP_LOSS", 0.20))
    trail_activate: float = field(default_factory=lambda: _f("CT_TRAIL_ACTIVATE", 0.15))
    trail_drawdown: float = field(default_factory=lambda: _f("CT_TRAIL_DRAWDOWN", 0.10))
    max_hold_min: float = field(default_factory=lambda: _f("CT_MAX_HOLD_MIN", 60))
    exit_on_whale_sell: bool = field(default_factory=lambda: _b("CT_EXIT_ON_WHALE_SELL", True))
    price_poll_seconds: float = field(default_factory=lambda: _f("CT_PRICE_POLL_SECONDS", 5))

    # --- sizing & risk ------------------------------------------------------
    position_size_sol: float = field(default_factory=lambda: _f("CT_POSITION_SIZE_SOL", 0.5))
    max_open_positions: int = field(default_factory=lambda: _i("CT_MAX_OPEN_POSITIONS", 5))
    max_daily_loss_sol: float = field(default_factory=lambda: _f("CT_MAX_DAILY_LOSS_SOL", 2.0))
    paper_start_balance_sol: float = field(default_factory=lambda: _f("CT_PAPER_START_SOL", 10.0))
    slippage_bps: int = field(default_factory=lambda: _i("CT_SLIPPAGE_BPS", 300))
    priority_fee_sol: float = field(default_factory=lambda: _f("CT_PRIORITY_FEE_SOL", 0.0005))
    network_fee_sol: float = field(default_factory=lambda: _f("CT_NETWORK_FEE_SOL", 0.000005))

    # --- live -----------------------------------------------------------------
    live_trading: bool = field(default_factory=lambda: _b("LIVE_TRADING", False))
    private_key: str = field(default_factory=lambda: os.getenv("SOLANA_PRIVATE_KEY", ""), repr=False)
    keypair_path: str = field(default_factory=lambda: os.getenv("SOLANA_KEYPAIR_PATH", ""))

    @property
    def mode(self) -> str:
        return "live" if self.live_ready else "paper"

    @property
    def live_ready(self) -> bool:
        return self.live_trading and bool(self.private_key or self.keypair_path)


def load() -> Settings:
    return Settings()
