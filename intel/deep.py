"""Deeper analysis on top of the stored snapshots and ticker events (no network, every 30 min, intel_state 'deep').

  events      event study: what the price did after each kind of ticker event (5 / 15 / 60 min, peak, crash)
  robustness  per feature: is the effect stable over time (older vs newer half), in every market phase, and does it
              add something beyond size, age and liquidity (partial rank correlation)? p-values corrected for
              testing many features at once (Benjamini-Hochberg).
  redundancy  features that measure the same thing (|Spearman| >= 0.8), so the brain does not count one fact twice
  playbook    setups like a trader's: single conditions and pairs of them. Found on the older two thirds of the
              samples, kept only when the newest third confirms them (walk-forward), BH-corrected.
"""
import math
import statistics
import time
from collections import defaultdict
from itertools import combinations

from intel import base, coin, evaluate, events, store
from intel.config import settings as default_cfg

CONTROLS = ("px_mcap", "px_age_h", "liq_usd")

# trader conditions: key -> (German text, test on a features dict); None-safe
def _g(f, k):
    return f.get(k)


CONDITIONS = {
    "smart2": ("≥ 2 Smart-Personen kaufen", lambda f: (_g(f, "flow_smart_entities_60m") or 0) >= 2),
    "smart_exit": ("Smart Money steigt aus", lambda f: (_g(f, "flow_smart_exit_share") or 0) >= 0.5),
    "accel": ("Käufer beschleunigen", lambda f: (_g(f, "flow_buyers_accel") or 0) >= 2 and (_g(f, "flow_buyers_15m") or 0) >= 5),
    "buy_press": ("Kaufdruck ≥ 65 %", lambda f: (_g(f, "flow_buy_ratio_15m") or 0) >= 0.65),
    "sell_press": ("Kaufdruck ≤ 35 %", lambda f: _g(f, "flow_buy_ratio_15m") is not None and f["flow_buy_ratio_15m"] <= 0.35),
    "wash": ("Wash ≥ 40 %", lambda f: (_g(f, "vol_roundtrip_60m") or 0) >= 0.4),
    "clean_vol": ("Volumen echt (Wash < 10 %)", lambda f: _g(f, "vol_roundtrip_60m") is not None and f["vol_roundtrip_60m"] < 0.1),
    "whales": ("Wale > 50 % der Käufe", lambda f: (_g(f, "flow_whale_share_60m") or 0) > 0.5),
    "retail": ("≥ 20 Kleinanleger kaufen", lambda f: (_g(f, "flow_retail_buyers_60m") or 0) >= 20),
    "vpin": ("Einseitiger Fluss (VPIN ≥ 0,7)", lambda f: (_g(f, "micro_vpin") or 0) >= 0.7),
    "absorb": ("Käufe werden absorbiert", lambda f: (_g(f, "micro_absorption") or 0) >= 0.5),
    "fading": ("Kauftempo lässt nach", lambda f: _g(f, "micro_buy_momentum") is not None and f["micro_buy_momentum"] < 0.5),
    "cheap": ("Billig handelbar (< 3 % hin und zurück)", lambda f: _cost(f) is not None and _cost(f) < 0.03),
    "expensive": ("Teuer (≥ 8 % hin und zurück)", lambda f: _cost(f) is not None and _cost(f) >= 0.08),
    "liq_drain": ("Liquidität fließt ab", lambda f: _g(f, "liq_chg_15m") is not None and f["liq_chg_15m"] <= -0.15),
    "off_peak": ("Liquidität > 50 % unter Hoch", lambda f: (_g(f, "liq_off_peak") or 0) > 0.5),
    "insiders_in": ("Insider halten noch > 50 %", lambda f: (_g(f, "tape_insider_held") or 0) > 0.5),
    "insiders_out": ("Insider bauen ab", lambda f: (_g(f, "tape_insider_sold_1h") or 0) >= 0.25),
    "distrib": ("Große Halter verteilen", lambda f: (_g(f, "tape_distribution") or 0) >= 0.15),
    "conc": ("Top-10 halten ≥ 50 %", lambda f: (_g(f, "hold_top10") or _g(f, "tape_top10") or 0) >= 0.5),
    "rugger": ("Dev mit Rug-Historie", lambda f: (_g(f, "net_dev_launches") or 0) >= 2 and (_g(f, "net_dev_rug_rate") or 0) >= 0.5),
    "bad_wallets": ("Bekannte Rug-Wallets dabei", lambda f: (_g(f, "net_early_bad") or 0) >= 2),
    "good_wallets": ("Wallets aus 2x-Launches dabei", lambda f: (_g(f, "net_early_good") or 0) >= 2),
    "bots": ("Bots ≥ 40 % des Volumens", lambda f: (_g(f, "net_bot_share_60m") or 0) >= 0.4),
    "young": ("Jünger als 1 h", lambda f: _g(f, "px_age_h") is not None and f["px_age_h"] < 1),
    "small": ("Market Cap < 100k", lambda f: _g(f, "px_mcap") is not None and f["px_mcap"] < 100_000),
    "big": ("Market Cap > 1 Mio.", lambda f: (_g(f, "px_mcap") or 0) > 1_000_000),
    "dip": ("> 30 % unter 24-h-Hoch", lambda f: (_g(f, "px_off_high_24h") or 0) > 0.3),
    "running": ("+20 % in 15 min", lambda f: (_g(f, "px_ret_15m") or 0) >= 0.2),
    "hot_mkt": ("Markt Risk-on oder heiß", lambda f: (_g(f, "mkt_phase") or 0) >= 55),
    "cold_mkt": ("Markt Risk-off", lambda f: _g(f, "mkt_phase") is not None and f["mkt_phase"] < 35),
    "hot_story": ("Heißes Narrativ", lambda f: (_g(f, "mkt_term_heat") or 0) >= 60),
    "boosted": ("Neu beworben (Boost)", lambda f: (_g(f, "att_boost_chg_1h") or 0) > 0),
}


