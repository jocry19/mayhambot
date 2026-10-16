"""Synthetic demo database for developing and testing the dashboard and the terminal.

Never used by the running app: it writes only to the path you give and refuses the app's own DB.

    python -m web.devdata /tmp/mayham-demo.db          # 3 days of plausible data
    MAYHAM_DB_PATH=/tmp/mayham-demo.db python -m web   # dashboard on the demo DB

All tables follow core/README.md, model/README.md and copytrade/README.md.
"""
from __future__ import annotations

import json
import math
import os
import re
import random
import sqlite3
import sys
import time

SYMS = ("PEPE2 GIGA MOODENG FWOG CHILLGUY PNUT GOAT ZEREBRO BONKO WIF2 POPCAT MEW BRETT SLERF MICHI RETARDIO "
        "SIGMA HAMMY LUCE FRED NUB DADDY AURA SPX MOTHER BILLY SC CHONKY GME2 KEKIUS BOBO NEIRO BAN FARTCOIN "
        "ANSEM MOG WOJAK TURBO SNEK APU").split()
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _addr(r):
    return "".join(r.choice(B58) for _ in range(44))



def _named(conn, sql: str) -> str:
    """Positional INSERT -> named columns (the first N of the table), so columns other packages append later
    never break the demo DB: "INSERT INTO t VALUES (?,?)" -> "INSERT INTO t(a,b) VALUES (?,?)"."""
    m = re.match(r"(INSERT (?:OR \w+ )?INTO )(\w+)( VALUES \(([?,]+)\))", sql)
    cols = [c[1] for c in conn.execute(f"PRAGMA table_info({m.group(2)})")][: m.group(4).count("?")]
    return f"{m.group(1)}{m.group(2)}({','.join(cols)}){m.group(3)}"

