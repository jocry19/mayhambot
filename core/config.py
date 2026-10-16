"""Settings loaded from mayhambot/.env and the environment. See .env.example."""
import os
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # optional
    load_dotenv = None

ROOT = Path(__file__).resolve().parent.parent
if load_dotenv:
    load_dotenv(ROOT / ".env")


def _f(name, default):
    v = os.getenv(name)
    return float(v) if v not in (None, "") else float(default)


def _i(name, default):
    return int(_f(name, default))


def _s(name, default=""):
    return os.getenv(name, default) or default


@dataclass
class Settings:
    db_path: str
    rpc_url: str
    helius_api_key: str
    telegram_bot_token: str
    telegram_chat_id: str
    # discovery / tracking
    min_liquidity_usd: float
    max_mcap_usd: float
    max_tracked_tokens: int
    cycle_seconds: int
    trades_budget_seconds: int
    discover_every_cycles: int
    # wallet scoring
    smart_min_score: float
    smart_min_tokens: int
    tier_ultra_score: float
    tier_good_score: float
    min_position_usd: float
    early_hit_multiple: float
    bot_max_trades_per_token: int
    backfill_wallets_per_cycle: int
    backfill_tx_limit: int
    # signals
    signal_window_min: int
    signal_min_score: float
    model_signal_min_score: float
    model_signal_min_lift: float
    model_signal_floor: float
    signal_min_wallets: int
    signal_min_net_usd: float
    signal_cooldown_min: int
    inflow_ref_usd: float
    wallets_ref_count: float

    @property
    def helius_rpc_url(self):
        return f"https://mainnet.helius-rpc.com/?api-key={self.helius_api_key}" if self.helius_api_key else ""


def load_settings() -> Settings:
    helius = _s("HELIUS_API_KEY")
    rpc = _s("SOLANA_RPC_URL") or (f"https://mainnet.helius-rpc.com/?api-key={helius}" if helius
                                   else "https://api.mainnet-beta.solana.com")
    db_path = _s("MAYHAM_DB_PATH", str(ROOT / "data" / "mayhambot.db"))
    return Settings(
        db_path=db_path,
        rpc_url=rpc,
        helius_api_key=helius,
        telegram_bot_token=_s("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=_s("TELEGRAM_CHAT_ID"),
        min_liquidity_usd=_f("MIN_LIQUIDITY_USD", 8000),
        max_mcap_usd=_f("MAX_MCAP_USD", 50_000_000),
        max_tracked_tokens=_i("MAX_TRACKED_TOKENS", 25),
        cycle_seconds=_i("CYCLE_SECONDS", 90),
        trades_budget_seconds=_i("TRADES_BUDGET_SECONDS", 150),
        discover_every_cycles=_i("DISCOVER_EVERY_CYCLES", 5),
        smart_min_score=_f("SMART_MIN_SCORE", 55),
        smart_min_tokens=_i("SMART_MIN_TOKENS", 2),
        tier_ultra_score=_f("TIER_ULTRA_SCORE", 75),
        tier_good_score=_f("TIER_GOOD_SCORE", 40),
        min_position_usd=_f("MIN_POSITION_USD", 25),
        early_hit_multiple=_f("EARLY_HIT_MULTIPLE", 2.0),
        bot_max_trades_per_token=_i("BOT_MAX_TRADES_PER_TOKEN", 40),
        backfill_wallets_per_cycle=_i("BACKFILL_WALLETS_PER_CYCLE", 3),
        backfill_tx_limit=_i("BACKFILL_TX_LIMIT", 60),
        signal_window_min=_i("SIGNAL_WINDOW_MIN", 30),
        signal_min_score=_f("SIGNAL_MIN_SCORE", 60),
        model_signal_min_score=_f("MODEL_SIGNAL_MIN_SCORE", 40),
        model_signal_min_lift=_f("MODEL_SIGNAL_MIN_LIFT", 2.0),
        model_signal_floor=_f("MODEL_SIGNAL_FLOOR", 10),
        signal_min_wallets=_i("SIGNAL_MIN_WALLETS", 2),
        signal_min_net_usd=_f("SIGNAL_MIN_NET_USD", 500),
        signal_cooldown_min=_i("SIGNAL_COOLDOWN_MIN", 60),
        inflow_ref_usd=_f("INFLOW_REF_USD", 5000),
        wallets_ref_count=_f("WALLETS_REF_COUNT", 4),
    )


settings = load_settings()