def _cost(f):
    return f.get("exec_q_0_25") if f.get("exec_q_0_25") is not None else f.get("exec_rt_0_25")


def _p_two_sided(z):
    return math.erfc(abs(z) / math.sqrt(2)) if z is not None else 1.0


def bh(ps):
    """Benjamini-Hochberg q-values in the input order."""
    n = len(ps)
    order = sorted(range(n), key=lambda i: ps[i])
    q = [1.0] * n
    best = 1.0
    for rank in range(n, 0, -1):
        i = order[rank - 1]
        best = min(best, ps[i] * n / rank)
        q[i] = best
    return q


# ------------------------------------------------------------------ event study
def _path(conn, mint, ts):
    pts = base.series(conn, mint, ts - 600, ts + 3600)
    p0 = base.at_or_before(pts, ts, 1, max_gap=600)
    fut = [(t, p) for t, p, _, _ in pts if t > ts and p]
    if not p0 or len(fut) < 2:
        return None
    def at(dt):
        c = [(t, p) for t, p in fut if t <= ts + dt]
        return c[-1][1] / p0 - 1 if c and c[-1][0] >= ts + dt * 0.6 else None
    return {"r5": at(300), "r15": at(900), "r60": at(3600), "max": max(p for _, p in fut) / p0 - 1,
            "min": min(p for _, p in fut) / p0 - 1}


