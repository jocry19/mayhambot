"""Research settings from .env (via core.config, which already loaded it)."""
import os
from dataclasses import dataclass

import core.config  # noqa: F401  (loads mayhambot/.env)


def _f(name, default):
    v = os.getenv(name)
    return float(v) if v not in (None, "") else float(default)


def _i(name, default):
    return int(_f(name, default))


@dataclass
class ResearchSettings:
    # discovery budget per run (GeckoTerminal allows ~30 calls/min keyless)
    gt_calls_per_run: int = _i("RESEARCH_GT_CALLS_PER_RUN", 80)
    early_tokens_per_run: int = _i("RESEARCH_EARLY_TOKENS_PER_RUN", 8)
    early_max_sig_pages: int = _i("RESEARCH_EARLY_MAX_SIG_PAGES", 40)   # 1000 signatures per page
    early_tx_per_token: int = _i("RESEARCH_EARLY_TX_PER_TOKEN", 120)    # earliest swaps parsed per token
    wallets_per_run: int = _i("RESEARCH_WALLETS_PER_RUN", 25)
    wallet_tx_limit: int = _i("RESEARCH_WALLET_TX_LIMIT", 100)
    rebackfill_hours: float = _f("RESEARCH_REBACKFILL_HOURS", 24)
    discover_every_min: float = _f("RESEARCH_DISCOVER_EVERY_MIN", 20)
    # what counts as "pumped" when choosing tokens to mine early buyers from
    pumped_multiple: float = _f("RESEARCH_PUMPED_MULTIPLE", 3.0)
    max_token_age_days: float = _f("RESEARCH_MAX_TOKEN_AGE_DAYS", 7)
    # influence = what happens in the N minutes after a wallet's buy
    influence_window_min: int = _i("RESEARCH_INFLUENCE_WINDOW_MIN", 10)
    default_supply: float = _f("RESEARCH_DEFAULT_SUPPLY", 1e9)  # pump.fun standard supply when unknown


rsettings = ResearchSettings()
