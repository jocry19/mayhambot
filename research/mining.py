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


def run(conn, now=None):
    """Add realized winners as research candidates. Returns how many."""
    cand = realized_winners(conn, now=now)
    by_prio = {}
    for w, p in cand.items():
        by_prio.setdefault(round(min(20.0, p), 1), []).append(w)
    for p, ws in by_prio.items():
        retry(lambda: add_research_wallets(conn, ws, "realized_winner", p), conn=conn)
    return len(cand)
