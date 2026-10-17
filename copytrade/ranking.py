"""Whale ranking: which wallets' buys are followed by a pump, how big, how fast,
and how long until the copy-trader crowd (volume spike) arrives.

For every historical whale buy we load 1-minute candles around it and measure:
  first_pump_gain   gain from the whale's entry to the first local top
                    (top = price retraces CT_PUMP_RETRACE from its running high)
  first_pump_min    minutes from the whale buy to that top
  inflow_lead_min   minutes from the whale buy until volume >= CT_INFLOW_SPIKE_FACTOR x baseline
                    (= when the copy traders arrive)
  achievable_gain   what WE would have made entering CT_ASSUMED_LATENCY_S later and exiting
                    with the bot's exit rules, after costs
Per wallet these become a hit rate, medians and an expected value; the score is
EV scaled by confidence (number of samples), so one lucky hit does not rank high.
"""
from __future__ import annotations

import logging
import statistics
import time
from typing import Sequence

from . import market
from .config import Settings

log = logging.getLogger("copytrade.ranking")

Candle = tuple  # (ts, o, h, l, c, v)
RETRY_EVERY_S, RETRY_FOR_S = 2 * 3600, 2 * 86400
ROUNDTRIP_COST = 0.045  # measured on paper fills 10/2026: Jupiter quote both ways + 0.75 % landing drift each way + fees


def _at(candles: Sequence[Candle], ts: float) -> int:
    """Index of the candle containing ts (or the next one)."""
    for i, c in enumerate(candles):
        if c[0] + 60 > ts:
            return i
    return len(candles)


def simulate_exit(candles: Sequence[Candle], entry: float, take_profit: float, stop_loss: float,
                  trail_activate: float, trail_drawdown: float, max_hold_min: float) -> tuple[float, float, str]:
    """Walk candles after entry with the bot's exit rules. Returns (gain, minutes, reason).

    Within a candle the stop is checked before the target (pessimistic)."""
    if not candles or entry <= 0:
        return 0.0, 0.0, "no_data"
    t0 = candles[0][0]
    high = entry
    for ts, o, h, l, c, v in candles:
        mins = (ts - t0) / 60 + 1
        trail_on = high / entry - 1 >= trail_activate
        stop_px = max(entry * (1 - stop_loss), high * (1 - trail_drawdown) if trail_on else 0)
        if l <= stop_px:  # gap below the stop (rug): filled at the open
            return min(o, stop_px) / entry - 1, mins, "trail" if trail_on else "stop"
        if take_profit and h >= entry * (1 + take_profit):
            return take_profit, mins, "target"
        high = max(high, h)
        if mins >= max_hold_min:
            return c / entry - 1, mins, "time"
    return candles[-1][4] / entry - 1, (candles[-1][0] - t0) / 60 + 1, "end"


def evaluate_buy(candles: Sequence[Candle], t0: int, s: Settings) -> dict | None:
    """Measure what happened after one whale buy at unix time t0."""
    if len(candles) < 5:
        return None
    i0 = _at(candles, t0)
    if i0 >= len(candles) - 2:
        return None
    entry = candles[i0][4]  # close of the whale's minute ~ whale fill
    if entry <= 0:
        return None
    end_ts = t0 + s.pump_window_min * 60
    after = [c for c in candles[i0 + 1:] if c[0] <= end_ts]
    if len(after) < 3:
        return None

    # first pump: first top >= pump_min_gain that then retraces pump_retrace
    peak, peak_ts = entry, candles[i0][0]
    first_gain, first_ts = None, None
    max_gain, max_ts = 0.0, candles[i0][0]
    for ts, o, h, l, c, v in after:
        if h > peak:
            peak, peak_ts = h, ts
        g = peak / entry - 1
        if g > max_gain:
            max_gain, max_ts = g, peak_ts
        if first_gain is None and g >= s.pump_min_gain and l <= peak * (1 - s.pump_retrace):
            first_gain, first_ts = g, peak_ts
    if first_gain is None:
        first_gain, first_ts = max_gain, max_ts

    # copy-trader inflow: volume spike vs. the quiet minutes before the whale
    before = [c[5] for c in candles[max(0, i0 - 15):i0]]
    baseline = statistics.median(before) if before else statistics.median([c[5] for c in after[:3]])
    baseline = max(baseline, 50.0)
    lead = None
    for ts, o, h, l, c, v in after:
        if v >= s.inflow_spike_factor * baseline:
            lead = (ts - t0) / 60
            break

    # what we would have made, entering after our latency
    j = _at(candles, t0 + s.assumed_latency_s)
    ours = [c for c in candles[j:] if c[0] <= end_ts]
    our_entry = ours[0][4] if ours else entry
    gain, mins, reason = simulate_exit(
        ours[1:], our_entry, take_profit=0, stop_loss=s.stop_loss,
        trail_activate=s.trail_activate, trail_drawdown=s.trail_drawdown, max_hold_min=s.max_hold_min,
    )
    return {
        "entry_price_usd": entry,
        "our_entry_price_usd": our_entry,
        "first_pump_gain": first_gain,
        "first_pump_min": max(0.0, (first_ts - t0) / 60),
        "max_gain": max_gain,
        "max_gain_min": max(0.0, (max_ts - t0) / 60),
        "min_before_peak": max(0.0, (first_ts - t0) / 60),
        "inflow_lead_min": lead,
        "achievable_gain": gain - ROUNDTRIP_COST,
        "pumped": int(first_gain >= s.pump_min_gain),
        "candles": len(after),
    }


