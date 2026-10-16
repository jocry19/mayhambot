"""Point-in-time wallet statistics: everything is computed from trades and prices strictly before `cutoff`.

Per wallet: positions (cost basis, realized + unrealized PnL), win rate, early entries (price later
>= 2x the entry), median entry market cap, minutes to 2x, rug exposure, consistency, and influence:
how many other wallets and how much money follow its buys within RESEARCH_INFLUENCE_WINDOW_MIN.
"""
import bisect
import math
from collections import defaultdict

from core.config import settings
from research.config import rsettings
from research.stats import median

COMPONENTS = ("win", "early", "roi", "consistency", "timing", "influence", "rug_avoid")
COMPONENT_LABELS = {
    "win": "Trefferquote", "early": "Frühe Einstiege (danach ≥2x)", "roi": "Rendite (ROI)",
    "consistency": "Konstanz", "timing": "Einstieg bei niedriger Market Cap", "influence": "Einfluss (Follower)",
    "rug_avoid": "Rug-Vermeidung",
}


def _positions(trades, cutoff):
    agg = {}
    for t in trades:
        if t.ts >= cutoff:
            break
        k = (t.wallet, t.mint)
        a = agg.get(k)
        if a is None:
            a = agg[k] = [0.0, 0.0, 0.0, 0.0, None, None, 0, 0.0]  # buy_usd, buy_tok, sell_usd, sell_tok, fts, fpx, n, buy_sol
        a[6] += 1
        if t.side == "buy":
            a[0] += t.usd
            a[1] += t.tok
            if a[4] is None:
                a[4], a[5] = t.ts, t.px
        else:
            a[2] += t.usd
            a[3] += t.tok
    return agg


def _influence(ds, cutoff, win_s):
    """(wallet, mint) -> (followers, follow_usd, lift_usd) for first buys whose window closed before cutoff
    and where pool-wide data covers the window."""
    out = {}
    for mint, lst in ds.by_mint.items():
        if not ds.pool_ts.get(mint):
            continue
        pool = [t for t in lst if t.pool and t.side == "buy" and t.ts < cutoff]
        if len(pool) < 5:
            continue
        pts = [t.ts for t in pool]
        first = {}
        for t in pool:
            first.setdefault(t.wallet, t.ts)
        firsts = sorted((ts, w) for w, ts in first.items() if w not in ds.bots)
        fts = [x[0] for x in firsts]
        for w, t0 in first.items():
            if t0 + win_s >= cutoff or w in ds.bots:
                continue
            if not ds.pool_covered(mint, t0, t0 + win_s, max_gap=win_s):
                continue
            i, j = bisect.bisect_right(fts, t0), bisect.bisect_right(fts, t0 + win_s)
            followers = sum(1 for k in range(i, j) if firsts[k][1] != w)
            a, b = bisect.bisect_right(pts, t0), bisect.bisect_right(pts, t0 + win_s)
            follow_usd = sum(pool[k].usd for k in range(a, b) if pool[k].wallet != w)
            c = bisect.bisect_left(pts, t0 - win_s)
            before_usd = sum(pool[k].usd for k in range(c, bisect.bisect_left(pts, t0)) if pool[k].wallet != w)
            out[(w, mint)] = (followers, follow_usd, follow_usd - before_usd)
    return out