def build(path: str, days: float = 3.0, seed: int = 7, n_tokens: int = 36, n_wallets: int = 700) -> str:
    try:
        from .db import db_path
        if os.path.abspath(path) == os.path.abspath(db_path()) and not os.getenv("MAYHAM_DB_PATH"):
            raise SystemExit("refusing to overwrite the app database")
    except ImportError:
        pass
    if os.path.exists(path):
        os.remove(path)
    os.environ["MAYHAM_DB_PATH"] = path
    r = random.Random(seed)
    from core import db as core_db  # schema
    conn = core_db.connect(path)
    try:
        from research import store as rstore
        rstore.ensure_schema(conn)
    except Exception:
        pass
    from copytrade import control, store as ct_store
    from copytrade.config import load
    s = load()
    s.db_path = path
    ct_store.connect(s)
    control.init(conn)
    # make sure ct_positions has all migrated columns
    ct_store.connect(s).close()

    now = int(time.time())
    t_start = now - int(days * 86400)
    sol = 182.0

    # ------------------------------------------------------------------ wallets
    tiers = (["ultra"] * 14) + (["smart"] * 45) + (["good"] * 110)
    wallets = []
    for i in range(n_wallets):
        tier = tiers[i] if i < len(tiers) else None
        base = {"ultra": 80, "smart": 66, "good": 53}.get(tier, r.uniform(5, 47))
        score = min(99, base + r.uniform(-4, 9)) if tier else base
        n_tok = r.randint(6, 40) if tier == "ultra" else r.randint(4, 25) if tier == "smart" else r.randint(3, 15) if tier else r.randint(1, 12)
        wr = {"ultra": r.uniform(.5, .75), "smart": r.uniform(.38, .6), "good": r.uniform(.27, .48)}.get(tier, r.uniform(0, .4))
        infl = {"ultra": r.uniform(15, 60), "smart": r.uniform(5, 25), "good": r.uniform(1, 10)}.get(tier, r.uniform(0, 4))
        wallets.append({"address": _addr(r), "tier": tier, "score": score, "n_tokens": n_tok, "win_rate": wr,
                        "early_rate": wr * r.uniform(.4, 1.0), "roi": (wr - .3) * r.uniform(1, 6),
                        "pnl": (wr - .3) * r.uniform(2000, 90000) * (3 if tier == "ultra" else 1),
                        "influence_followers": infl, "influence_usd": infl * r.uniform(300, 1500),
                        "speed": {"ultra": 0.1, "smart": 0.25, "good": 0.45}.get(tier, 0.7)})
    tiered = [w for w in wallets if w["tier"]]
    crowd = [w for w in wallets if not w["tier"]]

    # ------------------------------------------------------------------ tokens + price paths
    tokens = []
    for i in range(n_tokens):
        born = t_start + int(r.uniform(-0.5, 0.9) * (now - t_start))
        mint = _addr(r)[:40] + "pump"
        p0 = r.uniform(4e-6, 3e-5)
        will_pump = r.random() < 0.45
        pump_at = born + r.randint(1800, 8 * 3600) if will_pump else None
        pump_x = r.uniform(2.0, 12.0) if will_pump else 1.0
        tokens.append({"mint": mint, "symbol": SYMS[i % len(SYMS)] + ("" if i < len(SYMS) else str(i)),
                       "born": born, "p0": p0, "pump_at": pump_at, "pump_x": pump_x, "pts": []})

    def price_at(t, tok):
        return _price(tok, t)

    for tok in tokens:
        # random walk every 60 s from max(born, t_start) to now
        p = tok["p0"]
        t = max(tok["born"], t_start)
        pts = []
        rr = random.Random(hash(tok["mint"]) & 0xffffffff)
        while t <= now:
            drift = -0.0004
            if tok["pump_at"] and tok["pump_at"] <= t < tok["pump_at"] + 1800:
                drift = math.log(tok["pump_x"]) / 30
            elif tok["pump_at"] and tok["pump_at"] + 1800 <= t < tok["pump_at"] + 5400:
                drift = -math.log(tok["pump_x"]) / 90 * 0.6
            p *= math.exp(drift + rr.gauss(0, 0.025))
            pts.append((t, p))
            t += 60
        tok["pts"] = pts

    # ------------------------------------------------------------------ trades
    trades = []
    sig_i = 0
    for tok in tokens:
        pts = tok["pts"]
        if not pts:
            continue
        for k, (t, p) in enumerate(pts):
            pre_pump = tok["pump_at"] and tok["pump_at"] - 1800 <= t < tok["pump_at"]
            in_pump = tok["pump_at"] and tok["pump_at"] <= t < tok["pump_at"] + 1800
            n = r.choices([0, 1, 2, 3], [0.55, 0.3, 0.1, 0.05])[0] + (3 if in_pump else 0)
            for _ in range(n):
                if pre_pump and r.random() < 0.5:
                    w = r.choice(tiered)
                elif r.random() < 0.06:
                    w = r.choice(tiered)
                else:
                    w = r.choice(crowd)
                side = "buy" if r.random() < (0.75 if (pre_pump or in_pump) else 0.5) else "sell"
                usd = r.lognormvariate(5.3, 1.1) * (3 if w["tier"] == "ultra" else 1)
                sig_i += 1
                trades.append((f"sig{sig_i}", w["address"], tok["mint"], side, t + r.randint(0, 59),
                               usd / p, usd / sol, usd, p * r.uniform(0.995, 1.005), "geckoterminal"))
    conn.executemany(_named(conn, "INSERT OR IGNORE INTO trades VALUES (?,?,?,?,?,?,?,?,?,?)"), trades)

    # tokens table + snapshots
    for tok in tokens:
        if not tok["pts"]:
            continue
        last = tok["pts"][-1][1]
        p1h = tok["pts"][-61][1] if len(tok["pts"]) > 61 else tok["pts"][0][1]
        p5 = tok["pts"][-6][1] if len(tok["pts"]) > 6 else tok["pts"][0][1]
        p24 = tok["pts"][-1441][1] if len(tok["pts"]) > 1441 else tok["pts"][0][1]
        conn.execute("""INSERT OR REPLACE INTO tokens(mint, symbol, name, pool_address, dex, price_usd, mcap_usd, liquidity_usd,
                        pair_created_at, price_change_m5, price_change_h1, price_change_h24, volume_h1, first_seen, last_updated, tracked)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
                     (tok["mint"], tok["symbol"], tok["symbol"].title() + " Coin", _addr(r), "raydium", last, last * 1e9,
                      last * 1e9 * r.uniform(0.08, 0.2), tok["born"], (last / p5 - 1) * 100, (last / p1h - 1) * 100,
                      (last / p24 - 1) * 100, r.uniform(5e3, 4e5), tok["born"], now))
        conn.executemany(_named(conn, "INSERT OR REPLACE INTO token_prices VALUES (?,?,?,?,?)"),
                         [(tok["mint"], t, p, p * 1e9, p * 1e9 * 0.12) for t, p in tok["pts"][::5]])

    # wallets tables
    for w in wallets:
        conn.execute("""INSERT OR REPLACE INTO wallets(address, label, source, manual, score, tier, is_smart, is_bot, tokens_traded,
                        win_rate, early_rate, realized_pnl_usd, total_pnl_usd, roi, median_peak_multiple, consistency,
                        last_trade_ts, first_seen, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                     (w["address"], "", "auto", 0, w["score"], w["tier"], 1 if w["tier"] in ("ultra", "smart") else 0, 0,
                      w["n_tokens"], w["win_rate"], w["early_rate"], w["pnl"] * .8, w["pnl"], w["roi"], 1 + w["roi"],
                      r.uniform(.2, .9), now - r.randint(0, 86400), t_start, now))
    # wallet positions from trades
    from collections import defaultdict
    pos = defaultdict(lambda: {"buy": 0.0, "sell": 0.0, "bt": 0.0, "st": 0.0, "first": None, "fp": None, "n": 0})
    for t in trades:
        d = pos[(t[1], t[2])]
        d["n"] += 1
        if t[3] == "buy":
            d["buy"] += t[7]
            d["bt"] += t[5]
            if d["first"] is None or t[4] < d["first"]:
                d["first"], d["fp"] = t[4], t[8]
        else:
            d["sell"] += t[7]
            d["st"] += t[5]
    tokp = {tok["mint"]: tok["pts"][-1][1] for tok in tokens if tok["pts"]}
    conn.executemany(_named(conn, "INSERT OR REPLACE INTO wallet_positions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"),
                     [(k[0], k[1], d["buy"], d["sell"], d["bt"], d["st"], d["sell"] - d["buy"] * min(1, d["st"] / d["bt"]) if d["bt"] else 0,
                       max(0, d["bt"] - d["st"]) * tokp.get(k[1], 0), (d["sell"] / d["buy"] - 1) if d["buy"] else None,
                       d["first"], d["fp"], r.uniform(1, 6), d["n"]) for k, d in pos.items()])

    # ------------------------------------------------------------------ model: versions, wallet stats, predictions
    tier_of = {w["address"]: w for w in wallets}
    n_versions = 6
    for v in range(1, n_versions + 1):
        ts = t_start + int(v / n_versions * (now - t_start)) - 1800
        auc = 0.58 + 0.035 * v + r.uniform(-0.01, 0.01)
        tc = {"ultra": 6 + 2 * v - (2 if v == n_versions else 0), "smart": 25 + 4 * v, "good": 60 + 10 * v}
        if v == n_versions:
            tc = {"ultra": 14, "smart": 45, "good": 110}
        def hm(a, base):
            q = [dict(quintile=k + 1, n=8 * v, pump_rate=max(0.02, base * (2.6 - 0.45 * k) * (0.8 + 0.04 * v)),
                      median_max_ret=0.9 - 0.18 * k) for k in range(5)]
            return dict(n=40 * v, base_rate=base, auc=round(a, 3), auc_baseline=round(a - 0.05 - 0.01 * v, 3),
                        brier=round(.2 - .006 * v, 3), top10_pump_rate=round(min(.9, base * (2.2 + .15 * v)), 3),
                        top10_n=4 * v, top10_median_max_ret=1.1, precision_p50=[.5 + .03 * v, 3 * v], quintiles=q,
                        learned_share=min(1, .2 * v))
        metrics = {"15m": hm(auc - .03, .22), "1h": hm(auc, .18), "6h": hm(auc - .02, .12),
                   "selection": {h: dict(status="selected", chosen={"algo": "gbm" if (h == "1h" and v >= 4) else "logistic",
                                                                    "drop": "crowd" if h == "6h" and v > 2 else None},
                                         why="neuer Sieger" if (h == "1h" and v == 4) else "Champion bleibt (kein Kandidat klar besser)",
                                         n=120 * v, pos=22 * v, n_train=84 * v, n_test=36 * v, base_logloss=.48,
                                         holdout=dict(logloss=round(.46 - .01 * v, 3), auc=round(auc, 3)))
                                 for h in ("15m", "1h", "6h")},
                   "tier_forward": {t: dict(n=30 * v, pump_rate=pr + .01 * v, ci_low=pr - .08, ci_high=pr + .1, median_max_ret=pr * 2)
                                    for t, pr in (("ultra", .41), ("smart", .29), ("good", .2), ("none", .1))}}
        coef = [round(x + .03 * v, 3) for x in (.9, .5, .2, .25, .15, .05, .2, .01, .08, -.35, -.1, -.8, -.01, -.3, .1, .03, .2)]
        conn.execute("""INSERT INTO model_versions(ts, active, n_events, n_wallets, tier_counts_json, wallet_weights_json,
                        token_model_json, tier_rules_json, metrics_json, notes) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                     (ts, 1 if v == n_versions else 0, 120 * v, 300 + 70 * v, json.dumps(tc),
                      json.dumps({"weights": {"win": .22 - .01 * v, "early": .18 + .015 * v, "roi": .15, "consistency": .1,
                                              "timing": .12 + .01 * v, "influence": .13 + .006 * v, "rug_avoid": .1 - .005 * v},
                                  "info": {"n": 50 * v, "learned": v > 1}}),
                      json.dumps({h: {"kind": "logistic" if v > 1 else "prior", "bias": -1.8, "coef": coef, "n": 120 * v, "pos": 22 * v,
                                      "cfg": {"algo": "gbm" if (h == "1h" and v >= 4) else "logistic", "drop": "crowd" if h == "6h" and v > 2 else None}}
                                  for h in ("15m", "1h", "6h")}),
                      json.dumps({"ultra": {"min_score": 72}, "smart": {"min_score": 60}, "good": {"min_score": 48}}),
                      json.dumps(metrics), f"Retrain {v}: walk-forward, {120 * v} Events"))
        sample = r.sample(tiered, min(len(tiered), tc["ultra"] + tc["smart"] + tc["good"]))
        for w in sample:
            conn.execute(_named(conn, "INSERT OR REPLACE INTO model_tier_history VALUES (?,?,?,?)"),
                         (v, w["address"], w["tier"], w["score"] - (n_versions - v) * r.uniform(0, 2)))
    mws_extra = "exit_capture" in {c[1] for c in conn.execute("PRAGMA table_info(model_wallet_stats)")}
    for w in wallets:
        if w["score"] < 30 and not w["tier"]:
            continue
        comp = {k: round(r.uniform(.2, 1) * (w["score"] / 100), 3) for k in ("win", "early", "roi", "consistency", "timing", "influence", "rug_avoid")}
        cols = ["address", "version", "ts", "tier", "score", "n_tokens", "win_rate", "early_rate", "roi", "realized_pnl_usd",
                "total_pnl_usd", "median_entry_mcap", "median_min_to_2x", "influence_followers", "influence_usd",
                "inflow_lift_usd", "consistency", "rug_rate", "components_json"]
        vals = [w["address"], n_versions, now - 1800, w["tier"], w["score"], w["n_tokens"], w["win_rate"],
                w["early_rate"], w["roi"], w["pnl"] * .8, w["pnl"], r.uniform(2e4, 4e5), r.uniform(3, 60),
                w["influence_followers"], w["influence_usd"], w["influence_usd"] * r.uniform(.3, 1.2),
                r.uniform(.3, .9), r.uniform(0, .2), json.dumps(comp)]
        if mws_extra:  # newer research schema: cluster_id, exit_capture, median_hold_min
            cols += ["cluster_id", "exit_capture", "median_hold_min"]
            vals += [f"c{r.randint(1, 6)}" if r.random() < .15 else None, r.uniform(.35, .9), r.uniform(4, 180)]
        conn.execute(f"INSERT OR REPLACE INTO model_wallet_stats({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals)

    # ------------------------------------------------------------------ signal metrics + signals (+ predictions)
    by_mint = defaultdict(list)
    for t in trades:
        by_mint[t[2]].append(t)
    for m in by_mint:
        by_mint[m].sort(key=lambda t: t[4])
    import bisect
    feats = [("net_sol_ultra", "Netto-SOL Ultra"), ("n_ultra", "Ultra-Käufer"), ("n_smart", "Smart-Käufer"),
             ("net_sol_smart", "Netto-SOL Smart"), ("influence", "Einfluss der Käufer"), ("entry_mcap_log", "Market Cap beim Einstieg"),
             ("sell_ratio", "Verkaufsanteil Tier-Wallets"), ("mean_score", "Ø Wallet-Score"), ("token_age", "Coin-Alter"),
             ("crowd_buyers", "Copy-Trader-Käufer")]
    for tok in tokens:
        tl = by_mint.get(tok["mint"], [])
        ts_list = [t[4] for t in tl]
        last_sig = None
        for t, p in tok["pts"][::5]:
            i, j = bisect.bisect_left(ts_list, t - 1800), bisect.bisect_right(ts_list, t)
            win = tl[i:j]
            per = defaultdict(float)
            for x in win:
                if x[1] in tier_of and tier_of[x[1]]["tier"]:
                    per[x[1]] += x[7] if x[3] == "buy" else -x[7]
            buyers = {a: v for a, v in per.items() if v > 0}
            n_t = {k: sum(1 for a in buyers if tier_of[a]["tier"] == k) for k in ("ultra", "smart", "good")}
            smart_net = sum(v for a, v in per.items() if tier_of[a]["tier"] in ("ultra", "smart"))
            sb = n_t["ultra"] + n_t["smart"]
            qs = [tier_of[a]["score"] for a in buyers if tier_of[a]["tier"] in ("ultra", "smart")]
            q = sum(qs) / len(qs) if qs else 0
            parts = {"inflow": (0.4, 1 - math.exp(-max(smart_net, 0) / 2500), f"Netto-Inflow Smart ${smart_net:,.0f}"),
                     "breadth": (0.3, 1 - math.exp(-sb / 4), f"{n_t['ultra']} Ultra + {n_t['smart']} Smart kaufen"),
                     "quality": (0.3, q / 100, f"Ø Wallet-Score {q:.0f}")}
            contrib = {k: {"weight": w_, "value": round(v_, 4), "points": round(100 * w_ * v_, 1), "why": why}
                       for k, (w_, v_, why) in parts.items()}
            score = round(sum(c["points"] for c in contrib.values()), 1) if smart_net > 0 and sb else 0.0
            pred = 1 / (1 + math.exp(-(score - 62) / 9)) if score else None
            conn.execute("""INSERT OR REPLACE INTO signal_metrics(ts, mint, window_min, buy_usd, sell_usd, net_usd, smart_buyers,
                            avg_quality, score, price_usd, buy_sol, net_sol, mcap_usd, n_ultra, n_smart, n_good, prediction,
                            contributions_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                         (t, tok["mint"], 30, max(smart_net, 0), 0, smart_net, sb, q, score, p, max(smart_net, 0) / sol,
                          smart_net / sol, p * 1e9, n_t["ultra"], n_t["smart"], n_t["good"], pred, json.dumps(contrib)))
            if score >= 52 and sb >= 2 and smart_net >= 400 and (last_sig is None or t - last_sig >= 3600):
                last_sig = t
                fut = [pp for tt, pp in tok["pts"] if t < tt <= t + 86400]
                f1h = [pp for tt, pp in tok["pts"] if t < tt <= t + 3600]

                def ret(dt):
                    xs = [pp for tt, pp in tok["pts"] if tt >= t + dt]
                    return xs[0] / p - 1 if xs and t + dt <= now else None
                wl = [{"wallet": a, "tier": tier_of[a]["tier"], "score": round(tier_of[a]["score"], 1), "n_tokens": tier_of[a]["n_tokens"],
                       "buy_usd": round(v, 2), "buy_sol": round(v / sol, 3), "sell_usd": 0, "first_ts": t - r.randint(60, 1700)}
                      for a, v in sorted(buyers.items(), key=lambda kv: -kv[1])]
                conn.execute("""INSERT INTO signals(ts, mint, symbol, score, net_usd, buy_usd, smart_buyers, avg_quality, price_usd,
                                mcap_usd, wallets_json, buy_sol, net_sol, liquidity_usd, n_ultra, n_smart, n_good, prediction,
                                prediction_label, contributions_json, model_version, notified, ret_5m, ret_15m, ret_1h, ret_6h,
                                max_ret_1h, max_mcap_1h, max_ret_24h) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                             (t, tok["mint"], tok["symbol"], score, smart_net, smart_net, sb, q, p, p * 1e9, json.dumps(wl),
                              smart_net / sol, smart_net / sol, p * 1e9 * 0.12, n_t["ultra"], n_t["smart"], n_t["good"], pred,
                              f"P(+50% in 1h), Note {'ABCDE'[min(4, max(0, int((90 - score) // 10)))]}", json.dumps(contrib),
                              "core-heuristic-v1", 1, ret(300), ret(900), ret(3600), ret(21600),
                              (max(f1h) / p - 1) if f1h and t + 3600 <= now else None,
                              max(f1h) * 1e9 if f1h and t + 3600 <= now else None,
                              (max(fut) / p - 1) if fut and t + 86400 <= now else None))
            if score >= 35:
                vals = {"net_sol_ultra": sum(v for a, v in per.items() if tier_of[a]["tier"] == "ultra") / sol,
                        "n_ultra": n_t["ultra"], "n_smart": n_t["smart"],
                        "net_sol_smart": sum(v for a, v in per.items() if tier_of[a]["tier"] == "smart") / sol,
                        "influence": sum(tier_of[a]["influence_followers"] for a in buyers), "entry_mcap_log": math.log10(p * 1e9),
                        "sell_ratio": r.uniform(0, .4), "mean_score": q, "token_age": (t - tok["born"]) / 3600, "crowd_buyers": r.randint(0, 30)}
                cs = [(f, lab, vals[f], r.uniform(-.4, .9) if f not in ("sell_ratio", "entry_mcap_log") else -r.uniform(.1, .5)) for f, lab in feats]
                tot = sum(abs(c[3]) for c in cs)
                fl = [{"feature": f, "label": lab, "value": round(v, 4), "weight": round(c * 1.3, 3), "contribution": round(c, 4),
                       "share": round(abs(c) / tot, 4)} for f, lab, v, c in cs]
                p1 = 1 / (1 + math.exp(-(score - 60) / 8))
                f1h = [pp for tt, pp in tok["pts"] if t < tt <= t + 3600]
                conn.execute("""INSERT INTO model_predictions(ts, mint, symbol, version, score, p_15m, p_1h, p_6h, exp_ret_1h, price_usd,
                                mcap_usd, n_ultra, n_smart, n_good, net_sol, features_json, contributions_json, wallets_json, ret_15m,
                                ret_1h, ret_6h, max_ret_1h, max_ret_6h) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                             (t, tok["mint"], tok["symbol"], n_versions, round(p1 * 100, 1), p1 * .8, p1, min(.99, p1 * 1.1),
                              p1 * .9, p, p * 1e9, n_t["ultra"], n_t["smart"], n_t["good"], smart_net / sol, json.dumps(vals),
                              json.dumps({"1h": fl}), "[]", None, None, None,
                              (max(f1h) / p - 1) if f1h and t + 3600 <= now else None, None))

    # ------------------------------------------------------------------ copy trading (paper)
    whales = r.sample(tiered[:60], 30) + r.sample(crowd, 15)
    for w in whales:
        conn.execute("INSERT OR IGNORE INTO ct_wallets(address, label, source, added_at, active) VALUES (?,?,?,?,1)",
                     (w["address"], "", "core" if w["tier"] else "discover", t_start))
        n = r.randint(3, 25)
        hit = {"ultra": .62, "smart": .5, "good": .4}.get(w["tier"], .3) + r.uniform(-.1, .1)
        fp = r.uniform(.3, 1.4)
        ev = (hit * fp * .6 - (1 - hit) * .18) * r.uniform(.7, 1.1)
        conn.execute(_named(conn, "INSERT OR REPLACE INTO ct_wallet_rank VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"),
                     (w["address"], n, hit, fp, r.uniform(2, 25), r.uniform(.3, 6), fp * .6, -.18, ev, ev * n / (n + 5),
                      min(1.5, max(.15, .7 * fp)), r.uniform(15, 90), now - 600))
        for _ in range(n):
            ts = r.randint(t_start, now - 3600)
            pumped = r.random() < hit
            g = r.uniform(.25, 3) if pumped else r.uniform(0, .2)
            conn.execute(_named(conn, "INSERT OR REPLACE INTO ct_buy_outcomes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"),
                         (_addr(r), w["address"], r.choice(tokens)["mint"], ts, r.uniform(2, 30), "", 1e-5, 1.02e-5, g,
                          r.uniform(.5, 30), g * r.uniform(1, 1.8), r.uniform(1, 60), r.uniform(0, 20), r.uniform(-1, 8),
                          (g * .6 if pumped else -r.uniform(.05, .2)), 1 if pumped else 0, r.randint(30, 120), now))
    bal = 10.0
    eq_points = []
    closed_pnl = []
    pid = 0
    reasons_w = ["target", "inflow_peak", "trail", "stop", "time", "whale"]
    reasons_s = ["target", "trail", "stop", "time", "smart_exit"]
    t = t_start + 3600
    while t < now - 1800:
        t += r.randint(600, 3600)
        strat = "whale" if r.random() < 0.65 else "signal"
        tok = r.choice([x for x in tokens if x["pts"] and x["pts"][0][0] <= t])
        w = r.choice(whales) if strat == "whale" else None
        size = .5 if strat == "whale" else .4
        win = r.random() < (0.58 if strat == "whale" else 0.52)
        pct = r.uniform(.12, 1.1) if win else -r.uniform(.04, .22)
        hold = r.uniform(2, 40) if strat == "whale" else r.uniform(10, 200)
        closed_at = t + int(hold * 60)
        if closed_at > now:
            continue
        pnl = size * pct
        bal += pnl
        pid += 1
        p_in = _price(tok, t) or 1e-5
        conn.execute("""INSERT INTO ct_positions(mode, strategy, managed, wallet, mint, symbol, whale_ts, opened_at, entry_price_sol,
                        entry_price_usd, entry_mcap_usd, size_sol, tokens, token_decimals, entry_cost_sol, take_profit, stop_loss,
                        max_hold_min, high_price_sol, last_price_sol, status, closed_at, exit_price_sol, exit_mcap_usd, proceeds_sol,
                        pnl_sol, pnl_pct, exit_reason, meta) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                     ("paper", strat, "auto", w["address"] if w else "", tok["mint"], tok["symbol"], t - r.randint(4, 40) if w else None,
                      t, p_in / sol, p_in, p_in * 1e9, size, size * sol / p_in, 6, size, .35, .2, 60, p_in / sol * (1 + max(pct, 0) * 1.2),
                      p_in / sol * (1 + pct), "closed", closed_at, p_in / sol * (1 + pct), p_in * (1 + pct) * 1e9, size * (1 + pct),
                      pnl, pct, r.choice(reasons_w if strat == "whale" else reasons_s) if win or r.random() < .5 else "stop",
                      json.dumps({"fill_source": "jupiter", "impact": r.uniform(.001, .02)})))
        closed_pnl.append((closed_at, pnl))
        conn.execute("INSERT INTO ct_alerts(ts, strategy, mint, symbol, wallet, price_sol, price_usd, mcap_usd, score, action, reason, position_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (t, strat, tok["mint"], tok["symbol"], w["address"] if w else "", p_in / sol, p_in, p_in * 1e9,
                      r.uniform(50, 95), "bought", "Wal-Kauf 4.2 SOL, Wal-Score 0.31" if w else "Signal-Score 74, 4 Smart Wallets", pid))
        conn.execute(_named(conn, "INSERT INTO ct_events VALUES (?,?,?,?,?)"), (t, "info", "buy", f"KAUF [{strat}/paper] {tok['symbol']} {size:.2f} SOL", None))
        conn.execute(_named(conn, "INSERT INTO ct_events VALUES (?,?,?,?,?)"), (closed_at, "info", "sell", f"VERKAUF [{strat}/paper] {tok['symbol']} {pct:+.1%}", None))
    # equity every 10 min
    closed_pnl.sort()
    acc, k = 10.0, 0
    for ts in range(t_start, now, 600):
        while k < len(closed_pnl) and closed_pnl[k][0] <= ts:
            acc += closed_pnl[k][1]
            k += 1
        eq_points.append((ts, "paper", acc, 0.0, acc))
    conn.executemany(_named(conn, "INSERT OR REPLACE INTO ct_equity VALUES (?,?,?,?,?)"), eq_points)
    ct_store.set_state(conn, "paper_balance_sol", bal)
    # open positions
    for strat, managed, hint in (("whale", "auto", None), ("signal", "semi", "target"), ("signal", "auto", None)):
        tok = r.choice([x for x in tokens if x["pts"]])
        p_in = tok["pts"][-15][1] if len(tok["pts"]) > 15 else tok["pts"][0][1]
        p_now = tok["pts"][-1][1]
        w = r.choice(whales) if strat == "whale" else None
        conn.execute("""INSERT INTO ct_positions(mode, strategy, managed, wallet, mint, symbol, whale_ts, opened_at, entry_price_sol, entry_price_usd,
                        entry_mcap_usd, size_sol, tokens, token_decimals, entry_cost_sol, take_profit, stop_loss, max_hold_min, high_price_sol,
                        last_price_sol, last_price_usd, last_mcap_usd, status, exit_hint, exit_hint_at, meta) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                     ("paper", strat, managed, w["address"] if w else "", tok["mint"], tok["symbol"], now - 900 if w else None, now - 880,
                      p_in / sol, p_in, p_in * 1e9, .5, .5 * sol / p_in, 6, .5, .35, .2, 60, max(p_in, p_now) / sol, p_now / sol, p_now,
                      p_now * 1e9, "open", hint, now - 60 if hint else None, json.dumps({"fill_source": "jupiter"})))
    conn.execute(_named(conn, "INSERT INTO ct_events VALUES (?,?,?,?,?)"), (now - 60, "info", "hint", "VERKAUFS-HINWEIS: Ziel erreicht", None))
    control.set_mode(conn, "signal", "semi")
    try:
        from research.events import FEATURE_LABELS, FEATURE_NAMES
    except Exception:
        FEATURE_NAMES, FEATURE_LABELS = [], {}
    rep = {"version": n_versions, "horizon": "1h",
           "combos": {
               "Tier-Mix (Ultra/Smart/Good-Käufer)": [dict(bucket=b, n=nn, pumps=int(nn * rt), rate=rt, ci_low=rt - .1, median_max_ret=rt * 1.8)
                                                      for b, nn, rt in (("U0 S0 G1", 60, .08), ("U0 S1 G2", 44, .16), ("U0 S2 G3+", 31, .26),
                                                                        ("U1 S1 G1", 22, .36), ("U2+ S2 G3+", 12, .58))],
               "Smart-Money-Inflow": [dict(bucket=b, n=nn, pumps=int(nn * rt), rate=rt, ci_low=rt - .1, median_max_ret=rt * 2)
                                      for b, nn, rt in (("≤0 SOL", 50, .06), ("0–5 SOL", 70, .14), ("5–20 SOL", 40, .27), ("20–50 SOL", 18, .44), (">50 SOL", 8, .5))],
               "Market Cap beim Signal": [dict(bucket=b, n=nn, pumps=int(nn * rt), rate=rt, ci_low=rt - .1, median_max_ret=rt * 2)
                                          for b, nn, rt in (("<50k", 40, .31), ("50–200k", 62, .24), ("200k–1M", 50, .15), ("1–5M", 25, .09), (">5M", 9, .05))],
               "Beste Kombinationen": [dict(combo=cmb, n=nn, pumps=int(nn * rt), rate=rt, ci_low=rt - .15, median_max_ret=rt * 2.2)
                                       for cmb, nn, rt in (("U2+ S2 G3+ | 20–50 SOL | MC <50k", 6, .83), ("U1 S2 G3+ | 5–20 SOL | MC 50–200k", 9, .66),
                                                           ("U1 S1 G1 | 5–20 SOL | MC <50k", 8, .62), ("U0 S2 G3+ | 5–20 SOL | MC 50–200k", 14, .43))]},
           "univariate": sorted(({"feature": f, "label": FEATURE_LABELS[f], "auc": .5 + d_, "power": abs(d_) * 2,
                                  "direction": "höher = besser" if d_ >= 0 else "niedriger = besser"}
                                 for f, d_ in zip(FEATURE_NAMES, [.17, .12, .05, .15, .1, .03, .11, .06, .09, -.12, -.04, -.1, .02, -.05, .03, .01, .04])),
                                key=lambda d: -d["power"]),
           "paper": {"signals": [dict(grade=g, n=nn, hit_50=hr, median_max_ret_1h=mx, median_ret_1h=md)
                                 for g, nn, hr, mx, md in (("A", 9, .56, .74, .21), ("B", 21, .38, .41, .06), ("C", 34, .24, .22, -.02),
                                                           ("D", 48, .13, .1, -.06), ("E", 70, .06, .04, -.09))],
                     "paper": [dict(mode="paper", grade=g, n=nn, win_rate=wr, avg_pnl_pct=ap, pnl_sol=ap * nn * .5)
                               for g, nn, wr, ap in (("A", 7, .71, .29), ("B", 15, .6, .14), ("C", 12, .5, .03), ("ohne Signal", 64, .55, .08))]},
           "tier_profiles": {t: dict(n=nn, win_rate=wr, roi=ro, pnl=pn, entry_mcap=em, followers=fo, min_to_2x=m2)
                             for t, nn, wr, ro, pn, em, fo, m2 in (("ultra", 14, .61, 2.4, 410000, 62000, 31, 14), ("smart", 45, .47, 1.1, 520000, 140000, 12, 28),
                                                                   ("good", 110, .34, .45, 300000, 260000, 4, 45))}}
    try:
        conn.execute("INSERT INTO research_reports(ts, version, title, markdown, json) VALUES (?,?,?,?,?)",
                     (now - 1800, n_versions, "Forschungsbericht v%d" % n_versions,
                      "## Was Gewinner-Coins gemeinsam haben\n\n- **2+ Ultra-Wallets** in den ersten 30 min: 3,1× höhere Pump-Quote\n"
                      "- Einstieg unter **100k Market Cap**: beste Trefferquote\n- Verkaufsanteil der Tier-Wallets > 30 %: Pump-Quote halbiert\n\n"
                      "## Beste Indikator-Wallets\n\nDie 14 Ultra-Wallets kaufen im Median 11 min vor dem Copy-Ansturm.\n", json.dumps(rep)))
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()
    return path


def _price(tok, t):
    pts = tok["pts"]
    if not pts:
        return None
    i = min(len(pts) - 1, max(0, (t - pts[0][0]) // 60))
    return pts[int(i)][1]


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "/tmp/mayham-demo.db"
    print(build(out))
