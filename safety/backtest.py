"""Walk-forward backtest of the rug protection on the sniper's recorded launches (read-only, no network).

The sniper stores, for every pump.fun launch it looked at, what it measured at its decision (bundle share, top-10
share, dev buy/sold, holders never seen before, fast buyers, wallets of earlier rugs, ...) and what happened in the
next hour (`rugged`, `max_ret_60m`). That is thousands of labelled launches per day, far more than the coins safety
itself has watched long enough, so it is the test bench for the thresholds.

Walk-forward, so nothing is fitted on the data it is judged on:
  train = older half (by decided_ts), test = newer half.
Three scores are compared on the test half:
  * rules   - safety's own ramps (config.py) applied to the fields the sniper records
  * learned - per-feature rug rates from the train half (fixed bins, shrunk toward the base rate) added up in
              log-odds, then recalibrated (p = sigmoid(logit(base) + a + b * sum), a/b fitted on the last 30 % of
              train) so overlapping features are not counted twice. Its output is a rug probability, not points.
  * sniper  - the sniper's own `risk` column, when present (benchmark only)
Per score: AUC, Brier, calibration table and operating points. A threshold is chosen on train so that at most
5 / 10 / 20 % of the doublers (+100 % within 60 min) get flagged; the test half shows what it really catches.

The learned model of the full history is stored in safety_cache ('launch_model'); `rug_probability(conn, feats)`
scores a new launch with it. The report goes to safety_cache ('launch_backtest') and into the learn report.
"""
import math

from safety import store
from safety.checks import noisy_or, ramp
from safety.config import settings as default_settings

# feature -> bin edges (value >= edge moves to the next bin); fixed, chosen from the field's meaning, not fitted
FEATURES = {
    "bundle_pct": [5, 15, 35, 50],
    "top10_pct": [15, 25, 35, 50, 70],
    "unseen_pct": [10, 30, 50, 80],
    "dev_buy_pct": [5, 10, 20],
    "dev_sold_pct": [0.1, 50, 90, 99.9],
    "fast_buyers": [3, 10, 20],
    "n_buyers": [10, 30, 60, 120],
    "bundle_wallets": [1, 3, 5],
    "dev_launches_24h": [1, 2, 5],
    "known_bad": [1, 2],
}
LABEL_DE = {
    "bundle_pct": "Bundle-Anteil %", "top10_pct": "Top-10-Anteil %", "unseen_pct": "unbekannte Holder %",
    "dev_buy_pct": "Dev-Kauf %", "dev_sold_pct": "Dev verkauft %", "fast_buyers": "Schnellkäufer",
    "n_buyers": "Käufer", "bundle_wallets": "Bundle-Wallets", "dev_launches_24h": "Dev-Launches 24 h",
    "known_bad": "Wallets früherer Rugs",
}
PRIOR_N = 20          # shrinkage of a bin's rug rate toward the base rate (pseudo launches)
FA_TARGETS = [0.05, 0.10, 0.20]
MIN_ROWS = 200
DOUBLER = 1.0         # max_ret_60m >= +100 %


def load(conn):
    """Finished launches with outcome, oldest first, as dicts (None when the sniper table is missing)."""
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(sniper_launches)")}
    except Exception:  # noqa: BLE001
        return None
    if not {"rugged", "decided_ts", "done"} <= cols:
        return None
    want = [c for c in ["mint", "decided_ts", "rugged", "max_ret_60m", "risk", *FEATURES] if c in cols]
    rows = conn.execute(f"SELECT {', '.join(want)} FROM sniper_launches WHERE done=1 AND rugged IS NOT NULL "
                        "AND decided_ts IS NOT NULL ORDER BY decided_ts").fetchall()
    return [dict(zip(want, r)) for r in rows]


def _bin(edges, x):
    if x is None:
        return "na"
    i = 0
    while i < len(edges) and x >= edges[i]:
        i += 1
    return str(i)


def _logit(p):
    p = min(max(p, 1e-4), 1 - 1e-4)
    return math.log(p / (1 - p))


def fit(rows):
    """Per-feature, per-bin rug rate (shrunk) -> log-odds shift against the base rate."""
    n = len(rows)
    base = sum(1 for r in rows if r["rugged"]) / n if n else 0.0
    feats = {}
    for f, edges in FEATURES.items():
        if not any(f in r for r in rows):
            continue
        cnt = {}
        for r in rows:
            b = _bin(edges, r.get(f))
            c = cnt.setdefault(b, [0, 0])
            c[0] += 1
            c[1] += 1 if r["rugged"] else 0
        bins = {}
        for b, (k, rug) in cnt.items():
            p = (rug + PRIOR_N * base) / (k + PRIOR_N)
            bins[b] = {"n": k, "rugs": rug, "rate": round(rug / k, 4), "shift": round(_logit(p) - _logit(base), 4)}
        feats[f] = {"edges": edges, "bins": bins}
    return {"base": base, "n": n, "features": feats}


