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
    max_entry_delay_s: float = field(default_factory=lambda: _f("CT_MAX_ENTRY_DELAY_S", 90))
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