def evaluate_pending(conn, s: Settings, max_mints: int = 25, min_sol: float | None = None) -> int:
    """Evaluate whale buys that are old enough and not evaluated yet. Returns count."""
    min_sol = s.min_whale_buy_sol if min_sol is None else min_sol
    horizon = int(time.time()) - s.pump_window_min * 60
    rows = conn.execute(
        """SELECT s.sig, s.wallet, s.mint, s.ts, s.sol_amount FROM ct_swaps s
           LEFT JOIN ct_buy_outcomes o ON o.sig = s.sig
           WHERE s.side='buy' AND s.ts < ? AND s.ts > ? AND s.sol_amount >= ?
             AND (o.sig IS NULL OR (o.pumped IS NULL AND o.candles = 0 AND o.evaluated_at < ? AND s.ts > ?))
           ORDER BY o.sig IS NOT NULL, s.ts DESC""",  # failed candle fetches retried every 2 h for 2 days, new first
        (horizon, horizon - 30 * 86400, min_sol, int(time.time()) - RETRY_EVERY_S, int(time.time()) - RETRY_FOR_S),
    ).fetchall()
    by_mint: dict[str, list] = {}
    for r in rows:
        by_mint.setdefault(r["mint"], []).append(r)
    done = 0
    for mint in list(by_mint)[:max_mints]:
        buys = by_mint[mint]
        pool = market.best_pool(mint)
        lo = min(b["ts"] for b in buys) - 20 * 60
        hi = max(b["ts"] for b in buys) + s.pump_window_min * 60
        try:
            candles = market.minute_candles(pool, lo, hi, s, mint=mint) if pool or s.birdeye_api_key else []
        except Exception as e:
            log.warning("candles %s failed: %s", mint[:6], e)
            candles = []
        now = int(time.time())
        for b in buys:
            res = evaluate_buy(candles, b["ts"], s) if candles else None
            res = res or {"pumped": None, "candles": 0}
            conn.execute(
                """INSERT OR REPLACE INTO ct_buy_outcomes(sig, wallet, mint, ts, sol_amount, pool, entry_price_usd,
                   our_entry_price_usd, first_pump_gain, first_pump_min, max_gain, max_gain_min, min_before_peak,
                   inflow_lead_min, achievable_gain, pumped, candles, evaluated_at) VALUES
                   (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (b["sig"], b["wallet"], mint, b["ts"], b["sol_amount"], pool,
                 res.get("entry_price_usd"), res.get("our_entry_price_usd"), res.get("first_pump_gain"),
                 res.get("first_pump_min"), res.get("max_gain"), res.get("max_gain_min"),
                 res.get("min_before_peak"), res.get("inflow_lead_min"), res.get("achievable_gain"),
                 res.get("pumped"), res.get("candles"), now),
            )
            done += 1
        conn.commit()
    return done


def _median(xs: list) -> float | None:
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def rank_wallets(conn, s: Settings) -> int:
    """Aggregate evaluated buys into ct_wallet_rank. Returns number of wallets ranked."""
    rows = conn.execute(
        "SELECT * FROM ct_buy_outcomes WHERE pumped IS NOT NULL ORDER BY wallet, ts"
    ).fetchall()
    by_wallet: dict[str, list] = {}
    for r in rows:
        by_wallet.setdefault(r["wallet"], []).append(r)
    now = int(time.time())
    for w, rs in by_wallet.items():
        n = len(rs)
        hits = [r for r in rs if r["pumped"]]
        hit_rate = (len(hits) + 1) / (n + 3)  # shrunk towards ~33%
        ach = [max(-1.0, min(r["achievable_gain"], s.tp_max)) for r in rs]  # a rug costs everything, not stop + 5 %
        losses = [a for a in ach if a < 0]
        ev = sum(ach) / n
        confidence = n / (n + 5)
        med_first = _median([r["first_pump_gain"] for r in hits])
        med_pump_min = _median([r["first_pump_min"] for r in hits])
        tp = s.take_profit
        if med_first:
            tp = min(s.tp_max, max(s.tp_min, s.tp_fraction_of_median_pump * med_first))
        hold = s.max_hold_min
        if med_pump_min is not None:
            hold = min(s.max_hold_min, max(5.0, 1.5 * med_pump_min + 2))
        conn.execute(
            "INSERT OR REPLACE INTO ct_wallet_rank(address, samples, hit_rate, median_first_pump, median_pump_min, "
            "median_lead_min, median_achievable, avg_loss, expected_value, score, take_profit, max_hold_min, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (w, n, hit_rate, med_first, med_pump_min, _median([r["inflow_lead_min"] for r in rs]),
             _median(ach), (sum(losses) / len(losses)) if losses else 0.0, ev,
             ev * confidence * (1 if n >= s.min_wallet_samples else 0.5), tp, hold, now),
        )
    conn.commit()
    return len(by_wallet)


def wallet_rank(conn, wallet: str):
    return conn.execute("SELECT * FROM ct_wallet_rank WHERE address=?", (wallet,)).fetchone()