def _z(model, r):
    """Sum of the per-feature log-odds shifts (0 = base rate)."""
    z = 0.0
    for f, spec in model["features"].items():
        b = spec["bins"].get(_bin(spec["edges"], r.get(f)))
        if b:
            z += b["shift"]
    return z


def predict(model, r):
    """Rug probability: intercept + b * (sum of feature shifts); untrained calibration = base rate, b = 1."""
    a, b = model.get("calib") or (_logit(model["base"]), 1.0)
    x = a + b * _z(model, r)
    return 1 / (1 + math.exp(-max(-30.0, min(30.0, x))))


def _logloss(model, rows):
    s = 0.0
    for r in rows:
        p = min(max(predict(model, r), 1e-4), 1 - 1e-4)
        s -= math.log(p) if r["rugged"] else math.log(1 - p)
    return s / len(rows) if rows else None


def _platt(zs, ys, off, ridge=1.0):
    """Logistic fit y ~ off + a + b*z (Newton, light ridge pulling a->0, b->1)."""
    a, b = 0.0, 1.0
    for _ in range(50):
        haa, hab, hbb = ridge, 0.0, ridge
        ga, gb = ridge * a, ridge * (b - 1)
        for z, y in zip(zs, ys):
            x = max(-30.0, min(30.0, off + a + b * z))
            p = 1 / (1 + math.exp(-x))
            w = p * (1 - p)
            ga += p - y
            gb += (p - y) * z
            haa += w
            hab += w * z
            hbb += w * z * z
        det = haa * hbb - hab * hab
        if det <= 1e-12:
            break
        da = (hbb * ga - hab * gb) / det
        db = (haa * gb - hab * ga) / det
        a, b = a - da, b - db
        if abs(da) + abs(db) < 1e-7:
            break
    return round(a, 4), round(b, 4)


def train(rows):
    """Bins on the first 70 %, recalibration (a, b) on the last 30 % they never saw, then bins refitted on
    everything with that recalibration kept."""
    cut = int(len(rows) * 0.7)
    inner, hold = rows[:cut], rows[cut:]
    full = fit(rows)
    calib = (_logit(full["base"]), 1.0)
    if inner and hold and any(r["rugged"] for r in hold):
        m = fit(inner)
        off = _logit(m["base"])
        a, b = _platt([_z(m, r) for r in hold], [1 if r["rugged"] else 0 for r in hold], off)
        calib = (round(off + a, 4), b)  # absolute intercept: centred on the newest 30 % (rug rates drift)
    full["calib"] = list(calib)
    return full


def rules_score(r, cfg=None):
    """safety's own ramps on the sniper's fields (0-100, like the live score)."""
    c = cfg or default_settings
    risks = [ramp(r.get("top10_pct"), c.top10_warn_pct, c.top10_crit_pct, 20, 70),
             ramp(r.get("unseen_pct"), c.insider_warn_pct, c.insider_crit_pct, 25, 75),
             ramp(r.get("bundle_pct"), c.bundle_warn_pct, c.bundle_crit_pct, 30, 75)]
    if (r.get("dev_sold_pct") or 0) >= c.dev_sold_warn_pct:
        risks.append(ramp(r.get("dev_buy_pct"), 1, 10, 12, 30))
    return noisy_or(risks)


def auc(scores, labels):
    """Probability that a random rug scores above a random non-rug (ties count half)."""
    pairs = sorted(zip(scores, labels))
    pos = sum(labels)
    neg = len(labels) - pos
    if not pos or not neg:
        return None
    rank_sum, i = 0.0, 0
    while i < len(pairs):
        j = i
        while j < len(pairs) and pairs[j][0] == pairs[i][0]:
            j += 1
        avg = (i + j + 1) / 2  # 1-based average rank of the tie group
        rank_sum += avg * sum(1 for k in range(i, j) if pairs[k][1])
        i = j
    return round((rank_sum - pos * (pos + 1) / 2) / (pos * neg), 4)


def _threshold_for(scores, rows, fa):
    """Lowest score threshold that flags at most fa of the doublers."""
    d = sorted((s for s, r in zip(scores, rows) if not r["rugged"] and (r.get("max_ret_60m") or 0) >= DOUBLER),
               reverse=True)
    if not d:
        return None
    k = int(fa * len(d))  # may flag k doublers
    return d[k] + 1e-9 if k < len(d) else min(d)


