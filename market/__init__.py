"""Market analysis: phase (risk-on/off), sector and narrative heat, capital rotation, market context per coin.
Interface for signals/, sniper/, copytrade/, model/, web/, cli/ (see market/README.md)."""
from market.context import FEATURES, coin_context, features, phase, signal_factor, sniper_adjust, summary  # noqa: F401
from market.evaluate import evaluate as evaluation  # noqa: F401
from market.view import view  # noqa: F401