def compute(ds, cutoff, min_position_usd=None):
    """wallet -> stats dict (raw numbers + components 0..1). Bots and wallets without positions are skipped."""
    min_pos = settings.min_position_usd if min_position_usd is None else min_position_usd
    agg = _positions(ds.trades, cutoff)
    infl = _influence(ds, cutoff, rsettings.influence_window_min * 60)
    pidx = ds.pidx
    bots = set(ds.bots)
    per = defaultdict(list)
    for (w, m), a in agg.items():
        if a[6] > settings.bot_max_trades_per_token:
            bots.add(w)
        if a[1] <= 0 or a[0] < min_pos:
            continue
        buy_usd, buy_tok, sell_usd, sell_tok, fts, fpx, n, _ = a
        avg = buy_usd / buy_tok
        sold = min(sell_tok, buy_tok)
        sold_usd = sell_usd * (sold / sell_tok) if sell_tok else 0.0
        realized = sold_usd - avg * sold
        px_now = pidx.price_at(m, cutoff - 1) or 0.0
        remaining = buy_tok - sold
        lo = pidx.min_between(m, fts, cutoff - 1)
        crashed = bool(fpx and lo is not None and lo <= 0.1 * fpx)
        if crashed and px_now and px_now <= 0.15 * fpx:
            px_now = 0.0  # treat as rugged: holdings worth nothing
        unreal = remaining * px_now - avg * remaining
        pnl = realized + unreal
        peak = pidx.max_between(m, fts, cutoff - 1)
        peak_mult = (max(peak, px_now) / fpx) if fpx and peak else None
        t2x = pidx.first_time_reaching(m, fts, cutoff - 1, 2 * fpx) if fpx else None
        per[w].append(dict(
            mint=m, buy_usd=buy_usd, pnl=pnl, realized=realized, roi=pnl / buy_usd, ts=fts,
            peak=peak_mult, min_to_2x=(t2x - fts) / 60 if t2x else None,
            entry_mcap=pidx.mcap(m, fpx), rug=crashed and sell_usd < 0.5 * buy_usd,
            infl=infl.get((w, m))))
    out = {}
    for w, P in per.items():
        if w in bots:
            continue
        out[w] = summarize(P)
    return out, bots


def summarize(P):
    n = len(P)
    wins = sum(1 for p in P if p["roi"] > 0.05)
    early = sum(1 for p in P if (p["peak"] or 0) >= 2)
    rugs = sum(1 for p in P if p["rug"])
    total_buy = sum(p["buy_usd"] for p in P)
    total_pnl = sum(p["pnl"] for p in P)
    roi = total_pnl / total_buy if total_buy else 0.0
    pos = [p["pnl"] for p in P if p["pnl"] > 0]
    conc = 1 - max(pos) / sum(pos) if pos and total_pnl > 0 else 0.0
    srt = sorted(P, key=lambda p: p["ts"])
    halves = [srt[:n // 2], srt[n // 2:]] if n >= 4 else [srt]
    stable = sum(1 for h in halves if sum(p["pnl"] for p in h) > 0) / len(halves)
    consistency = min(1.0, 0.5 * min(1.0, 2 * conc) + 0.5 * stable) if total_pnl > 0 else 0.25 * stable
    inf = [p["infl"] for p in P if p["infl"]]
    k = len(inf)
    med_f = median([x[0] for x in inf]) if inf else None
    med_usd = median([x[1] for x in inf]) if inf else None
    med_lift = median([x[2] for x in inf]) if inf else None
    infl_raw = 1 - math.exp(-(med_f or 0) / 5)
    infl_c = (k * infl_raw + 3 * 0.3) / (k + 3)
    mc = median([p["entry_mcap"] for p in P if p["entry_mcap"]])
    timing = min(1.0, max(0.0, 1 - (math.log10(mc) - 4) / 3)) if mc else 0.5
    comps = dict(
        win=(wins + 1) / (n + 2), early=(early + 0.5) / (n + 2), roi=0.5 + 0.5 * math.tanh(roi),
        consistency=consistency, timing=timing, influence=infl_c, rug_avoid=1 - (rugs + 0.5) / (n + 2))
    return dict(
        n_tokens=n, win_rate=wins / n, early_rate=early / n, roi=roi,
        realized_pnl_usd=sum(p["realized"] for p in P), total_pnl_usd=total_pnl,
        median_entry_mcap=mc, median_min_to_2x=median([p["min_to_2x"] for p in P]),
        influence_followers=med_f, influence_usd=med_usd, inflow_lift_usd=med_lift, influence_samples=k,
        consistency=consistency, rug_rate=rugs / n, components=comps,
        last_ts=max(p["ts"] for p in P))


def first_buys(ds, a, b):
    """(wallet, mint, ts, price) of each wallet's first buy in a coin with a <= ts < b."""
    seen = set()
    out = []
    for t in ds.trades:
        if t.side != "buy":
            continue
        k = (t.wallet, t.mint)
        if k in seen:
            continue
        seen.add(k)
        if a <= t.ts < b and t.px > 0:
            out.append((t.wallet, t.mint, t.ts, t.px))
    return out
