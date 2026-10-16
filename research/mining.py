"""Find winner wallets in data we already have (no API calls).

realized_winners: wallets that, in the last N days, sold a coin for at least `min_mult` x their buy cost after
round-trip costs (measured in SOL, so the SOL price does not matter), on real size. Wallets that did it in
several coins rank highest. They go into research_wallets, so the backfill judges them over their whole
history before they can reach a tier (one lucky coin is not enough for a tier).
"""
import math
import time

from research.config import rsettings
from research.store import add_research_wallets, retry


def realized_winners(conn, days=7, min_mult=2.0, min_buy_sol=0.2, limit=300, now=None):
    now = int(now or time.time())
    cost = rsettings.cost_roundtrip
    rows = conn.execute(
        """SELECT wallet, mint,
                  SUM(CASE WHEN side='buy' THEN sol_amount ELSE 0 END) AS b_sol,
                  SUM(CASE WHEN side='buy' THEN token_amount ELSE 0 END) AS b_tok,
                  SUM(CASE WHEN side='sell' THEN sol_amount ELSE 0 END) AS s_sol,
                  SUM(CASE WHEN side='sell' THEN token_amount ELSE 0 END) AS s_tok,
                  COUNT(*) AS n
           FROM trades WHERE ts > ? GROUP BY wallet, mint
           HAVING b_sol >= ? AND s_tok > 0 AND b_tok > 0""", (now - days * 86400, min_buy_sol)).fetchall()
    bots = {r[0] for r in conn.execute("SELECT address FROM wallets WHERE is_bot=1")}
    per = {}
    for w, _m, b_sol, b_tok, s_sol, s_tok, n in rows:
        if w in bots or n > 40:
            continue
        sold = min(1.0, s_tok / b_tok)
        mult = (s_sol / sold) / b_sol if sold > 0 else 0.0  # sell price / buy price, in SOL
        d = per.setdefault(w, [0, 0, 0.0])
        d[0] += 1
        if sold >= 0.5 and mult * (1 - cost) >= min_mult:
            d[1] += 1
            d[2] = max(d[2], mult)
    out = {w: d[1] * 2 + math.log2(d[2]) + d[1] / d[0] for w, d in per.items() if d[1] >= 1}
    return dict(sorted(out.items(), key=lambda x: -x[1])[:limit])


def repeat_early(conn, min_peak=3.0, entry_share=1 / 3, min_coins=2, days=14, limit=300, now=None):
    """Wallets that bought several coins that later ran >= min_peak x, each time at <= entry_share of the later
    peak (early, not chasing). Coins and peaks from research candles; prices in USD at trade time, so it is a
    candidate filter only: the backfill judges every wallet over its whole history."""
    now = int(now or time.time())
    peaks = dict(conn.execute(
        """SELECT c.mint, MAX(c.h) FROM research_candles c JOIN research_tokens t ON t.mint = c.mint
           WHERE t.peak_multiple >= ? AND c.ts > ? GROUP BY c.mint""", (min_peak, now - days * 86400)).fetchall())
    if not peaks:
        return {}
    bots = {r[0] for r in conn.execute("SELECT address FROM wallets WHERE is_bot=1")}
    hits = {}
    for mint, peak in peaks.items():
        if not peak:
            continue
        for w, px in conn.execute("""SELECT wallet, MIN(price_usd) FROM trades WHERE mint=? AND side='buy'
                                     AND price_usd > 0 GROUP BY wallet""", (mint,)):
            if w not in bots and px <= peak * entry_share:
                hits.setdefault(w, set()).add(mint)
    out = {w: 5.0 + 2 * len(ms) for w, ms in hits.items() if len(ms) >= min_coins}
    return dict(sorted(out.items(), key=lambda x: -x[1])[:limit])


def run(conn, now=None):
    """Add realized winners and repeat early buyers as research candidates. Returns how many."""
    cand = realized_winners(conn, now=now)
    for w, p in repeat_early(conn, now=now).items():
        cand[w] = max(cand.get(w, 0), p)
    by_prio = {}
    for w, p in cand.items():
        by_prio.setdefault(round(min(20.0, p), 1), []).append(w)
    for p, ws in by_prio.items():
        retry(lambda: add_research_wallets(conn, ws, "realized_winner", p), conn=conn)
    return len(cand)
