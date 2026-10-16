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

COMPONENTS = ("win", "early", "roi", "consistency", "timing", "lead", "influence", "rug_avoid", "exit")
COMPONENT_LABELS = {
    "win": "Trefferquote", "early": "Frühe Einstiege (danach ≥2x)", "roi": "Rendite (ROI)",
    "consistency": "Konstanz", "timing": "Einstieg bei niedriger Market Cap",
    "lead": "Kauft vor der Masse", "influence": "Einfluss (Follower)",
    "rug_avoid": "Rug-Vermeidung", "exit": "Ausstiegs-Qualität (verkauft nahe am Hoch)",
}


def _positions(trades, cutoff):
    agg = {}
    for t in trades:
        if t.ts >= cutoff:
            break
        k = (t.wallet, t.mint)
        a = agg.get(k)
        if a is None:
            a = agg[k] = [0.0, 0.0, 0.0, 0.0, None, None, 0, None]  # buy_usd, buy_tok, sell_usd, sell_tok, fts, fpx, n, first sell ts
        a[6] += 1
        if t.side == "buy":
            a[0] += t.usd
            a[1] += t.tok
            if a[4] is None:
                a[4], a[5] = t.ts, t.px
        else:
            a[2] += t.usd
            a[3] += t.tok
            if a[7] is None:
                a[7] = t.ts
    return agg


def _influence(ds, cutoff, win_s):
    """(wallet, mint) -> (followers, follow_usd, lift_usd, entry_rank) for first buys whose window closed
    before cutoff and where pool-wide data covers the window. entry_rank: 0 = first buyer of the coin seen,
    1 = last one (share of known buyers that came before this wallet)."""
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
        nfirst = max(1, len(firsts) - 1)
        for w, t0 in first.items():
            if t0 + win_s >= cutoff or w in ds.bots:
                continue
            if not ds.pool_covered(mint, t0, t0 + win_s, max_gap=win_s):
                continue
            i, j = bisect.bisect_right(fts, t0), bisect.bisect_right(fts, t0 + win_s)
            followers = sum(1 for k in range(i, j) if firsts[k][1] != w)
            # new buyers in the same span BEFORE the buy = the coin's normal traffic; influence is the excess
            base = bisect.bisect_left(fts, t0) - bisect.bisect_left(fts, t0 - win_s)
            followers = max(0, followers - base)
            a, b = bisect.bisect_right(pts, t0), bisect.bisect_right(pts, t0 + win_s)
            follow_usd = sum(pool[k].usd for k in range(a, b) if pool[k].wallet != w)
            c = bisect.bisect_left(pts, t0 - win_s)
            before_usd = sum(pool[k].usd for k in range(c, bisect.bisect_left(pts, t0)) if pool[k].wallet != w)
            rank = bisect.bisect_left(fts, t0) / nfirst
            out[(w, mint)] = (followers, follow_usd, follow_usd - before_usd, min(1.0, rank))
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
        buy_usd, buy_tok, sell_usd, sell_tok, fts, fpx, n, sell_ts = a
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
        capture = hold = None
        if sell_tok >= 0.5 * buy_tok and sell_tok > 0 and peak and fpx:
            capture = min(1.0, (sell_usd / sell_tok) / max(peak, fpx))  # sold at what share of the high
            hold = (sell_ts - fts) / 60 if sell_ts and fts else None
        per[w].append(dict(
            mint=m, buy_usd=buy_usd, pnl=pnl, realized=realized, roi=pnl / buy_usd, ts=fts,
            peak=peak_mult, min_to_2x=(t2x - fts) / 60 if t2x else None,
            entry_mcap=pidx.mcap(m, fpx), rug=crashed and sell_usd < 0.5 * buy_usd,
            infl=infl.get((w, m)), capture=capture, hold=hold))
    out = {}
    for w, P in per.items():
        if w in bots:
            continue
        out[w] = summarize(P)
    return out, bots


