"""Does a data source help? Every stored coin snapshot gets its real outcome one hour later (from token_prices,
nothing the snapshot could have seen), then each feature is measured against it:

  IC        Spearman rank correlation of the feature with the max return within 1 h
  top/bottom pump rate (max >= +50 % in 1 h, the model's 1 h target) of the top fifth vs the bottom fifth
  z         two-sample z test of those two rates; |z| >= 2 with >= 40 samples per fifth counts as "clear"
  rug       share of samples with <= -50 % at some point in the hour, top fifth vs bottom fifth

One sample per coin and hour (the first snapshot), so a coin watched for a day does not count 96 times."""
import json
import math
import time
from collections import defaultdict

from intel import base, coin, store
from intel.config import settings as default_cfg


def label(conn, now=None, limit=3000):
    """Fill ret_1h / max_ret_1h / min_ret_1h of coin snapshots whose hour is over. Returns rows labelled."""
    now = int(now or time.time())
    rows = conn.execute("SELECT key, ts FROM intel_snapshots WHERE kind='coin' AND labeled_at IS NULL AND ts<=? "
                        "ORDER BY ts LIMIT ?", (now - 3900, limit)).fetchall()
    out = []
    for mint, ts in rows:
        pts = base.series(conn, mint, ts - 1800, ts + 3600)
        p0 = base.at_or_before(pts, ts, 1, max_gap=1800)
        fut = [p[1] for p in pts if p[0] > ts and p[1]]
        if p0 and len(fut) >= 2:
            end = [p for p in pts if ts + 2400 <= p[0] <= ts + 3600 and p[1]]
            out.append((end[-1][1] / p0 - 1 if end else None, max(fut) / p0 - 1, min(fut) / p0 - 1, now, mint, ts))
        elif ts < now - 3 * 3600:
            out.append((None, None, None, now, mint, ts))  # no price data afterwards: never labelled
    if out:
        def write():
            conn.executemany("UPDATE intel_snapshots SET ret_1h=?, max_ret_1h=?, min_ret_1h=?, labeled_at=? "
                             "WHERE kind='coin' AND key=? AND ts=?", out)
            conn.commit()
        from core import db
        db.retry_locked(conn, write)
    return len(out)


def _ranks(xs):
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


def spearman(x, y):
    if len(x) < 3:
        return None
    rx, ry = _ranks(x), _ranks(y)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    vx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    vy = math.sqrt(sum((b - my) ** 2 for b in ry))
    return cov / (vx * vy) if vx and vy else None


def ztest(h1, n1, h2, n2):
    if not n1 or not n2:
        return None
    p = (h1 + h2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    return (h1 / n1 - h2 / n2) / se if se else None


def samples(conn, days=14, now=None):
    now = int(now or time.time())
    seen, out = set(), []
    for key, ts, fj, mx, mn in conn.execute(
            "SELECT key, ts, features_json, max_ret_1h, min_ret_1h FROM intel_snapshots WHERE kind='coin' AND ts>=? "
            "AND max_ret_1h IS NOT NULL ORDER BY ts", (now - days * 86400,)):
        k = (key, ts // 3600)
        if k in seen:
            continue
        seen.add(k)
        out.append((json.loads(fj), mx, mn))
    return out


def evaluate(conn, days=14, now=None, cfg=default_cfg, min_n=100):
    if not store.has(conn, "intel_snapshots"):
        return {"n": 0, "features": [], "note": "Noch keine Zentrale-Daten."}
    data = samples(conn, days, now)
    n = len(data)
    base_rate = (sum(1 for _, mx, _ in data if mx >= cfg.hit_ret) / n) if n else None
    rows = []
    for name, (group, label_de, unit) in coin.FEATURES.items():
        pts = [(f.get(name), mx, mn) for f, mx, mn in data if f.get(name) is not None]
        r = {"feature": name, "group": group, "group_label": coin.GROUPS.get(group, group), "label": label_de, "unit": unit,
             "n": len(pts), "coverage": round(len(pts) / n, 3) if n else None}
        if len(pts) >= min_n:
            pts.sort(key=lambda p: p[0])
            q = max(1, len(pts) // 5)
            lo, hi = pts[:q], pts[-q:]
            hl = sum(1 for p in lo if p[1] >= cfg.hit_ret)
            hh = sum(1 for p in hi if p[1] >= cfg.hit_ret)
            z = ztest(hh, len(hi), hl, len(lo))
            r.update(ic=spearman([p[0] for p in pts], [p[1] for p in pts]), q_n=q, hit_top=hh / len(hi), hit_bottom=hl / len(lo),
                     rug_top=sum(1 for p in hi if p[2] is not None and p[2] <= -0.5) / len(hi),
                     rug_bottom=sum(1 for p in lo if p[2] is not None and p[2] <= -0.5) / len(lo), z=z,
                     clear=bool(z is not None and abs(z) >= 2 and q >= 40))
            r["direction"] = None if z is None else ("höher ist besser" if z > 0 else "niedriger ist besser")
        rows.append(r)
    rows.sort(key=lambda r: (-(r.get("clear") or 0), -abs(r.get("z") or 0)))
    by_group = defaultdict(lambda: {"n_features": 0, "clear": 0, "best_z": None})
    for r in rows:
        g = by_group[r["group"]]
        g["label"] = r["group_label"]
        g["n_features"] += 1
        g["clear"] += bool(r.get("clear"))
        if r.get("z") is not None and (g["best_z"] is None or abs(r["z"]) > abs(g["best_z"])):
            g["best_z"], g["best"] = r["z"], r["label"]
    note = (f"{n} Stichproben (je Coin und Stunde eine) aus {days:g} Tagen, Basis: {100 * base_rate:.1f} % machen +50 % in 1 h."
            if n else "Noch keine abgeschlossene Stunde: die ersten Ergebnisse kommen gut eine Stunde nach dem Start.")
    return {"n": n, "days": days, "base_rate": base_rate, "min_n": min_n, "features": rows, "groups": dict(by_group), "note": note}
