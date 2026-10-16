"""Why are there (no) signals? Per-condition funnel over the stored signal_metrics rows.

    .venv/bin/python -m signals.funnel --hours 10

Every cycle stores one signal_metrics row per watched coin AFTER model + safety + market, so this shows
exactly where coins drop out: smart buyers, distinct entities, net inflow, score threshold, cooldown.
"""
import argparse
import json
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
    rows = conn.execute("SELECT ts, mint, net_usd, smart_buyers, score, n_ultra, n_smart, n_good, contributions_json "
                        "FROM signal_metrics WHERE ts>=?", (since,)).fetchall()
    out = dict(hours=hours, rows=len(rows), cycles=len({r["ts"] for r in rows}), coins=len({r["mint"] for r in rows}),
               model_rows=0, smart_ge1=0, smart_ge_min=0, net_ge_min=0, score_ge_min=0,
               smart_and_net=0, all_pass=0, max_score=0.0, max_score_model=0.0, max_score_heuristic=0.0,
               any_good=0, min_wallets=cfg.signal_min_wallets, min_net=cfg.signal_min_net_usd,
               min_score=cfg.signal_min_score, min_score_model=cfg.model_signal_min_score)
    from signals import engine
    thr_model = engine.cfg_min_model(conn, cfg)
    net_min = engine.min_net_usd(conn, cfg, now)
    out.update(min_net_calibrated=net_min, min_score_model_calibrated=thr_model, model_base_rate_1h=engine._model_thr.get("base"),
               all_pass_calibrated=0)
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
    out["signals"] = conn.execute("SELECT COUNT(*) FROM signals WHERE ts>=?", (since,)).fetchone()[0]
    out["tiers"] = {t: n for t, n in conn.execute("SELECT tier, COUNT(*) FROM wallets WHERE tier IS NOT NULL GROUP BY tier")}
    return out


COST = 0.06  # round trip: fees + slippage + priority, as in copytrade paper trading


def outcomes(conn, hours=168, cost=COST, now=None, bands=(0, 20, 30, 40, 50, 60, 101)):
    """Signal results by score band after costs: hit rate (+50% max within 1h), median max_ret_1h, mean ret_1h
    minus costs. This is what the score bands get calibrated on."""
    now = now or int(time.time())
    rows = conn.execute("SELECT score, ret_1h, max_ret_1h FROM signals WHERE ts>=? AND ret_1h IS NOT NULL",
                        (now - int(hours * 3600),)).fetchall()
    out = []
    for lo, hi in zip(bands, bands[1:]):
        b = [r for r in rows if lo <= (r["score"] or 0) < hi]
        if not b:
            continue
        mx = sorted(r["max_ret_1h"] or 0 for r in b)
        out.append(dict(band=f"{lo}-{hi - 1 if hi > 100 else hi}", n=len(b),
                        hit_50=round(sum(1 for r in b if (r["max_ret_1h"] or 0) >= 0.5) / len(b), 3),
                        median_max_ret_1h=round(mx[len(mx) // 2], 3),
                        mean_ret_1h_after_cost=round(sum(r["ret_1h"] for r in b) / len(b) - cost, 3)))
    return out


def main():
    ap = argparse.ArgumentParser(description="signal funnel: where coins drop out")
    ap.add_argument("--hours", type=float, default=10)
    ap.add_argument("--outcomes", action="store_true", help="signal results by score band after costs")
    a = ap.parse_args()
    conn = db.connect()
    if a.outcomes:
        for b in outcomes(conn, a.hours):
            print(b)
        return
    f = funnel(conn, a.hours)
    for k, v in f.items():
        print(f"{k:22s} {v}")


if __name__ == "__main__":
    main()