def _operating(scores, rows, thr):
    flagged = [r for s, r in zip(scores, rows) if s >= thr]
    rugs = sum(1 for r in rows if r["rugged"])
    dbl = sum(1 for r in rows if not r["rugged"] and (r.get("max_ret_60m") or 0) >= DOUBLER)
    hit = sum(1 for r in flagged if r["rugged"])
    fa = sum(1 for r in flagged if not r["rugged"] and (r.get("max_ret_60m") or 0) >= DOUBLER)
    return {"flagged": len(flagged), "flagged_share": round(len(flagged) / len(rows), 3) if rows else None,
            "rugs_caught": hit, "rugs_total": rugs, "recall": round(hit / rugs, 3) if rugs else None,
            "precision": round(hit / len(flagged), 3) if flagged else None,
            "doublers_flagged": fa, "doublers_total": dbl, "false_alarm_rate": round(fa / dbl, 3) if dbl else None}


def _calibration(probs, rows, edges=(0.02, 0.05, 0.1, 0.2, 0.4, 0.7)):
    out = []
    lo = 0.0
    for hi in [*edges, 1.01]:
        sel = [(p, r) for p, r in zip(probs, rows) if lo <= p < hi]
        if sel:
            out.append({"from": lo, "to": min(hi, 1.0), "n": len(sel),
                        "predicted": round(sum(p for p, _ in sel) / len(sel), 4),
                        "actual": round(sum(1 for _, r in sel if r["rugged"]) / len(sel), 4)})
        lo = hi
    return out


def _evaluate(name, train_scores, test_scores, train_rows, test_rows, prob=False):
    labels = [1 if r["rugged"] else 0 for r in test_rows]
    ev = {"auc": auc(test_scores, labels), "operating": []}
    for fa in FA_TARGETS:
        thr = _threshold_for(train_scores, train_rows, fa)
        if thr is None:
            continue
        ev["operating"].append({"fa_target": fa, "threshold": round(thr, 4),
                                **_operating(test_scores, test_rows, thr)})
    if prob:
        ev["brier"] = round(sum((p - y) ** 2 for p, y in zip(test_scores, labels)) / len(labels), 5)
        base = sum(1 for r in train_rows if r["rugged"]) / len(train_rows)
        ev["brier_base"] = round(sum((base - y) ** 2 for y in labels) / len(labels), 5)
        ev["calibration"] = _calibration(test_scores, test_rows)
    return ev


def run(conn, cfg=None, save=True):
    """Walk-forward report (and the full-history model in safety_cache). None when there is too little data."""
    cfg = cfg or default_settings
    store.ensure_schema(conn)
    rows = load(conn)
    if not rows or len(rows) < MIN_ROWS:
        return None
    half = len(rows) // 2
    tr, te = rows[:half], rows[half:]
    model = train(tr)
    report = {
        "n": len(rows), "rugs": sum(1 for r in rows if r["rugged"]),
        "doublers": sum(1 for r in rows if not r["rugged"] and (r.get("max_ret_60m") or 0) >= DOUBLER),
        "train": {"n": len(tr), "from": tr[0]["decided_ts"], "to": tr[-1]["decided_ts"],
                  "rug_rate": round(model["base"], 4)},
        "test": {"n": len(te), "from": te[0]["decided_ts"], "to": te[-1]["decided_ts"],
                 "rug_rate": round(sum(1 for r in te if r["rugged"]) / len(te), 4)},
        "calib": model["calib"], "scores": {},
    }
    report["scores"]["learned"] = _evaluate("learned", [predict(model, r) for r in tr],
                                            [predict(model, r) for r in te], tr, te, prob=True)
    report["scores"]["rules"] = _evaluate("rules", [rules_score(r, cfg) for r in tr],
                                          [rules_score(r, cfg) for r in te], tr, te)
    if any(r.get("risk") is not None for r in rows):
        tr_s = [r.get("risk") or 0 for r in tr]
        te_s = [r.get("risk") or 0 for r in te]
        report["scores"]["sniper"] = _evaluate("sniper", tr_s, te_s, tr, te)
    # strongest single bins of the train half (what the model leans on)
    top = []
    for f, spec in model["features"].items():
        for b, v in spec["bins"].items():
            if b != "na" and v["n"] >= 10:
                top.append({"feature": f, "bin": _bin_label(spec["edges"], b), "n": v["n"], "rug_rate": v["rate"]})
    report["strongest"] = sorted(top, key=lambda x: -x["rug_rate"])[:8]
    if save:
        full = train(rows)
        store.cache_put(conn, "launch_model", full)
        store.cache_put(conn, "launch_backtest", report)
    return report


