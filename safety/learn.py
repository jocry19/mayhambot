"""The safety layer learns from what really happened to the coins it checked (no network).

1. Outcome: for every mint, its first assessment that stored per-check risks is compared with the price and
   liquidity snapshots core already records (`token_prices`) over the next SAFETY_OUTCOME_HORIZON_H hours.
   Rugged = price or liquidity fell by SAFETY_RUG_DROP_PCT (90 %) or more below the value at the check.
2. Per check: rug rate of coins the check flagged (risk >= SAFETY_LEARN_FLAG_RISK) versus all coins (lift).
   A check whose warnings come true more often than average gets more weight, one that cries wolf less:
       raw = 0.5 + 0.5 * lift            (lift 1 -> 1.0, lift 2 -> 1.5, lift 0 -> 0.5)
       weight = 1 + (raw - 1) * n / (n + SAFETY_LEARN_SHRINK_N), clamped to [WEIGHT_MIN, WEIGHT_MAX]
   only once a check flagged at least SAFETY_LEARN_MIN_FLAGGED labelled coins. Hard flags keep blocking.
3. Calibration per level (niedrig/erhöht/hoch/kritisch): how many coins of each level rugged. Stored as a
   report in safety_cache ('learn_report') for the dashboard and `python -m safety learn`.
"""
import json
import logging
import time

from core import db

from safety import store
from safety.config import settings as default_settings

log = logging.getLogger("mayhambot.safety")
LEVELS = ["low", "caution", "high", "critical"]


def _snapshots(conn, mint, t0, t1):
    return conn.execute("SELECT ts, price_usd, liquidity_usd FROM token_prices WHERE mint=? AND ts>=? AND ts<=? "
                        "ORDER BY ts", (mint, t0, t1)).fetchall()


def label_outcomes(conn, cfg=None, now=None):
    """Label every mint whose first stored assessment is older than the horizon. Returns new labels."""
    cfg = cfg or default_settings
    store.ensure_schema(conn)
    now = now or int(time.time())
    horizon = int(cfg.outcome_horizon_h * 3600)
    rows = conn.execute(
        """SELECT h.mint, h.ts, h.score, h.level, k.risks_json FROM safety_history h
           JOIN (SELECT mint, MIN(ts) AS ts FROM safety_risks GROUP BY mint) f ON f.mint = h.mint AND f.ts = h.ts
           JOIN safety_risks k ON k.mint = h.mint AND k.ts = h.ts
           WHERE h.ts <= ? AND h.mint NOT IN (SELECT mint FROM safety_outcomes)""", (now - horizon,)).fetchall()
    new = []
    for mint, ts, score, level, risks in rows:
        snaps = _snapshots(conn, mint, ts - 3600, ts + horizon)
        before = [s for s in snaps if s["ts"] <= ts + 600 and s["price_usd"]]
        after = [s for s in snaps if s["ts"] > ts and s["price_usd"]]
        if not before or len(after) < 3:
            continue  # no price at the check or too few points afterwards: unknown, not "safe"
        p0 = before[-1]["price_usd"]
        l0 = before[-1]["liquidity_usd"] or 0.0
        min_ret = min(s["price_usd"] for s in after) / p0 - 1
        ret_end = after[-1]["price_usd"] / p0 - 1
        liqs = [s["liquidity_usd"] for s in after if s["liquidity_usd"] is not None]
        liq_drop = (1 - min(liqs) / l0) if l0 and liqs else None
        cut = cfg.rug_drop_pct / 100
        rugged = int(min_ret <= -cut or (liq_drop is not None and liq_drop >= cut))
        hit = next((x["ts"] for x in after if x["price_usd"] <= p0 * (1 - cut)
                    or (l0 and x["liquidity_usd"] is not None and x["liquidity_usd"] <= l0 * (1 - cut))), None)
        max_ret = max(x["price_usd"] for x in after) / p0 - 1
        new.append((mint, ts, score, level, risks, p0, l0, round(min_ret, 4), round(ret_end, 4),
                    None if liq_drop is None else round(liq_drop, 4), len(after), rugged, now,
                    None if hit is None else round((hit - ts) / 60, 1), round(max_ret, 4)))

    def write():
        conn.executemany("INSERT OR REPLACE INTO safety_outcomes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", new)
        conn.commit()
    if new:
        db.retry_locked(conn, write)
    return len(new)


