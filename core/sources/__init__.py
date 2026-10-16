"""Pluggable data sources. `market` = token discovery, prices and pool trades; `wallet_source` = wallet history."""
from core.sources import market  # noqa: F401
from core.sources import wallet_history as wallet_source  # noqa: F401
