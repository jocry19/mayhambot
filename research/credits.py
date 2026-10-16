"""Helius credits for research: a thin view on core.http's shared ledger (data/ratelimit/credits-helius.json).

core.http books every Helius request itself (RPC 1, DAS 10, api.helius.xyz 100) against the research share of
the monthly plan and raises http.CreditsExhausted for DAS/Enhanced once today's budget is used up. Research
only asks before an expensive call whether it is still worth trying. RESEARCH_HELIUS_CREDITS_PER_DAY > 0 adds
an extra, lower cap on top (default 0 = core's daily budget alone).
"""
from core import http
from research.config import rsettings

COST = {"rpc": 1, "das": 10, "enhanced": 100}


def used_today():
    st = http.credits("research") or {}
    return st.get("credits_today", 0)


def left_today():
    """Credits research may still spend today (None = unlimited, e.g. a manual run without a service name)."""
    left = http.credits_left_today()
    if rsettings.helius_credits_per_day > 0:
        own = max(0.0, rsettings.helius_credits_per_day - used_today())
        left = own if left is None else min(left, own)
    return left


def can(kind, n=1):
    left = left_today()
    return left is None or left >= COST[kind] * n


def report():
    return http.credits("research") or {}