def summarize(P):
    """Everything after costs where it decides: a win is a position whose return beats the round-trip cost,
    ROI and the tier PnL rule use net PnL (PnL minus cost x invested)."""
    n = len(P)
    cost = rsettings.cost_roundtrip
    wins = sum(1 for p in P if p["roi"] > cost)
    early = sum(1 for p in P if (p["peak"] or 0) >= 2)
    rugs = sum(1 for p in P if p["rug"])
    total_buy = sum(p["buy_usd"] for p in P)
    total_pnl = sum(p["pnl"] for p in P)
    roi = total_pnl / total_buy if total_buy else 0.0
    net_pnl = total_pnl - cost * total_buy
    net_roi = net_pnl / total_buy if total_buy else 0.0
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
    infl_raw = 1 - math.exp(-(med_f or 0) / 4)
    infl_c = (k * infl_raw + 3 * 0.3) / (k + 3)
    med_rank = median([x[3] for x in inf]) if inf else None
    lead = (k * (1 - med_rank) + 3 * 0.5) / (k + 3) if inf else 0.5
    caps = [p["capture"] for p in P if p["capture"] is not None]
    exit_c = (sum(caps) + 2 * 0.4) / (len(caps) + 2)
    mc = median([p["entry_mcap"] for p in P if p["entry_mcap"]])
    timing = min(1.0, max(0.0, 1 - (math.log10(mc) - 4) / 3)) if mc else 0.5
    comps = dict(
        win=(wins + 1) / (n + 2), early=(early + 0.5) / (n + 2), roi=0.5 + 0.5 * math.tanh(net_roi),
        consistency=consistency, timing=timing, lead=lead, influence=infl_c, exit=exit_c, rug_avoid=1 - (rugs + 0.5) / (n + 2))
    return dict(
        n_tokens=n, win_rate=wins / n, early_rate=early / n, roi=roi,
        realized_pnl_usd=sum(p["realized"] for p in P), total_pnl_usd=total_pnl,
        net_pnl_usd=net_pnl, net_roi=net_roi,
        median_entry_mcap=mc, median_min_to_2x=median([p["min_to_2x"] for p in P]),
        influence_followers=med_f, influence_usd=med_usd, inflow_lift_usd=med_lift, influence_samples=k, median_entry_rank=med_rank,
        exit_capture=median(caps) if caps else None, median_hold_min=median([p["hold"] for p in P]),
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


def clusters(ds, wallets, cutoff, window_s=60, min_shared=2):
    """Group wallets that repeatedly buy the same coin within `window_s` seconds of each other (same entity,
    bundles, shared signal groups), or that were funded by the same address (ds.funding, exchanges/hubs
    excluded, funding before `cutoff` only). Returns wallet -> cluster id (smallest member address)."""
    wallets = set(wallets)
    pair = {}
    for mint, lst in ds.by_mint.items():
        firsts = {}
        for t in lst:
            if t.ts >= cutoff:
                break
            if t.side == "buy" and t.wallet in wallets and t.wallet not in firsts:
                firsts[t.wallet] = t.ts
        if len(firsts) < 2:
            continue
        fl = sorted(firsts.items(), key=lambda x: x[1])
        for i, (a, ta) in enumerate(fl):
            for b, tb in fl[i + 1:]:
                if tb - ta > window_s:
                    break
                k = (a, b) if a < b else (b, a)
                pair[k] = pair.get(k, 0) + 1
    parent = {}

    def find(x):
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent[x], parent[x])
            x = parent[x]
        return x
    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)
    for (a, b), c in pair.items():
        if c >= min_shared:
            union(a, b)
    funding = getattr(ds, "funding", None) or {}
    if funding:
        from research.funding import hubs
        skip = hubs(funding)
        by_funder = {}
        for w in wallets:
            f = funding.get(w)
            if f and f[0] not in skip and f[1] < cutoff:
                by_funder.setdefault(f[0], []).append(w)
                if f[0] in wallets:
                    union(w, f[0])
        for ws in by_funder.values():
            for w in ws[1:]:
                union(ws[0], w)
    return {w: find(w) for w in wallets}
