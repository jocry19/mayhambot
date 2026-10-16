"""Synthetic market with known truth (skilled wallets, followers, pumps, rugs) for offline tests.
Never used by the app itself — only by tests and `python -m research selftest`."""
import random

from core import db
from core.models import Token, Trade
from research.store import ensure_schema, store_candles

TRUE_TIERS = {"ultra": (6, 0.65, 0.06), "smart": (14, 0.45, 0.10), "good": (30, 0.30, 0.14)}  # n, p|pump, p|no pump


def build(path, days=5, n_tokens=140, n_random=300, seed=1, now=None):
    rng = random.Random(seed)
    conn = ensure_schema(db.connect(path))
    now = now or 1_760_000_000
    start = now - days * 86400
    wallets = {}
    for tier, (n, _, _) in TRUE_TIERS.items():
        for i in range(n):
            wallets[f"{tier.upper()}{i:03d}"] = tier
    randoms = [f"RAND{i:04d}" for i in range(n_random)]
    bots = [f"BOT{i}" for i in range(3)]
    trades, toks = [], []
    sig = 0

    def T(w, mint, side, ts, usd, px):
        nonlocal sig
        sig += 1
        return Trade(f"s{sig}", w, mint, side, int(ts), usd / px, usd / 150, usd, px, "test_pool")

    for k in range(n_tokens):
        mint = f"MINT{k:04d}"
        t0 = start + rng.randint(0, int(days * 86400 * 0.9))
        pump = rng.random() < 0.3
        p0 = 5e-6 * rng.uniform(0.5, 3)
        pump_start = rng.randint(20, 90) * 60          # pump begins 20-90 min after launch
        peak_t = pump_start + rng.randint(15, 120) * 60
        mult = rng.uniform(2.5, 12) if pump else rng.uniform(0.9, 1.3)
        rug = (not pump) and rng.random() < 0.3
        candles = []
        price = p0
        for m in range(0, 10 * 60):
            ts = t0 + m * 60
            if ts > now:
                break
            s = m * 60
            if s < pump_start:
                target = p0 * (1 + 0.1 * rng.random())
            elif s < peak_t:
                target = p0 * (1 + (mult - 1) * (s - pump_start) / (peak_t - pump_start))
            else:
                target = p0 * mult * max(0.25, 1 - 0.002 * (s - peak_t) / 60)
            if rug and s > pump_start:
                target = p0 * 0.03
            price = target * rng.uniform(0.97, 1.03)
            candles.append((ts, price, price * 1.02, price * 0.98, price, 1000.0))
        store_candles(conn, mint, 60, candles)
        last = candles[-1][4]
        toks.append(Token(mint=mint, symbol=f"T{k}", price_usd=last, mcap_usd=last * 1e9, liquidity_usd=50000,
                          pool_address=f"POOL{k}", pair_created_at=t0))

        def px_at(ts):
            i = max(0, min(len(candles) - 1, int((ts - t0) // 60)))
            return candles[i][4]

        # skilled wallets: enter before the pump, more often on coins that will pump
        entries = []
        for w, tier in wallets.items():
            _, p_pump, p_no = TRUE_TIERS[tier]
            if rng.random() < (p_pump if pump else p_no):
                ts = t0 + rng.randint(2, max(3, pump_start // 60 - 1)) * 60
                if ts < now:
                    entries.append((w, ts, tier))
        for w, ts, tier in entries:
            usd = rng.uniform(200, 2000)
            trades.append(T(w, mint, "buy", ts, usd, px_at(ts)))
            sell_ts = t0 + peak_t - rng.randint(0, 10) * 60 if pump else ts + rng.randint(10, 60) * 60
            if sell_ts < now:
                trades.append(T(w, mint, "sell", sell_ts, usd * px_at(sell_ts) / px_at(ts), px_at(sell_ts)))
            # followers: ultra/smart wallets pull copy traders in the next minutes
            nf = {"ultra": 6, "smart": 3, "good": 1}[tier]
            for f in rng.sample(randoms, nf):
                fts = ts + rng.randint(30, 500)
                if fts < now:
                    trades.append(T(f, mint, "buy", fts, rng.uniform(50, 400), px_at(fts)))
        # random crowd
        for w in rng.sample(randoms, rng.randint(8, 25)):
            ts = t0 + rng.randint(1, 9 * 60) * 60
            if ts >= now:
                continue
            usd = rng.uniform(30, 600)
            trades.append(T(w, mint, "buy", ts, usd, px_at(ts)))
            st = ts + rng.randint(5, 240) * 60
            if st < now:
                trades.append(T(w, mint, "sell", st, usd * px_at(st) / px_at(ts), px_at(st)))
        # bots: many tiny trades
        for b in bots:
            for _ in range(rng.randint(0, 50)):
                ts = t0 + rng.randint(1, 9 * 60) * 60
                if ts < now:
                    trades.append(T(b, mint, rng.choice(["buy", "sell"]), ts, 20, px_at(ts)))
    db.insert_trades(conn, trades)
    db.upsert_tokens(conn, toks, tracked=False)
    return conn, wallets, now