def _bin_label(edges, b):
    i = int(b)
    lo = edges[i - 1] if i > 0 else None
    hi = edges[i] if i < len(edges) else None
    if lo is None:
        return f"< {hi:g}"
    if hi is None:
        return f"≥ {lo:g}"
    return f"{lo:g}–{hi:g}"


def rug_probability(conn, feats, max_age=86400 * 2):
    """Rug probability within 60 min for a launch described by the sniper's fields (dict), from the stored
    full-history model; None when no model has been trained yet."""
    store.ensure_schema(conn)
    model = store.cache_get(conn, "launch_model", max_age)
    if not model:
        return None
    return round(predict(model, feats), 4)


def report_text(rep):
    """German summary for the CLI."""
    if not rep:
        return "Zu wenige Sniper-Launches mit Ausgang für eine Rückrechnung."
    names = {"learned": "gelernt (Rug-Wahrscheinlichkeit)", "rules": "Regeln von safety", "sniper": "Sniper-Risiko"}
    out = [f"Rückrechnung auf {rep['n']} Sniper-Launches ({rep['rugs']} Rugs, {rep['doublers']} Verdoppler). "
           f"Gelernt an der älteren Hälfte ({rep['train']['n']}, Rug-Rate {100 * rep['train']['rug_rate']:.1f} %), "
           f"geprüft an der neueren ({rep['test']['n']}, Rug-Rate {100 * rep['test']['rug_rate']:.1f} %)."]
    for k, ev in rep["scores"].items():
        a = f"{ev['auc']:.2f}" if ev["auc"] is not None else "–"
        line = f"{names.get(k, k)}: Trennschärfe (AUC) {a}"
        if "brier" in ev:
            line += f", Brier {ev['brier']:.4f} (ohne Modell {ev['brier_base']:.4f})"
        out.append(line)
        for op in ev["operating"]:
            rec = f"{100 * op['recall']:.0f} %" if op["recall"] is not None else "–"
            prec = f"{100 * op['precision']:.0f} %" if op["precision"] is not None else "–"
            fa = f"{100 * op['false_alarm_rate']:.0f} %" if op["false_alarm_rate"] is not None else "–"
            out.append(f"  Ziel ≤{100 * op['fa_target']:.0f} % Fehlalarme: {op['rugs_caught']}/{op['rugs_total']} Rugs "
                       f"({rec}), Treffer unter den Markierten {prec}, Verdoppler markiert {fa}")
    cal = rep["scores"].get("learned", {}).get("calibration")
    if cal:
        out.append("Kalibrierung (vorhergesagt → tatsächlich):")
        for c in cal:
            out.append(f"  {100 * c['from']:>3.0f}–{100 * c['to']:.0f} %: {c['n']:>5} Launches, "
                       f"{100 * c['predicted']:.1f} % → {100 * c['actual']:.1f} %")
    if rep.get("strongest"):
        out.append("Stärkste Warnzeichen (ältere Hälfte):")
        for s in rep["strongest"]:
            out.append(f"  {LABEL_DE.get(s['feature'], s['feature'])} {s['bin']}: {100 * s['rug_rate']:.0f} % Rugs (n={s['n']})")
    return "\n".join(out)


def summary(rep):
    """Compact form for the learn report / dashboard: AUC per score and the ≤10 % false-alarm operating point."""
    out = {"n": rep["n"], "rugs": rep["rugs"], "doublers": rep["doublers"], "test_n": rep["test"]["n"], "scores": {}}
    lines = [f"Rückrechnung Sniper-Launches (an älterer Hälfte gelernt, an {rep['test']['n']} neueren geprüft):"]
    names = {"learned": "gelernt", "rules": "Regeln", "sniper": "Sniper"}
    for k, ev in rep["scores"].items():
        op = next((o for o in ev["operating"] if o["fa_target"] == 0.10), None)
        out["scores"][k] = {"auc": ev["auc"], "at_10pct_fa": op}
        if op and op["recall"] is not None:
            lines.append(f"  {names.get(k, k):<8} AUC {ev['auc']:.2f}; bei ≤10 % Fehlalarmen {op['rugs_caught']}/"
                         f"{op['rugs_total']} Rugs erkannt, {100 * (op['precision'] or 0):.0f} % der Markierten "
                         f"waren Rugs, {op['doublers_flagged']}/{op['doublers_total']} Verdoppler markiert")
    out["text"] = "\n".join(lines)
    return out