def fit_weights(conn, cfg=None):
    """Per-check weight from labelled outcomes. Returns the report dict (also stored)."""
    cfg = cfg or default_settings
    rows = conn.execute("SELECT level, risks_json, rugged FROM safety_outcomes").fetchall()
    n = len(rows)
    base = sum(r["rugged"] for r in rows) / n if n else 0.0
    per_check = {}
    for r in rows:
        for name, risk in json.loads(r["risks_json"] or "{}").items():
            d = per_check.setdefault(name, {"flagged": 0, "flagged_rugs": 0, "clean": 0, "clean_rugs": 0})
            key = "flagged" if (risk or 0) >= cfg.learn_flag_risk else "clean"
            d[key] += 1
            d[key + "_rugs"] += r["rugged"]
    weights, now = {}, int(time.time())
    for name, d in per_check.items():
        rate_f = d["flagged_rugs"] / d["flagged"] if d["flagged"] else None
        rate_c = d["clean_rugs"] / d["clean"] if d["clean"] else None
        lift = rate_f / base if rate_f is not None and base > 0 else None
        w = 1.0
        if lift is not None and d["flagged"] >= cfg.learn_min_flagged:
            raw = 0.5 + 0.5 * lift
            w = 1 + (raw - 1) * d["flagged"] / (d["flagged"] + cfg.learn_shrink_n)
            w = max(cfg.learn_weight_min, min(cfg.learn_weight_max, w))
        weights[name] = dict(weight=round(w, 3), n_flagged=d["flagged"], rug_rate_flagged=rate_f,
                             rug_rate_clean=rate_c, base_rate=base, lift=lift)
    levels = {lv: {"n": 0, "rugged": 0} for lv in LEVELS}
    for r in rows:
        if r["level"] in levels:
            levels[r["level"]]["n"] += 1
            levels[r["level"]]["rugged"] += r["rugged"]
    for v in levels.values():
        v["rug_rate"] = round(v["rugged"] / v["n"], 3) if v["n"] else None
    report = {"ts": now, "n_labelled": n, "base_rug_rate": round(base, 3), "levels": levels,
              "thresholds": _sweep([(r["score"], r["rugged"], r["max_ret"]) for r in
                                    conn.execute("SELECT score, rugged, max_ret FROM safety_outcomes")], cfg),
              "rug_after_min": _quantiles([r[0] for r in conn.execute(
                  "SELECT rug_after_min FROM safety_outcomes WHERE rugged=1 AND rug_after_min IS NOT NULL")]),
              "launches": evaluate_launches(conn, cfg),
              "checks": weights, "horizon_h": cfg.outcome_horizon_h, "rug_drop_pct": cfg.rug_drop_pct}

    def write():
        conn.executemany("INSERT OR REPLACE INTO safety_weights VALUES (?,?,?,?,?,?,?,?)",
                         [(k, v["weight"], v["n_flagged"], v["rug_rate_flagged"], v["rug_rate_clean"],
                           v["base_rate"], v["lift"], now) for k, v in weights.items()])
        conn.commit()
    if weights:
        db.retry_locked(conn, write)
    store.cache_put(conn, "learn_report", report)
    return report


def _quantiles(xs):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    q = lambda p: round(xs[min(len(xs) - 1, int(p * len(xs)))], 1)  # noqa: E731
    return {"n": len(xs), "p25": q(0.25), "median": q(0.5), "p75": q(0.75)}


def _sweep(rows, cfg):
    """Hit rate and false alarms per score threshold. rows: (score, rugged, max_ret); a 'doubler' went +100 %."""
    rugs = sum(1 for _, r, _ in rows if r)
    doublers = sum(1 for _, r, m in rows if not r and (m or 0) >= 1.0)
    out = {}
    for name, thr in (("erhöht", cfg.caution_score), ("hoch", cfg.high_score), ("gesperrt", cfg.block_score)):
        flagged = [(r, m) for s, r, m in rows if (s or 0) >= thr]
        hit = sum(1 for r, _ in flagged if r)
        fa = sum(1 for r, m in flagged if not r and (m or 0) >= 1.0)
        out[name] = {"threshold": thr, "flagged": len(flagged),
                     "rugs_caught": hit, "rugs_total": rugs, "recall": round(hit / rugs, 3) if rugs else None,
                     "precision": round(hit / len(flagged), 3) if flagged else None,
                     "doublers_flagged": fa, "doublers_total": doublers,
                     "false_alarm_rate": round(fa / doublers, 3) if doublers else None}
    return out


