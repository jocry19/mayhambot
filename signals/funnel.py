"""Why are there (no) signals? Per-condition funnel over the stored signal_metrics rows.

    .venv/bin/python -m signals.funnel --hours 10

Every cycle stores one signal_metrics row per watched coin AFTER model + safety + market, so this shows
exactly where coins drop out: smart buyers, distinct entities, net inflow, score threshold, cooldown.
"""
import argparse
import json
import os
import time

from core import db
from core.config import settings


def _is_model(contrib_json):
    try:
        c = json.loads(contrib_json or "{}")
    except ValueError:
        return False
    return bool(c) and "inflow" not in c


def _pct(vals, q):
    if not vals:
        return None
    vals = sorted(vals)
    return vals[min(len(vals) - 1, int(q * len(vals)))]


def funnel(conn, hours=10, cfg=settings, now=None):
    now = now or int(time.time())
    since = now - int(hours * 3600)
    rows = conn.execute("SELECT ts, mint, net_usd, smart_buyers, score, n_ultra, n_smart, n_good, contributions_json, "
                        "model_exp_net FROM signal_metrics WHERE ts>=?", (since,)).fetchall()
    out = dict(hours=hours, rows=len(rows), cycles=len({r["ts"] for r in rows}), coins=len({r["mint"] for r in rows}),
               model_rows=0, smart_ge1=0, smart_ge_min=0, net_ge_min=0, score_ge_min=0,
               smart_and_net=0, all_pass=0, max_score=0.0, max_score_model=0.0, max_score_heuristic=0.0,
               any_good=0, min_wallets=cfg.signal_min_wallets, min_net=cfg.signal_min_net_usd,
               min_score=cfg.signal_min_score, min_score_model=cfg.model_signal_min_score)
    from signals import engine
    thr_model = engine.cfg_min_model(conn, cfg)
    net_min = engine.min_net_usd(conn, cfg, now)
    out.update(min_net_calibrated=net_min, min_score_model_calibrated=thr_model, model_base_rate_1h=engine._model_thr.get("base"),
               all_pass_calibrated=0, blocked_loss_veto=0)
    model_scores, coins_pass = [], set()
    for r in rows:
        is_model = _is_model(r["contributions_json"])
        thr = cfg.model_signal_min_score if is_model else cfg.signal_min_score
        s, sb, net = r["score"] or 0.0, r["smart_buyers"] or 0, r["net_usd"] or 0.0
        out["model_rows"] += is_model
        out["any_good"] += (r["n_good"] or 0) > 0
        out["smart_ge1"] += sb >= 1
        out["smart_ge_min"] += sb >= cfg.signal_min_wallets
        out["net_ge_min"] += net >= cfg.signal_min_net_usd
        out["score_ge_min"] += s >= thr
        out["smart_and_net"] += sb >= cfg.signal_min_wallets and net >= cfg.signal_min_net_usd
        if s >= thr and sb >= cfg.signal_min_wallets and net >= cfg.signal_min_net_usd:
            out["all_pass"] += 1
            coins_pass.add(r["mint"])
        if (s >= (thr_model if is_model else thr) and sb >= cfg.signal_min_wallets
                and net >= net_min):
            out["all_pass_calibrated"] += 1
            if r["model_exp_net"] is not None and r["model_exp_net"] < engine.MODEL_MIN_EXP_NET:
                out["blocked_loss_veto"] += 1  # passed every minimum, stopped only by the model's expected loss
        out["max_score"] = max(out["max_score"], s)
        key = "max_score_model" if is_model else "max_score_heuristic"
        out[key] = max(out[key], s)
        if is_model:
            model_scores.append(s)
    out["coins_pass"] = len(coins_pass)
    out["model_score_p50"], out["model_score_p90"], out["model_score_p99"] = (
        _pct(model_scores, 0.5), _pct(model_scores, 0.9), _pct(model_scores, 0.99))
    tr = conn.execute(
        """SELECT COUNT(*) n, COUNT(DISTINCT t.mint) coins, COUNT(DISTINCT t.wallet) wallets,
                  SUM(CASE WHEN w.tier IN ('ultra','smart') THEN 1 ELSE 0 END) smart_trades,
                  COUNT(DISTINCT CASE WHEN w.tier IN ('ultra','smart') THEN t.wallet END) smart_wallets,
                  COUNT(DISTINCT CASE WHEN w.tier IN ('ultra','smart') THEN t.mint END) smart_coins
           FROM trades t LEFT JOIN wallets w ON w.address=t.wallet WHERE t.ts>=?""", (since,)).fetchone()
    out.update(trades=tr["n"], trade_coins=tr["coins"], trade_wallets=tr["wallets"], smart_trades=tr["smart_trades"] or 0,
               smart_trade_wallets=tr["smart_wallets"] or 0, smart_trade_coins=tr["smart_coins"] or 0)
    out["signals"] = conn.execute("SELECT COUNT(*) FROM signals WHERE ts>=? AND COALESCE(kind,'v1')='v1'", (since,)).fetchone()[0]
    out["signals_v2"] = conn.execute("SELECT COUNT(*) FROM signals WHERE ts>=? AND kind='v2'", (since,)).fetchone()[0]
    out["tiers"] = {t: n for t, n in conn.execute("SELECT tier, COUNT(*) FROM wallets WHERE tier IS NOT NULL GROUP BY tier")}
    return out


