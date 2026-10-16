"""Market dynamics on top of the snapshots: regime with hysteresis, trend and transitions, narrative life
cycle, sector rotation lead (does share growth predict the next hour?), and per-component predictive power
with weights learned only when they hold out of sample. Everything is point in time (rows at or before ts)."""
import json
import math
import statistics
from collections import Counter, defaultdict

from market import store
from market.config import settings as default_cfg

ORDER = ["risk_off", "neutral", "risk_on", "hot"]
HYST = 3.0           # points beyond a threshold before the regime switches (no flapping around 35 / 55 / 75)
TREND_FAST = 8.0     # points per hour that count as a clear move


# ---------------------------------------------------------------- regime
def thresholds(cfg=default_cfg):
    return [("risk_off", cfg.riskoff_below), ("risk_on", cfg.riskon_above), ("hot", cfg.hot_above)]


def label_hyst(prev, score, cfg=default_cfg):
    """Regime label with hysteresis: keep `prev` until the score is HYST points past the boundary."""
    from market.analysis import label_for
    raw = label_for(score, cfg)
    if prev is None or raw is None or raw == prev:
        return raw
    lo = {"risk_off": -1e9, "neutral": cfg.riskoff_below, "risk_on": cfg.riskon_above, "hot": cfg.hot_above}
    hi = {"risk_off": cfg.riskoff_below, "neutral": cfg.riskon_above, "risk_on": cfg.hot_above, "hot": 1e9}
    if lo[prev] - HYST <= score < hi[prev] + HYST:
        return prev
    return raw


def slope(points, hours):
    """Least-squares slope in points per hour over the last `hours` of (ts, score); None with < 3 points."""
    if not points:
        return None
    t_end = points[-1][0]
    pts = [(t, s) for t, s in points if t >= t_end - hours * 3600 and s is not None]
    if len(pts) < 3 or pts[-1][0] - pts[0][0] < hours * 1800:
        return None
    mt = statistics.fmean(t for t, _ in pts)
    ms = statistics.fmean(s for _, s in pts)
    var = sum((t - mt) ** 2 for t, _ in pts)
    if var <= 0:
        return None
    return sum((t - mt) * (s - ms) for t, s in pts) / var * 3600


def ema(points, half_life_s=1800):
    out, last_t = None, None
    for t, s in points:
        if s is None:
            continue
        if out is None:
            out = s
        else:
            a = 1 - 0.5 ** ((t - last_t) / half_life_s)
            out += a * (s - out)
        last_t = t
    return out


def regime(conn, ts, score, cfg=default_cfg):
    """-> {regime, regime_since, score_ema, slope_1h, slope_3h, trend} from the snapshots before ts plus `score`."""
    hist = []
    if store.has(conn, "market_snapshots"):
        cols = {r[1] for r in conn.execute("PRAGMA table_info(market_snapshots)")}
        sel = "ts, score, regime, regime_since" if "regime" in cols else "ts, score, NULL, NULL"
        hist = [tuple(r) for r in conn.execute(
            f"SELECT {sel} FROM market_snapshots WHERE ts<? AND ts>=? ORDER BY ts", (ts, ts - 26 * 3600))]
    pts = [(t, s) for t, s, _, _ in hist] + [(ts, score)]
    prev = next((r for r in reversed(hist) if r[1] is not None), None)
    prev_regime = prev[2] if prev and prev[2] else None
    if prev and prev_regime is None:  # rows from before this column existed: rebuild the regime once
        lab = None
        for _, s, _, _ in hist:
            if s is not None:
                lab = label_hyst(lab, s, cfg)
        prev_regime = lab
    reg = label_hyst(prev_regime, score, cfg) if score is not None else prev_regime
    since = ts
    if prev and reg == prev_regime and prev[3]:
        since = prev[3]
    elif prev and reg == prev_regime:
        since = next((t for t, s, r, _ in hist if r == reg), prev[0])
    s1, s3 = slope(pts, 1), slope(pts, 3)
    trend = "stabil"
    if s1 is not None:
        if s1 >= TREND_FAST:
            trend = "steigt"
        elif s1 <= -TREND_FAST:
            trend = "fällt"
        if score is not None and s1 is not None:
            # about to cross into the next regime within an hour at this pace
            nxt = {"risk_off": cfg.riskoff_below + HYST, "neutral": cfg.riskon_above + HYST, "risk_on": cfg.hot_above + HYST}
            prv = {"neutral": cfg.riskoff_below - HYST, "risk_on": cfg.riskon_above - HYST, "hot": cfg.hot_above - HYST}
            if s1 > 0 and reg in nxt and score + s1 >= nxt[reg]:
                trend = "kippt nach oben"
            elif s1 < 0 and reg in prv and score + s1 <= prv[reg]:
                trend = "kippt nach unten"
    return {"regime": reg, "regime_since": since, "score_ema": None if score is None else round(ema(pts), 2),
            "slope_1h": None if s1 is None else round(s1, 2), "slope_3h": None if s3 is None else round(s3, 2),
            "trend": trend}