def evaluate_launches(conn, cfg=None):
    """The sniper records the safety risk at its decision and the outcome of every launch (read-only here).
    Same sweep as for tracked coins: how many rugs the protection caught and how many doublers it would
    have blocked."""
    cfg = cfg or default_settings
    try:
        rows = conn.execute("SELECT safety_risk, rugged, max_ret_60m FROM sniper_launches "
                            "WHERE done=1 AND safety_risk IS NOT NULL AND rugged IS NOT NULL").fetchall()
    except Exception:  # noqa: BLE001 - sniper not installed
        return None
    if not rows:
        return None
    return {"n": len(rows), "rugs": sum(1 for r in rows if r[1]), "thresholds": _sweep(
        [(r[0], r[1], r[2]) for r in rows], cfg)}


def learn(conn, cfg=None):
    cfg = cfg or default_settings
    store.ensure_schema(conn)
    added = label_outcomes(conn, cfg)
    report = fit_weights(conn, cfg)
    report["new_labels"] = added
    try:
        from safety import backtest
        bt = backtest.run(conn, cfg)
    except Exception:  # noqa: BLE001 - the backtest must never stop learning
        log.exception("safety launch backtest failed")
        bt = None
    if bt:
        report["launch_backtest"] = backtest.summary(bt)
    try:
        from safety import audit
        report["buys"] = audit.audit(conn, since=int(time.time()) - 7 * 86400, cfg=cfg)
    except Exception:  # noqa: BLE001
        log.exception("safety buy audit failed")
    store.cache_put(conn, "learn_report", report)
    log.info("safety learn: %d new outcomes, %d labelled, base rug rate %.0f %%", added, report["n_labelled"],
             100 * report["base_rug_rate"])
    return report


def maybe_learn(conn, cfg=None):
    cfg = cfg or default_settings
    store.ensure_schema(conn)
    if store.cache_get(conn, "learn_report", cfg.learn_every_sec) is not None:
        return None
    return learn(conn, cfg)


def report_text(report):
    """German summary for the CLI."""
    de = {"low": "niedrig", "caution": "erhöht", "high": "hoch", "critical": "kritisch"}
    out = [f"{report['n_labelled']} Coins mit bekanntem Ausgang ({report['horizon_h']:g} h nach der ersten Prüfung), "
           f"davon {100 * report['base_rug_rate']:.0f} % gerugged (Kurs oder Liquidität −{report['rug_drop_pct']:g} %)."]
    for lv in LEVELS:
        v = report["levels"][lv]
        if v["n"]:
            out.append(f"  Stufe {de[lv]:<9} {v['n']:>4} Coins, {100 * v['rug_rate']:.0f} % gerugged")
    def sweep(title, sw):
        out.append(title)
        for name, v in sw.items():
            if v["recall"] is None and v["false_alarm_rate"] is None:
                continue
            rec = f"{100 * v['recall']:.0f} %" if v["recall"] is not None else "–"
            fa = f"{100 * v['false_alarm_rate']:.0f} %" if v["false_alarm_rate"] is not None else "–"
            out.append(f"  ab {name:<9} (Risiko ≥ {v['threshold']:g}): {v['rugs_caught']}/{v['rugs_total']} Rugs erkannt "
                       f"({rec}), {v['doublers_flagged']}/{v['doublers_total']} Verdoppler fälschlich markiert ({fa})")
    if report.get("thresholds"):
        sweep("Trefferquote und Fehlalarme (beobachtete Coins):", report["thresholds"])
    if report.get("launches"):
        sweep(f"Sniper-Launches ({report['launches']['n']}, davon {report['launches']['rugs']} Rugs):",
              report["launches"]["thresholds"])
    if report.get("launch_backtest"):
        out.append(report["launch_backtest"]["text"])
    if report.get("buys"):
        from safety import audit
        out.append("Käufe der letzten 7 Tage nach Rug-Stufe beim Kauf:")
        out.append(audit.report_text(report["buys"]))
    if report.get("rug_after_min"):
        q = report["rug_after_min"]
        out.append(f"Zeit von der ersten Prüfung bis zum Rug: Median {q['median']:g} min (25–75 %: {q['p25']:g}–{q['p75']:g} min, n={q['n']})")
    out.append("Gewichte je Check:")
    for name, v in sorted(report["checks"].items(), key=lambda kv: -(kv[1]["lift"] or 0)):
        lift = f"{v['lift']:.1f}x" if v["lift"] is not None else "–"
        rate = f"{100 * v['rug_rate_flagged']:.0f} %" if v["rug_rate_flagged"] is not None else "–"
        out.append(f"  {name:<12} warnte {v['n_flagged']:>4}x, davon gerugged {rate:>5}, Trefferfaktor {lift:>5}, "
                   f"Gewicht {v['weight']:.2f}")
    return "\n".join(out)