RET_CAP = 3.0  # a mean is capped at +300 % per call: three +3,000 % prints turned a -14 % backtest into +1 %
COST = 0.06  # round trip: fees + slippage + priority, as in copytrade paper trading


def _med(vals):
    vals = sorted(v for v in vals if v is not None)
    return round(vals[len(vals) // 2], 3) if vals else None


def peak_after_call(rows):
    """How high calls ran after the call, for selling at the high by hand: hit rates of +25 / +50 / +100 % at the
    1 h peak, median peaks in 15 min / 1 h / 4 h, minutes to the 1 h peak, the dip before it, and how the alert's
    target / stop played out (selling exactly at the target, after costs)."""
    n = len(rows)
    mx = [r["max_ret_1h"] or 0 for r in rows]
    out = dict(hit_25=round(sum(x >= 0.25 for x in mx) / n, 3), hit_100=round(sum(x >= 1.0 for x in mx) / n, 3),
               hit_150=round(sum(x >= 1.5 for x in mx) / n, 3),
               median_lag_first_s=_med(r["lag_first_s"] for r in rows),
               median_lag_last_s=_med(r["lag_last_s"] for r in rows),
               median_max_ret_15m=_med(r["max_ret_15m"] for r in rows),
               median_max_ret_4h=_med(r["max_ret_4h"] for r in rows),
               median_peak_min_1h=_med(r["peak_min_1h"] for r in rows),
               median_dip_before_peak=_med(r["dd_before_peak_1h"] for r in rows))
    planned = [r for r in rows if r["target_hit_ts"] or r["stop_hit_ts"]]
    if planned:
        out.update(target_hits=sum(1 for r in planned if r["target_hit_ts"]),
                   stop_hits=sum(1 for r in planned if r["stop_hit_ts"]))
    return out


def outcomes(conn, hours=168, cost=COST, now=None, bands=(0, 20, 30, 40, 50, 60, 101), kind="v1"):
    """Signal results by score band after costs: hit rate (+50% max within 1h), median max_ret_1h, mean ret_1h
    minus costs. This is what the score bands get calibrated on. One kind at a time (v1 burst, v2 accumulation)."""
    now = now or int(time.time())
    rows = conn.execute("SELECT score, ret_1h, max_ret_1h, max_ret_15m, max_ret_4h, peak_min_1h, dd_before_peak_1h, "
                        "target_hit_ts, stop_hit_ts, lag_first_s, lag_last_s FROM signals WHERE ts>=? AND ts<=? AND ret_1h IS NOT NULL "
                        "AND COALESCE(kind,'v1')=?", (now - int(hours * 3600), now - 3900, kind)).fetchall()
    missing = conn.execute("SELECT COUNT(*) FROM signals WHERE ts>=? AND ts<=? AND ret_1h IS NULL AND COALESCE(kind,'v1')=?",
                           (now - int(hours * 3600), now - 3900, kind)).fetchone()[0]
    out = []
    groups = [(f"{lo}-{hi - 1 if hi > 100 else hi}", [r for r in rows if lo <= (r["score"] or 0) < hi])
              for lo, hi in zip(bands, bands[1:])]
    for name, b in groups + ([("all", rows)] if rows else []):
        if not b:
            continue
        mx = sorted(r["max_ret_1h"] or 0 for r in b)
        d = dict(band=name, n=len(b),
                 hit_50=round(sum(1 for r in b if (r["max_ret_1h"] or 0) >= 0.5) / len(b), 3),
                 median_max_ret_1h=round(mx[len(mx) // 2], 3),
                 mean_ret_1h_after_cost=round(sum(min(r["ret_1h"], RET_CAP) for r in b) / len(b) - cost, 3),
                 dead=sum(1 for r in b if r["ret_1h"] <= -0.9))
        d.update(peak_after_call(b))
        if name == "all":  # unmeasured calls (no price near +1 h, not provably dead) count as misses here
            d.update(n_unmeasured=missing, hit_50_with_unmeasured=round(
                sum(1 for r in b if (r["max_ret_1h"] or 0) >= 0.5) / (len(b) + missing), 3))
        out.append(d)
    if missing:  # due but unmeasurable (no price near +1h): listed, so a gap never hides a loser silently
        out.append(dict(band="no 1h price", n=missing))
    return out


def _group_stats(rows):
    mx = [r["max_ret_1h"] or 0 for r in rows]
    return dict(n=len(rows), hit_50=round(sum(x >= 0.5 for x in mx) / len(rows), 3) if rows else None,
                median_max_ret_1h=_med(mx), median_ret_1h=_med(r["ret_1h"] for r in rows))


# hypotheses from the data thread (17.10., 44 h): 3+ independent smart groups 33 % to +50 % (n 55), net $500-2000
# with 2+ groups 6 of 9, mcap > $1M slightly positive (n 28). Tracked on the calls so a confidence tier can be decided
CONFIDENCE_GROUPS = {
    "entities>=3": lambda r: (r["smart_buyers"] or 0) >= 3,
    "entities=2": lambda r: (r["smart_buyers"] or 0) == 2,
    "net 500-2000, entities>=2": lambda r: (r["smart_buyers"] or 0) >= 2 and 500 <= (r["net_usd"] or 0) <= 2000,
    "mcap>=1M": lambda r: (r["mcap_usd"] or 0) >= 1e6,
}


def by_confidence(conn, hours=168, now=None, kind="v1"):
    now = now or int(time.time())
    rows = conn.execute("SELECT smart_buyers, net_usd, mcap_usd, ret_1h, max_ret_1h FROM signals WHERE ts>=? AND ts<=? "
                        "AND ret_1h IS NOT NULL AND COALESCE(kind,'v1')=?",
                        (now - int(hours * 3600), now - 3900, kind)).fetchall()
    return {k: _group_stats([r for r in rows if f(r)]) for k, f in CONFIDENCE_GROUPS.items()}


REPORT_EVERY = 3600
TRADABLE_LIQ_USD = float(os.getenv("CT_MIN_LIQUIDITY_USD", 15000))  # the strict copytrade account's liquidity floor


def report_path():
    from pathlib import Path
    return Path(settings.db_path).parent / "reports" / "signals_report.json"


def report_due(path=None, every=REPORT_EVERY):
    """By the report file's age, so frequent restarts (auto-updates) never starve the hourly report."""
    p = path or report_path()
    try:
        return time.time() - p.stat().st_mtime >= every
    except OSError:
        return True


def write_report(conn, path=None, now=None):
    """Hourly self-report for threads that may read logs but not the DB: reports/signals_report.json next to
    the DB (data/ on the Mac; funnel 4 h / 24 h, outcomes by band 24 h / 7 d, newest EV calibration). Small, no secrets, overwritten."""
    from pathlib import Path
    now = now or int(time.time())
    path = Path(path) if path else report_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    cal = None
    try:
        r = conn.execute("SELECT ts, active, n_all, oos_json, notes FROM signal_calibration ORDER BY id DESC LIMIT 1").fetchone()
        if r:
            cal = dict(ts=r[0], active=bool(r[1]), n_all=r[2], oos=json.loads(r[3] or "{}"), notes=r[4])
    except Exception:  # noqa: BLE001 - no calibration yet
        pass
    from signals import accum, engine
    rep = dict(ts=now, last_cycle=dict(engine.last_funnel), last_cycle_v2=dict(accum.last_funnel),
               outcomes_v2_24h=outcomes(conn, 24, now=now, kind="v2"), outcomes_v2_7d=outcomes(conn, 168, now=now, kind="v2"), funnel_4h=funnel(conn, 4, now=now), funnel_24h=funnel(conn, 24, now=now),
               outcomes_24h=outcomes(conn, 24, now=now), outcomes_7d=outcomes(conn, 168, now=now), calibration=cal,
               recent_signals=[dict(r, tradable_strict=(r["liquidity_usd"] or 0) >= TRADABLE_LIQ_USD)
                               for r in conn.execute(
                   "SELECT ts, symbol, score, ev, ev_n, smart_buyers, net_usd, liquidity_usd, mcap_usd, ret_1h, "
                   "max_ret_15m, max_ret_1h, max_ret_4h, peak_min_1h, dd_before_peak_1h, call_no, target_ret, stop_ret, lag_last_s, "
                   "target_hit_ts, stop_hit_ts, outcome_flag, reason, COALESCE(kind,'v1') AS kind FROM signals ORDER BY ts DESC LIMIT 20")])
    from signals import plan
    rep["plan_next"] = plan.levels(conn, now)
    rep["plan_next_v2"] = plan.levels(conn, now, kind="v2")
    rep["confidence_7d"] = by_confidence(conn, 168, now)
    from signals import exits  # exit rules replayed on the calls; switch only once verdict() says it is decidable
    rep["exits_7d"] = exits.compare(conn, 7, now, kind="v1")
    rep["exits_verdict_14d"] = exits.verdict(conn, 14, now, kind="v1")
    # inflow band class: goal >= +5 % net over 50 paper cases with the trail from +100 % (plan 18.10.)
    rep.update(outcomes_band_7d=outcomes(conn, 168, now=now, kind="band"), exits_band_14d=exits.compare(conn, 14, now, kind="band"),
               plan_next_band=plan.levels(conn, now, kind="band"))
    from signals import shadow  # rejected coins by reason and what they did next (calls: outcomes_* above)
    rep.update(shadow_24h=shadow.summary(conn, 24, now), shadow_7d=shadow.summary(conn, 168, now),
               shadow_v2_7d=shadow.summary(conn, 168, now, kind="v2"))
    rs = rep["recent_signals"]
    rep["tradable_strict_share"] = round(sum(r["tradable_strict"] for r in rs) / len(rs), 3) if rs else None
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(rep, ensure_ascii=False, indent=1, default=str))
    tmp.replace(path)
    return path


def main():
    ap = argparse.ArgumentParser(description="signal funnel: where coins drop out")
    ap.add_argument("--hours", type=float, default=10)
    ap.add_argument("--outcomes", action="store_true", help="signal results by score band after costs")
    ap.add_argument("--report", action="store_true", help="write data/reports/signals_report.json now")
    a = ap.parse_args()
    conn = db.connect()
    if a.report:
        print(write_report(conn))
        return
    if a.outcomes:
        for b in outcomes(conn, a.hours):
            print(b)
        return
    f = funnel(conn, a.hours)
    for k, v in f.items():
        print(f"{k:22s} {v}")


if __name__ == "__main__":
    main()
