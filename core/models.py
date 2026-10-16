from dataclasses import dataclass, field

SOL_MINT = "So11111111111111111111111111111111111111112"
STABLE_MINTS = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
}
QUOTE_MINTS = {SOL_MINT} | STABLE_MINTS


@dataclass
class Trade:
    sig: str
    wallet: str
    mint: str
    side: str            # 'buy' | 'sell'
    ts: int
    token_amount: float
    sol_amount: float    # SOL value of the trade (0 if unknown)
    usd_amount: float
    price_usd: float     # token price in USD at the trade
    source: str


@dataclass
class Token:
    mint: str
    symbol: str = ""
    name: str = ""
    pool_address: str = ""
    dex: str = ""
    price_usd: float = 0.0
    mcap_usd: float = 0.0
    liquidity_usd: float = 0.0
    pair_created_at: int = 0
    price_change_m5: float = 0.0
    price_change_h1: float = 0.0
    price_change_h24: float = 0.0
    volume_h1: float = 0.0
    extra: dict = field(default_factory=dict)
