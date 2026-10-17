"""Which exit turns model signals into profit after costs? Replays a grid of exit rules on the walk-forward
events (only prices after the entry, cost per round trip from MODEL_COST_ROUNDTRIP).

Rule = take profit (tp), stop loss (sl, None = none), maximum hold. Exit at the first level touched; when the
take profit and the stop are touched in the same candle the stop counts (conservative). Otherwise exit at the
price when the hold time ends. A rule is chosen on the older 60 % of events and judged on the newer 40 %, so the
reported result is out of sample.
"""
from research.config import rsettings

TPS = (0.15, 0.25, 0.5, 1.0, 2.0)
SLS = (None, -0.2, -0.35, -0.5)
HOLDS = (("15m", 900), ("1h", 3600), ("6h", 21600))
GRID = [dict(tp=tp, sl=sl, hold=h, hold_s=hs) for tp in TPS for sl in SLS for h, hs in HOLDS]


def replay(pidx, mint, t, rule):
    """Net return of one rule after costs, or None when prices do not cover the hold time yet."""
    p0 = pidx.price_at(mint, t)
    end_t = t + rule["hold_s"]
    if not p0 or pidx.coverage_until(mint) < end_t:
        return None
    t_tp = pidx.first_time_reaching(mint, t, end_t, p0 * (1 + rule["tp"]))
    t_sl = pidx.first_time_below(mint, t, end_t, p0 * (1 + rule["sl"])) if rule["sl"] is not None else None
    if t_sl is not None and (t_tp is None or t_sl <= t_tp):
        gross = rule["sl"]
    elif t_tp is not None:
        gross = rule["tp"]
    else:
        end = pidx.price_at(mint, end_t)
        gross = end / p0 - 1 if end else -1.0
    return gross - rsettings.cost_roundtrip


def _mean(xs):
    return sum(xs) / len(xs) if xs else None


def study(events, pidx, score_key="p", top_share=0.2, min_n=30):
    """events: dicts with mint, ts and a score (walk-forward prediction). Returns the rule table for all events
    and for the top `top_share` by score, plus the out-of-sample check of the rule picked on the older part."""
    evs = sorted((e for e in events if e.get(score_key) is not None), key=lambda e: e["ts"])
    if len(evs) < min_n:
        return {}
    k = max(1, int(len(evs) * top_share))
    thr = sorted((e[score_key] for e in evs), reverse=True)[k - 1]
    nets = {}
    for i, r in enumerate(GRID):
        nets[i] = [replay(pidx, e["mint"], e["ts"] + rsettings.entry_delay_s, r) for e in evs]  # realistic entry

    def table(sel):
        rows = []
        for i, r in enumerate(GRID):
            xs = [nets[i][j] for j in sel if nets[i][j] is not None]
            if len(xs) >= 10:
                rows.append(dict(rule=r, n=len(xs), mean_net=_mean(xs), win_rate=sum(1 for x in xs if x > 0) / len(xs)))
        return sorted(rows, key=lambda d: -d["mean_net"])

    all_idx = list(range(len(evs)))
    top_idx = [j for j, e in enumerate(evs) if e[score_key] >= thr]
    cut = int(len(evs) * 0.6)
    old_top = [j for j in top_idx if j < cut]
    new_top = [j for j in top_idx if j >= cut]
    oos = None
    # best older-part rule that can be judged on the newer part (long holds of recent signals may not be
    # complete yet; skipping to the next rule keeps the check instead of silently dropping it)
    for cand in table(old_top):
        i = GRID.index(cand["rule"])
        xs = [nets[i][j] for j in new_top if nets[i][j] is not None]
        if len(xs) >= 10:
            oos = dict(rule=cand["rule"], n=len(xs), mean_net=_mean(xs),
                       win_rate=sum(1 for x in xs if x > 0) / len(xs), in_sample_mean=cand["mean_net"])
            break
    return dict(n=len(evs), n_top=len(top_idx), all=table(all_idx)[:5], top=table(top_idx)[:5], out_of_sample=oos)