def transitions(conn, ts, days=14):
    """Empirical next-hour regime transitions from hourly samples (Laplace +1): {from: {to: p}}, counts, n."""
    if not store.has(conn, "market_snapshots"):
        return {"n": 0, "p": {}, "counts": {}}
    cols = {r[1] for r in conn.execute("PRAGMA table_info(market_snapshots)")}
    col = "COALESCE(regime, label)" if "regime" in cols else "label"
    hourly = {}
    for t, lab in conn.execute(f"SELECT ts, {col} FROM market_snapshots WHERE ts<=? AND ts>=? AND score IS NOT NULL "
                               "ORDER BY ts", (ts, ts - days * 86400)):
        hourly[t // 3600] = lab
    counts = defaultdict(Counter)
    for h, lab in hourly.items():
        nxt = hourly.get(h + 1)
        if lab and nxt:
            counts[lab][nxt] += 1
    p = {}
    for a in ORDER:
        tot = sum(counts[a].values())
        p[a] = {b: round((counts[a][b] + 1) / (tot + len(ORDER)), 3) for b in ORDER}
    return {"n": sum(sum(c.values()) for c in counts.values()), "p": p,
            "counts": {a: dict(c) for a, c in counts.items()}}


# ---------------------------------------------------------------- narrative life cycle
STAGES = {"new": "neu", "growing": "wachsend", "peak": "Höhepunkt", "mature": "reif", "fading": "abklingend"}


def term_history(conn, ts, hours=48):
    """{term: [(ts, n_recent), ...]} of earlier rounds (before ts)."""
    out = defaultdict(list)
    if not store.has(conn, "market_terms"):
        return out
    for t, term, n in conn.execute("SELECT ts, term, n_recent FROM market_terms WHERE ts<? AND ts>=? ORDER BY ts",
                                   (ts, ts - hours * 3600)):
        out[term].append((t, n))
    return out


def lifecycle(term_row, hist, ts):
    """Stage of one narrative: new (first seen < 1 h ago), growing (+15 % vs ~1 h ago), fading (< 50 % of its
    peak), peak (>= 80 % of the peak), else mature. Adds first_seen, peak_n, age_h, growth_1h."""
    n = term_row["n_recent"]
    h = hist or []
    first = h[0][0] if h else ts
    peak = max([x for _, x in h] + [n])
    back = [x for t, x in h if ts - 5400 <= t <= ts - 2700]
    prev = back[-1] if back else None
    growth = (n / prev - 1) if prev else None
    if ts - first < 3600:
        stage = "new"
    elif n < 0.5 * peak:
        stage = "fading"
    elif growth is not None and growth >= 0.15:
        stage = "growing"
    elif n >= 0.8 * peak:
        stage = "peak"
    else:
        stage = "mature"
    return {"stage": stage, "first_seen": first, "peak_n": peak, "age_h": round((ts - first) / 3600, 2),
            "growth_1h": None if growth is None else round(growth, 3)}


# ---------------------------------------------------------------- statistics helpers
def _rank(xs):
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    r = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for k in range(i, j + 1):
            r[order[k]] = (i + j) / 2
        i = j + 1
    return r


def spearman(xs, ys):
    pairs = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    if len(pairs) < 5:
        return None
    rx, ry = _rank([p[0] for p in pairs]), _rank([p[1] for p in pairs])
    mx, my = statistics.fmean(rx), statistics.fmean(ry)
    sx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    sy = math.sqrt(sum((b - my) ** 2 for b in ry))
    if not sx or not sy:
        return None
    return sum((a - mx) * (b - my) for a, b in zip(rx, ry)) / (sx * sy)


def _tstat(ics):
    ics = [x for x in ics if x is not None]
    if len(ics) < 3:
        return None
    sd = statistics.pstdev(ics)
    return statistics.fmean(ics) / sd * math.sqrt(len(ics)) if sd else None


# ---------------------------------------------------------------- sector rotation lead
LEAD_FACTORS = {"vol_share_chg": "Volumen-Anteil wächst", "heat": "Hitze", "smart_net_usd": "Smart Money netto",
                "net_flow_usd": "Netto-Kauf geschätzt", "median_ret_h1": "Momentum 1 h (Fortsetzung)"}


def sector_lead(conn, ts, days=7, horizon=3600, min_sectors=5):
    """Does a sector factor at t predict the sector's median 1 h return at t + 1 h? Cross-sectional Spearman IC per
    hour, averaged (factor-IC method). Older and newer half are reported separately; a factor counts as a
    lead only when both halves have the same sign and the t-stat is >= 2."""
    if not store.has(conn, "market_sectors"):
        return {}
    rows = defaultdict(dict)
    for r in conn.execute("SELECT ts, sector, vol_share_chg, heat, smart_net_usd, net_flow_usd, median_ret_h1 "
                          "FROM market_sectors WHERE ts<=? AND ts>=? AND sector!='other'", (ts, ts - days * 86400 - horizon)):
        rows[r["ts"]][r["sector"]] = dict(r)
    times = sorted(rows)
    hourly = {}
    for t in times:
        hourly.setdefault(t // 3600, t)
    out = {}
    for f, label in LEAD_FACTORS.items():
        ics = []
        for t in sorted(hourly.values()):
            target_t = min((x for x in times if abs(x - (t + horizon)) <= 600), key=lambda x: abs(x - t - horizon), default=None)
            if target_t is None or target_t > ts:
                continue
            now, later = rows[t], rows[target_t]
            common = [s for s in now if s in later and now[s].get(f) is not None and later[s].get("median_ret_h1") is not None]
            if len(common) < min_sectors:
                continue
            ics.append((t, spearman([now[s][f] for s in common], [later[s]["median_ret_h1"] for s in common])))
        ics = [(t, x) for t, x in ics if x is not None]
        vals = [x for _, x in ics]
        half = len(vals) // 2
        a, b = vals[:half], vals[half:]
        mean = statistics.fmean(vals) if vals else None
        t_all = _tstat(vals)
        ok = bool(vals) and len(vals) >= 12 and t_all is not None and abs(t_all) >= 2 and a and b \
            and (statistics.fmean(a) > 0) == (statistics.fmean(b) > 0) == (mean > 0)
        out[f] = {"label": label, "n_hours": len(vals), "ic": None if mean is None else round(mean, 3),
                  "t": None if t_all is None else round(t_all, 2),
                  "ic_old": round(statistics.fmean(a), 3) if a else None, "ic_new": round(statistics.fmean(b), 3) if b else None,
                  "confirmed": ok}
    return out


def rotation_streaks(conn, ts, rounds=12):
    """Sectors whose volume share grew (or shrank) in each of the last n rounds: {sector: +n / -n}."""
    if not store.has(conn, "market_sectors"):
        return {}
    times = [r[0] for r in conn.execute("SELECT DISTINCT ts FROM market_sectors WHERE ts<=? AND vol_share_chg IS NOT NULL "
                                        "ORDER BY ts DESC LIMIT ?", (ts, rounds))]
    if not times:
        return {}
    by = defaultdict(dict)
    q = f"SELECT ts, sector, vol_share_chg FROM market_sectors WHERE ts IN ({','.join('?' * len(times))})"
    for t, s, c in conn.execute(q, times):
        by[s][t] = c
    out = {}
    for s, d in by.items():
        n = 0
        sign = None
        for t in times:
            c = d.get(t)
            if c is None or c == 0:
                break
            sg = c > 0
            if sign is None:
                sign = sg
            if sg != sign:
                break
            n += 1
        if n:
            out[s] = n if sign else -n
    return out


# ---------------------------------------------------------------- per-component predictive power
def launch_outcomes_after(conn, since, until):
    """Launches created in [since, until) with a known 60 min outcome: (created_ts, hit)."""
    if not store.has(conn, "sniper_launches"):
        return []
    return [(r[0], int((r[1] or 0) >= 1.0)) for r in conn.execute(
        "SELECT created_ts, max_ret_60m FROM sniper_launches WHERE created_ts>=? AND created_ts<? AND max_ret_60m IS NOT NULL",
        (since, until))]


def component_power(conn, ts, days=14, min_launches=20):
    """For every phase input and the phase score itself: Spearman IC between its value at an hourly snapshot and
    the 2x rate of launches created in the following hour (never overlaps the 1-6 h old launches the phase
    itself uses). Older/newer half separately. Returns {rows: {name: {...}}, n_hours, learned: weights | None}."""
    if not store.has(conn, "market_snapshots"):
        return {"n_hours": 0, "rows": {}, "learned": None}
    snaps = {}
    for r in conn.execute("SELECT ts, score, components_json FROM market_snapshots WHERE ts<=? AND ts>=? "
                          "AND score IS NOT NULL ORDER BY ts", (ts - 3600, ts - days * 86400)):
        snaps.setdefault(r[0] // 3600, r)
    import bisect
    launches = sorted(launch_outcomes_after(conn, ts - days * 86400, ts))
    lt = [t for t, _ in launches]
    data = []
    for h, r in sorted(snaps.items()):
        t = r[0]
        i, j = bisect.bisect_left(lt, t), bisect.bisect_left(lt, t + 3600)
        out = [hit for _, hit in launches[i:j]]
        if len(out) < min_launches:
            continue
        comps = json.loads(r[2] or "{}")
        vals = {k: v.get("value") for k, v in comps.items()}
        vals["score"] = r[1]
        data.append((t, vals, sum(out) / len(out)))
    names = sorted({k for _, v, _ in data for k in v})
    half = len(data) // 2
    rows = {}
    for k in names:
        xs = [v.get(k) for _, v, _ in data]
        ys = [y for _, _, y in data]
        ic = spearman(xs, ys)
        ic_a = spearman(xs[:half], ys[:half])
        ic_b = spearman(xs[half:], ys[half:])
        n = sum(1 for x in xs if x is not None)
        ok = ic is not None and ic_a is not None and ic_b is not None and n >= 24 and \
            (ic_a > 0) == (ic_b > 0) == (ic > 0) and abs(ic) * math.sqrt(max(1, n - 2)) >= 2
        rows[k] = {"n_hours": n, "ic": None if ic is None else round(ic, 3),
                   "ic_old": None if ic_a is None else round(ic_a, 3), "ic_new": None if ic_b is None else round(ic_b, 3),
                   "confirmed": ok}
    return {"n_hours": len(data), "rows": rows, "learned": learn_weights(data, rows)}


def learn_weights(data, rows, shrink=0.5):
    """Weights from the older half only: prior x (1 + IC) for inputs with a positive IC there, tested on the
    newer half against the fixed weights (IC of each score with the next-hour 2x rate). Adopted only when the
    learned score beats the fixed one out of sample. shrink pulls towards the prior (no overfitting)."""
    from market.analysis import PHASE_WEIGHTS
    if len(data) < 24:
        return None
    half = len(data) // 2
    old, new = data[:half], data[half:]
    w = {}
    for k, prior in PHASE_WEIGHTS.items():
        ic = spearman([v.get(k) for _, v, _ in old], [y for _, _, y in old])
        adj = 1 + (ic or 0) * 2
        w[k] = max(0.0, prior * ((1 - shrink) * max(0.0, adj) + shrink))
    tot = sum(w.values()) or 1.0
    w = {k: v / tot for k, v in w.items()}

    def score(vals, weights):
        have = {k: x for k, x in vals.items() if k in weights and x is not None}
        s = sum(weights[k] for k in have)
        return None if not s else sum(weights[k] * x for k, x in have.items()) / s

    ys = [y for _, _, y in new]
    ic_fixed = spearman([score(v, PHASE_WEIGHTS) for _, v, _ in new], ys)
    ic_learned = spearman([score(v, w) for _, v, _ in new], ys)
    better = ic_learned is not None and ic_fixed is not None and ic_learned > ic_fixed + 0.05 and ic_learned > 0
    return {"weights": {k: round(v, 4) for k, v in w.items()}, "ic_fixed_new": None if ic_fixed is None else round(ic_fixed, 3),
            "ic_learned_new": None if ic_learned is None else round(ic_learned, 3), "adopt": better,
            "n_old": len(old), "n_new": len(new)}
