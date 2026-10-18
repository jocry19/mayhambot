"""A running price per mint from the swaps the bot reads anyway (ct_swaps of tracked wallets, core's trades).

For coins DexScreener does not list yet and the sniper does not watch: strategy._curve_snapshot uses it as a third
source for entries and for marking open positions. No RPC call: only swaps already in the database count.
"""
from __future__ import annotations

import statistics
import time

MIN_SOL = 0.01  # dust swaps (fee-sized) say nothing about the price


def last_price(conn, mint: str, max_age_s: float = 60, now: float | None = None) -> dict | None:
    """{"ts", "price_sol", "source", "n", "median_sol"} of the newest swap of `mint` at most max_age_s old (ts = its
    block time; median over the swaps in that window, against single outliers), else None."""
    now = now or time.time()
    since = int(now - max_age_s)
    rows = []
    try:
        rows += [(r[0], r[1], "ct_swaps") for r in conn.execute(
            "SELECT ts, price_sol FROM ct_swaps WHERE mint=? AND ts>=? AND sol_amount>=? AND price_sol>0",
            (mint, since, MIN_SOL))]
    except Exception:
        pass
    try:
        rows += [(r[0], r[1] / r[2], "trades") for r in conn.execute(
            "SELECT ts, sol_amount, token_amount FROM trades WHERE mint=? AND ts>=? AND sol_amount>=? AND token_amount>0",
            (mint, since, MIN_SOL))]
    except Exception:
        pass
    if not rows:
        return None
    ts, px, src = max(rows, key=lambda r: r[0])
    return {"ts": ts, "price_sol": px, "source": src, "n": len(rows),
            "median_sol": statistics.median(r[1] for r in rows)}
