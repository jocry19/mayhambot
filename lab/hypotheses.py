"""The fixed hypotheses of the test bench. Each family reads the database (point in time: only what was known at
the entry) and returns entries with their gross result on the recorded price path; costs come later (costs.py).

Entry rows: hyp, mint, t (entry, unix s), dex, liq (USD at entry), kind (launch | copy | market), gross (share,
-1.0 = total loss; a low under 10 % of the entry counts as a rug = -1.0).
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

RUG = 0.1
CAP = 10.0
GRID_S = 300  # one decision point per coin every 5 min
ENTRY_DELAY_S = 60  # entry at the first snapshot at least a minute after the decision point
ENTRY_TOL_S = 240
COLS = ["hyp", "mint", "t", "dex", "liq", "kind", "gross"]


def _empty() -> pd.DataFrame:
    return pd.DataFrame(columns=COLS)


def load_prices(conn, since: int) -> pd.DataFrame:
    df = pd.read_sql("SELECT mint, ts, price_usd p, liquidity_usd liq FROM token_prices WHERE ts>=? AND price_usd>0",
                     conn, params=(since,))
    return df.sort_values(["mint", "ts"], kind="stable")


def _exit(ts, p, j, horizon_s, tp=None):
    """Gross result of an entry at index j held horizon_s, optionally sold at the first snapshot >= +tp."""
    pe = p[j]
    k = np.searchsorted(ts, ts[j] + horizon_s, side="right") - 1
    if ts[-1] < ts[j] + horizon_s - 600:  # the path ends early: unknown, not a win
        return None
    seg = p[j:k + 1]
    if seg.min() <= RUG * pe:
        return -1.0
    if tp is not None:
        hit = np.nonzero(seg >= pe * (1 + tp))[0]
        if len(hit):
            return min(CAP, seg[hit[0]] / pe - 1)
    return min(CAP, p[k] / pe - 1)


def panel(conn, since: int, prices: pd.DataFrame | None = None) -> pd.DataFrame:
    """Decision points of every followed coin (token_prices) with what was known then: liquidity, change over 5 and
    60 min, distance to the 60-min high, age of the pair, market breadth of the previous hour. Coins with fewer than
    20 snapshots or a price that did not move for an hour (dead or stale) are left out."""
    df = load_prices(conn, since) if prices is None else prices
    tok = pd.read_sql("SELECT mint, dex, pair_created_at pca FROM tokens", conn).set_index("mint")
    pca = tok.pca.where(tok.pca < 1e11, tok.pca / 1000)  # milliseconds in some rows
    rows = []
    for mint, g in df.groupby("mint", sort=False):
        if len(g) < 20:
            continue
        ts, p, lq = g.ts.to_numpy(), g.p.to_numpy(), g.liq.to_numpy()
        grid = np.arange(ts[0] + 3600, ts[-1], GRID_S)
        if not len(grid):
            continue
        i = np.searchsorted(ts, grid, side="right") - 1
        ok = grid - ts[i] <= 120
        i5 = np.searchsorted(ts, grid - 300, side="right") - 1
        i60 = np.searchsorted(ts, grid - 3600, side="right") - 1
        je = np.searchsorted(ts, grid + ENTRY_DELAY_S)
        ok &= je < len(ts)
        je = np.minimum(je, len(ts) - 1)
        ok &= ts[je] - grid <= ENTRY_TOL_S
        for n in np.nonzero(ok)[0]:
            a, b = i[n], i60[n]
            if b < 0 or grid[n] - 3600 - ts[b] > 600:
                continue
            r60 = p[a] / p[b] - 1
            r5 = p[a] / p[i5[n]] - 1 if i5[n] >= 0 else np.nan
            if r60 == 0 and r5 == 0:
                continue
            lo = np.searchsorted(ts, grid[n] - 3600)
            rows.append((mint, int(ts[je[n]]), float(lq[a] or 0), r5, r60, p[a] / p[lo:a + 1].max() - 1, int(je[n])))
    P = pd.DataFrame(rows, columns=["mint", "t", "liq", "r5", "r60", "dd60", "j"])
    if P.empty:
        return P
    P["dex"] = P.mint.map(tok.dex).fillna("")
    P["age_h"] = (P.t - P.mint.map(pca)) / 3600
    hr = P.t // 3600
    breadth = P.groupby(hr).r60.apply(lambda x: x.clip(-1, 3).mean())
    prev = breadth.shift(1)
    # top half of the hours so far (expanding, only the past): the regime brake of section 3 'Neu' 4
    rank = prev.expanding(min_periods=6).apply(lambda x: (x[:-1] < x[-1]).mean() if len(x) > 1 else np.nan, raw=True)
    P["breadth_prev"] = hr.map(prev)
    P["regime_rank"] = hr.map(rank)
    P.attrs["paths"] = {m: (g.ts.to_numpy(), g.p.to_numpy()) for m, g in df.groupby("mint", sort=False)}
    return P


PANEL_HYPS = {
    # name: (filter, horizon min, take profit)
    "ueberlebende_60m": (lambda P: (P.age_h > 6) & (P.liq > 5e4), 60, None),
    "ueberlebende_240m": (lambda P: (P.age_h > 6) & (P.liq > 5e4), 240, None),
    "ueberlebende_tp30_240m": (lambda P: (P.age_h > 6) & (P.liq > 5e4), 240, 0.3),
    "ueberlebende_regime_oben_60m": (lambda P: (P.age_h > 6) & (P.liq > 5e4) & (P.regime_rank >= 0.5), 60, None),
    "ueberlebende_regime_unten_60m": (lambda P: (P.age_h > 6) & (P.liq > 5e4) & (P.regime_rank < 0.5), 60, None),
    "nach_pump_60m": (lambda P: (P.liq > 2e4) & (P.r60 > 0.5), 60, None),
    "ruecksetzer_60m": (lambda P: (P.age_h > 6) & (P.liq > 2e4) & (P.r60 > -0.5) & (P.r60 <= -0.15), 60, None),
    "abgekuehlt_240m": (lambda P: (P.age_h > 1) & (P.liq > 2e4) & (P.r60 > -0.05) & (P.r60 <= 0.2)
                        & (P.dd60 > -0.15), 240, None),
    "jung_abgekuehlt_240m": (lambda P: (P.age_h > 1) & (P.age_h <= 6) & (P.liq > 2e4) & (P.r60 > -0.05)
                             & (P.r60 <= 0.2), 240, None),
}


def panel_entries(P: pd.DataFrame) -> pd.DataFrame:
    if P.empty:
        return _empty()
    paths = P.attrs["paths"]
    out = []
    for name, (flt, h, tp) in PANEL_HYPS.items():
        S = P[flt(P).fillna(False).astype(bool)]
        S = S.assign(blk=S.t // 7200).drop_duplicates(["mint", "blk"])  # one entry per coin per 2 h
        for r in S.itertuples():
            ts, p = paths[r.mint]
            g = _exit(ts, p, r.j, h * 60, tp)
            if g is not None:
                out.append((name, r.mint, r.t, r.dex, r.liq, "market", g))
    return pd.DataFrame(out, columns=COLS) if out else _empty()


def migration_entries(conn, since: int, prices: pd.DataFrame) -> pd.DataFrame:
    """Buying N minutes after a pump.fun migration, held 60 min."""
    M = pd.read_sql("SELECT mint, migrated_ts FROM sniper_launches WHERE migrated=1 AND migrated_ts>=?", conn,
                    params=(since,))
    if M.empty or prices.empty:
        return _empty()
    G = {m: g for m, g in prices[prices.mint.isin(set(M.mint))].groupby("mint")}
    out = []
    for r in M.itertuples():
        g = G.get(r.mint)
        if g is None:
            continue
        ts, p, lq = g.ts.to_numpy(), g.p.to_numpy(), g.liq.to_numpy()
        for d in (2, 10, 30, 60, 180):
            t = r.migrated_ts + d * 60
            j = np.searchsorted(ts, t)
            if j >= len(ts) or ts[j] - t > 300:
                continue
            gr = _exit(ts, p, j, 3600)
            if gr is not None:
                out.append((f"nach_migration_{d}m_60m", r.mint, int(ts[j]), "pumpswap", float(lq[j] or 0), "market", gr))
    return pd.DataFrame(out, columns=COLS) if out else _empty()


def _snipe(pts, dec, k, s):
    e = next((i for i, q in enumerate(pts) if q[0] >= dec), None)
    if e is None or pts[e][1] <= 0:
        return None
    pe = pts[e][1]
    for _, cl, lo, hi in pts[e + 1:]:
        if lo <= pe * RUG and s >= 0.9:
            return -1.0
        if lo <= pe * (1 - s):
            return max(-1.0, (1 - s) * 0.97 - 1)  # stop filled 3 % under the mark
        if hi >= pe * k:
            return min(CAP, k - 1)
    return -1.0 if pts[-1][1] <= pe * RUG else min(CAP, pts[-1][1] / pe - 1)


SNIPE_BANDS = ((30, 50), (50, 100), (100, 200), (200, 400))
SNIPE_EXITS = ((2, 0.3), (3, 0.5), (10, 0.3), (1e9, 0.99))


def snipe_entries(conn, since: int) -> pd.DataFrame:
    """Launches bought at the sniper's decision (sniper_paths, market cap in SOL), by market cap band at the
    decision, with a take profit x k and a stop s (k 1e9 / s 0.99 = held to the end of the path)."""
    S = pd.read_sql("SELECT p.mint, p.created_ts, p.decided_ts, p.points_json, l.mcap_decide_sol mcd, "
                    "l.sol_usd FROM sniper_paths p JOIN sniper_launches l USING(mint) WHERE p.created_ts>=?",
                    conn, params=(since,))
    out = []
    for r in S.itertuples():
        mcd = r.mcd or 0
        band = next((b for b in SNIPE_BANDS if b[0] <= mcd < b[1]), None)
        if band is None:
            continue
        try:
            pts = json.loads(r.points_json)
        except (TypeError, ValueError):
            continue
        if len(pts) < 2:
            continue
        liq = mcd * (r.sol_usd or 180) * 0.3  # curve depth, rough: the impact share is small at 0.1 SOL
        for k, s in SNIPE_EXITS:
            g = _snipe(pts, (r.decided_ts or r.created_ts) - r.created_ts, k, s)
            if g is not None:
                ex = "halten" if k > 1e6 else f"x{k:g}_stop{int(s * 100)}"
                out.append((f"launch_mc{band[0]}-{band[1]}_{ex}", r.mint, int(r.decided_ts or r.created_ts),
                            "pumpfun", liq, "launch", g))
    return pd.DataFrame(out, columns=COLS) if out else _empty()


LABEL_COST = 0.055  # what labels.py takes off (simulator 3 % + pool 2.5 %); added back to get gross


def label_entries(conn, since: int) -> pd.DataFrame:
    """The labelled opportunities of every source under each exit rule the labeller replays (label_rules)."""
    O = pd.read_sql("SELECT source, mint, ts, liq_usd, mcap_usd, features, label_rug, label_rules FROM opportunities "
                    "WHERE label_at IS NOT NULL AND label_rules IS NOT NULL AND ts>=?", conn, params=(since,))
    out = []
    for r in O.itertuples():
        try:
            rules = json.loads(r.label_rules)
            f = json.loads(r.features or "{}")
        except (TypeError, ValueError):
            continue
        dex = str(f.get("dex") or "")
        liq = r.liq_usd or f.get("liq_usd") or 0
        kind = "launch" if r.source == "sniper" else ("copy" if r.source in ("copytrade", "strategy") else "market")
        for name, v in rules.items():
            if v is None:
                continue
            g = -1.0 if (r.label_rug and name == "hold60") else max(-1.0, min(CAP, float(v) + LABEL_COST))
            out.append((f"label_{r.source}_{name}", r.mint, int(r.ts), dex, float(liq), kind, g))
    return pd.DataFrame(out, columns=COLS) if out else _empty()


def all_entries(conn, since: int) -> tuple[pd.DataFrame, dict]:
    prices = load_prices(conn, since)
    P = panel(conn, since, prices)
    parts, info = [], {"prices": len(prices), "coins": int(prices.mint.nunique()) if len(prices) else 0,
                       "panel_points": len(P)}
    for name, fn in (("panel", lambda: panel_entries(P)), ("migration", lambda: migration_entries(conn, since, prices)),
                     ("snipe", lambda: snipe_entries(conn, since)), ("labels", lambda: label_entries(conn, since))):
        try:
            parts.append(fn())
        except Exception as e:  # one broken family must not cost the others
            info[f"error_{name}"] = repr(e)[:200]
    parts = [x for x in parts if len(x)]
    return (pd.concat(parts, ignore_index=True) if parts else _empty()), info