def event_study(conn, now, days=7, cfg=default_cfg, base_rate=None, limit=6000):
    rows = conn.execute("SELECT kind, mint, ts FROM intel_events WHERE mint IS NOT NULL AND ts>=? AND ts<=? ORDER BY ts DESC LIMIT ?",
                        (now - days * 86400, now - 3600, limit)).fetchall()
    by = defaultdict(list)
    seen = set()
    for kind, mint, ts in rows:
        k = (kind, mint, ts // 1800)
        if k in seen:  # the same kind on the same coin within 30 min is one event
            continue
        seen.add(k)
        p = _path(conn, mint, ts)
        if p:
            by[kind].append(p)
    out = []
    for kind, ps in by.items():
        n = len(ps)
        hits = sum(1 for p in ps if p["max"] >= cfg.hit_ret)
        crash = sum(1 for p in ps if p["min"] <= -0.5)
        med = lambda k: statistics.median([p[k] for p in ps if p[k] is not None]) if any(p[k] is not None for p in ps) else None  # noqa: E731
        z = None
        if base_rate and n:
            se = math.sqrt(base_rate * (1 - base_rate) / n)
            z = (hits / n - base_rate) / se if se else None
        out.append({"kind": kind, "label": events.KINDS.get(kind, (kind,))[0], "tone": events.KINDS.get(kind, ("", ""))[1], "n": n,
                    "med_r5": med("r5"), "med_r15": med("r15"), "med_r60": med("r60"), "med_max": med("max"),
                    "hit_rate": hits / n, "crash_rate": crash / n, "z": z, "clear": bool(z is not None and abs(z) >= 2 and n >= 30)})
    out.sort(key=lambda r: -r["n"])
    return out


# ------------------------------------------------------------------ robustness, redundancy
def _partial_ic(x, y, controls):
    """Spearman of x and y after removing what the controls explain from both (ranks, least squares)."""
    rx, ry = evaluate._ranks(x), evaluate._ranks(y)
    rc = [evaluate._ranks(c) for c in controls]
    def resid(v):
        # Gram-Schmidt on the control ranks, then subtract the projection
        n = len(v)
        basis = []
        for c in rc:
            m = sum(c) / n
            u = [a - m for a in c]
            for b in basis:
                d = sum(a * bb for a, bb in zip(u, b))
                u = [a - d * bb for a, bb in zip(u, b)]
            norm = math.sqrt(sum(a * a for a in u))
            if norm > 1e-9:
                basis.append([a / norm for a in u])
        m = sum(v) / n
        r = [a - m for a in v]
        for b in basis:
            d = sum(a * bb for a, bb in zip(r, b))
            r = [a - d * bb for a, bb in zip(r, b)]
        return r
    a, b = resid(rx), resid(ry)
    va, vb = math.sqrt(sum(t * t for t in a)), math.sqrt(sum(t * t for t in b))
    return sum(s * t for s, t in zip(a, b)) / (va * vb) if va and vb else None


PHASES = (("cold", "Risk-off", 0, 35), ("neutral", "Neutral", 35, 55), ("warm", "Risk-on/heiß", 55, 101))


def robustness(data, cfg=default_cfg, min_n=100):
    """data: [(features, max_ret, min_ret)] oldest first."""
    rows = []
    for name, (group, label, unit) in coin.FEATURES.items():
        pts = [(f.get(name), mx, f) for f, mx, _ in data if f.get(name) is not None]
        if len(pts) < min_n:
            continue
        x, y = [p[0] for p in pts], [p[1] for p in pts]
        if len(set(x)) < 2:
            continue
        ic = evaluate.spearman(x, y)
        h = len(pts) // 2
        ic_old, ic_new = evaluate.spearman(x[:h], y[:h]), evaluate.spearman(x[h:], y[h:])
        stable = bool(ic_old is not None and ic_new is not None and ic_old * ic_new > 0 and min(abs(ic_old), abs(ic_new)) >= 0.03)
        by_phase = {}
        for key, lab, lo, hi in PHASES:
            sub = [(p[0], p[1]) for p in pts if p[2].get("mkt_phase") is not None and lo <= p[2]["mkt_phase"] < hi]
            if len(sub) >= 40:
                by_phase[key] = {"label": lab, "n": len(sub), "ic": evaluate.spearman([s[0] for s in sub], [s[1] for s in sub])}
        ctrl_pts = [p for p in pts if all(p[2].get(c) is not None for c in CONTROLS)]
        partial = None
        if len(ctrl_pts) >= min_n and name not in CONTROLS:
            partial = _partial_ic([p[0] for p in ctrl_pts], [p[1] for p in ctrl_pts], [[p[2][c] for p in ctrl_pts] for c in CONTROLS])
        z = ic * math.sqrt(len(pts) - 3) if ic is not None and len(pts) > 3 else None  # Fisher-style approx for rank IC
        rows.append({"feature": name, "label": label, "group": group, "group_label": coin.GROUPS.get(group, group), "n": len(pts),
                     "ic": ic, "ic_old": ic_old, "ic_new": ic_new, "stable": stable, "by_phase": by_phase,
                     "partial_ic": partial, "z": z, "p": _p_two_sided(z)})
    qs = bh([r["p"] for r in rows])
    for r, q in zip(rows, qs):
        r["q"] = q
        adds = r["partial_ic"] is None or abs(r["partial_ic"]) >= 0.02
        r["verdict"] = ("robust" if q <= 0.05 and r["stable"] and adds else
                        "nur Größe/Alter" if q <= 0.05 and r["stable"] and not adds else
                        "instabil" if q <= 0.05 else "kein Effekt")
    rows.sort(key=lambda r: (r["verdict"] != "robust", r["q"]))
    return rows


def redundancy(data, names, min_n=150, thr=0.8, max_pairs=40, max_samples=2000):
    """Feature pairs that say the same (|rank corr| >= thr). A strided subsample keeps it cheap (rho is stable)."""
    if len(data) > max_samples:
        data = data[::-(-len(data) // max_samples)]
    pairs = []
    for a, b in combinations(names, 2):
        pts = [(f[a], f[b]) for f, _, _ in data if f.get(a) is not None and f.get(b) is not None]
        if len(pts) < min_n:
            continue
        rho = evaluate.spearman([p[0] for p in pts], [p[1] for p in pts])
        if rho is not None and abs(rho) >= thr:
            pairs.append({"a": a, "b": b, "a_label": coin.FEATURES[a][1], "b_label": coin.FEATURES[b][1], "rho": rho, "n": len(pts)})
    pairs.sort(key=lambda r: -abs(r["rho"]))
    return pairs[:max_pairs]


# ------------------------------------------------------------------ playbook
def _stats(rows, hit_ret):
    n = len(rows)
    if not n:
        return None
    return {"n": n, "hit": sum(1 for _, mx, _ in rows if mx >= hit_ret) / n,
            "crash": sum(1 for _, _, mn in rows if mn is not None and mn <= -0.5) / n,
            "med_max": statistics.median(mx for _, mx, _ in rows)}


def playbook(data, cfg=default_cfg, min_n=30, min_confirm=15):
    """Setups from the older 2/3 of the samples, confirmed on the newest 1/3. Returns {'good': [...], 'bad': [...], ...}."""
    n = len(data)
    if n < 3 * min_n:
        return {"n": n, "good": [], "bad": [], "tested": 0, "note": "Zu wenig Stichproben für Setups."}
    cut = int(n * 2 / 3)
    old, new = data[:cut], data[cut:]
    base_old = sum(1 for _, mx, _ in old if mx >= cfg.hit_ret) / len(old)
    base_new = sum(1 for _, mx, _ in new if mx >= cfg.hit_ret) / len(new)
    crash_old = sum(1 for _, _, mn in old if mn is not None and mn <= -0.5) / len(old)
    match_old = {k: [fn(f) for f, _, _ in old] for k, (_, fn) in CONDITIONS.items()}
    match_new = {k: [fn(f) for f, _, _ in new] for k, (_, fn) in CONDITIONS.items()}
    singles = [(k,) for k in CONDITIONS if sum(match_old[k]) >= min_n]
    pairs = [(a, b) for (a,), (b,) in combinations(singles, 2)]
    cands = []
    for s in singles + pairs:
        mo = [r for i, r in enumerate(old) if all(match_old[k][i] for k in s)]
        if len(mo) < min_n:
            continue
        st = _stats(mo, cfg.hit_ret)
        se = math.sqrt(max(base_old * (1 - base_old), 1e-6) / st["n"])
        z = (st["hit"] - base_old) / se
        sec = math.sqrt(max(crash_old * (1 - crash_old), 1e-6) / st["n"])
        zc = (st["crash"] - crash_old) / sec
        cands.append({"setup": s, "old": st, "z": z, "zc": zc, "p": min(_p_two_sided(z), _p_two_sided(zc))})
    qs = bh([c["p"] for c in cands])
    good, bad = [], []
    for c, q in zip(cands, qs):
        if q > 0.1:
            continue
        mn = [r for i, r in enumerate(new) if all(match_new[k][i] for k in c["setup"])]
        sn = _stats(mn, cfg.hit_ret)
        kind = "good" if c["z"] > 0 and c["zc"] <= 0 else "bad" if c["z"] < 0 or c["zc"] > 0 else None
        if not kind:
            continue
        confirmed = bool(sn and sn["n"] >= min_confirm and (
            (kind == "good" and sn["hit"] > base_new) or (kind == "bad" and (sn["hit"] < base_new or sn["crash"] > crash_old))))
        row = {"setup": list(c["setup"]), "text": " + ".join(CONDITIONS[k][0] for k in c["setup"]), "q": q,
               "found": c["old"], "lift": c["old"]["hit"] / base_old if base_old else None, "check": sn, "confirmed": confirmed}
        (good if kind == "good" else bad).append(row)
    good.sort(key=lambda r: (not r["confirmed"], -(r["lift"] or 0)))
    bad.sort(key=lambda r: (not r["confirmed"], r["lift"] or 0))
    return {"n": n, "n_found": cut, "n_check": n - cut, "base_found": base_old, "base_check": base_new, "crash_base": crash_old,
            "tested": len(cands), "good": good[:15], "bad": bad[:15]}


def matching(f, book):
    """Confirmed setups (good and bad) that a coin's current features match: [(kind, text, lift)]."""
    out = []
    for kind in ("good", "bad"):
        for s in (book or {}).get(kind, []):
            if s.get("confirmed") and all(CONDITIONS[k][1](f) for k in s["setup"] if k in CONDITIONS):
                out.append((kind, s["text"], s.get("lift")))
    return out


def run(conn, now=None, days=14, cfg=default_cfg):
    now = int(now or time.time())
    data = evaluate.samples(conn, days, now)
    base_rate = (sum(1 for _, mx, _ in data if mx >= cfg.hit_ret) / len(data)) if data else None
    rob = robustness(data, cfg) if data else []
    names = [r["feature"] for r in sorted(rob, key=lambda r: r["q"]) if r["n"] >= 150][:24]
    return {"ts": now, "n": len(data), "base_rate": base_rate,
            "events": event_study(conn, now, cfg=cfg, base_rate=base_rate) if store.has(conn, "intel_events") else [],
            "robustness": rob, "redundancy": redundancy(data, names) if data else [],
            "playbook": playbook(data, cfg), "conditions": {k: v[0] for k, v in CONDITIONS.items()}}
