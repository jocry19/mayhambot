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


def _edge_demo(conn, r):
    """Whale replays belong to real tiered wallets, and every model wallet has its own net PnL after costs:
    so the wallet views can show 'the whale earns' next to 'copying it earns us'."""
    cols = {c[1] for c in conn.execute("PRAGMA table_info(model_wallet_stats)")}
    if not cols:
        return
    top = [x[0] for x in conn.execute("SELECT address FROM model_wallet_stats WHERE tier IS NOT NULL ORDER BY score DESC LIMIT 12")]
    if "net_pnl_usd" in cols:
        for (a, pnl) in conn.execute("SELECT address, total_pnl_usd FROM model_wallet_stats").fetchall():
            net = (pnl or 0) * r.uniform(0.55, 0.9) - r.uniform(50, 900)
            conn.execute("UPDATE model_wallet_stats SET net_pnl_usd=?, net_roi=? WHERE address=?", (round(net, 2), round(net / 20000, 4), a))
    if top and conn.execute("SELECT 1 FROM sqlite_master WHERE name='ct_evals'").fetchone():
        keys = [x[0] for x in conn.execute("SELECT ref FROM ct_evals WHERE strategy='whale' ORDER BY ref")]
        for i, k in enumerate(keys):
            conn.execute("UPDATE ct_evals SET wallet=? WHERE strategy='whale' AND ref=?", (top[i % min(len(top), 6)], k))
    conn.commit()


def _inflight_demo(conn, r, tokens, now):
    """The short-lived states copytrade writes: an intent row before the swap ('opening'), a live buy that bought
    nothing ('failed') and buy commands nobody picked up in time ('expired')."""
    pcols = {c[1] for c in conn.execute("PRAGMA table_info(ct_positions)")}
    tok = [t for t in tokens if t["pts"]]
    for mode, st, why in (("paper", "opening", None), ("live", "failed", "Swap fehlgeschlagen: Slippage zu hoch")):
        t = r.choice(tok)
        row = {"mode": mode, "strategy": "whale", "mint": t["mint"], "symbol": t["symbol"], "opened_at": now - (20 if st == "opening" else 3600),
               "entry_price_sol": 0, "size_sol": .5, "tokens": 0, "entry_cost_sol": 0, "status": st, "exit_reason": why,
               "intent": json.dumps({"size_sol": .5, "source": "whale"})}
        row = {k: v for k, v in row.items() if k in pcols}
        conn.execute(f"INSERT INTO ct_positions({','.join(row)}) VALUES ({','.join('?' * len(row))})", list(row.values()))
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='ct_commands'").fetchone():
        ccols = {c[1] for c in conn.execute("PRAGMA table_info(ct_commands)")}
        for ts, st, res in ((now - 400, "expired", "abgelaufen"), (now - 1500, "done", "gekauft")):
            row = {"ts": ts, "action": "buy", "mint": r.choice(tok)["mint"], "size_sol": .2, "strategy": "signal", "status": st,
                   "result": res, "done_at": ts + (120 if st == "expired" else 5)}
            row = {k: v for k, v in row.items() if k in ccols}
            conn.execute(f"INSERT INTO ct_commands({','.join(row)}) VALUES ({','.join('?' * len(row))})", list(row.values()))


def _live_demo(conn, r, tokens, now):
    """live/ tables in their real format: a rehearsal (dry run) history with one failed simulation and one
    rejected order, so the control page's real-money card has orders and a partly met go-live checklist."""
    try:
        from live import store
    except Exception:
        return
    store.init(conn)
    tok = [t for t in tokens if t["pts"]]
    plan = [("buy", "dry_ok", None), ("buy", "dry_ok", None), ("sell", "dry_built", None), ("buy", "dry_sim_failed", "Simulation: slippage tolerance exceeded"),
            ("buy", "rejected", "Grenze: max. 2 offene Positionen"), ("buy", "dry_ok", None), ("sell", "dry_built", None)]
    for i, (side, st, err) in enumerate(plan):
        oid = store.new_order(conn, dry=True, side=side, mint=r.choice(tok)["mint"], in_raw=100_000_000)
        conn.execute("UPDATE live_orders SET ts=?, status=?, error=?, price_impact=?, slippage_bps=?, done_ts=? WHERE id=?",
                     (now - 3600 * (len(plan) - i), st, err, round(r.uniform(.002, .02), 4), 300, now - 3600 * (len(plan) - i) + 3, oid))
    store.put(conn, "heartbeat", now - 40)
    store.put(conn, "wallet_pubkey", "DemoTradingWallet1111111111111111111111111")
    conn.commit()


def _learn_demo(conn, r, now):
    """Self-learning tables of copytrade (ct_evals, ct_param_versions) in their real format, if copytrade has them."""
    try:
        from copytrade import learn, sim
    except Exception:
        return
    learn.init(conn)

    def gains(variants, edge):
        out = []
        for v in variants:
            g = edge + 0.25 * min(v.take_profit, 0.6) - 0.15 * max(0, v.take_profit - 0.6) - 0.3 * abs(v.stop_loss - 0.2)
            g += (0.04 if v.trail_activate else 0) + (0.05 if getattr(v, "inflow_exit", False) else 0)
            g -= 0.0004 * abs(v.max_hold_min - 45) + 0.0002 * getattr(v, "delay_s", 15)
            out.append(round(max(-0.6, min(2.0, g + r.gauss(0, 0.25))), 4))
        return out
    for strategy, variants, n in (("whale", sim.GRID, 70), ("signal", sim.EXIT_GRID, 50)):
        for i in range(n):
            kind = "trade" if i % 3 == 0 else ("whale_buy" if strategy == "whale" else "signal")
            edge = r.gauss(-0.02, 0.12)
            feats = ({"tier": r.choice(["ultra", "smart", "good", "none"]), "mcap": r.choice(["<50k", "50-200k", "200k-1M", ">1M"]),
                      "liq": r.choice(["<10k", "10-50k", ">50k"]), "size": r.choice(["<2", "2-10", ">10"])}
                     if strategy == "whale" else
                     {"mcap": r.choice(["<50k", "50-200k", "200k-1M"]), "score": r.choice(["50+", "60+", "70+"]),
                      "buyers": r.choice(["2", "3", "4", "6+"]), "ultra": r.choice(["ja", "nein"])})
            if feats.get("tier") == "ultra" or feats.get("ultra") == "ja":
                edge += 0.08
            g = gains(variants, edge)
            conn.execute(_named(conn, "INSERT OR REPLACE INTO ct_evals VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"),
                         (kind, f"{strategy}-{i}", strategy, "", "", now - 86400 * 2 + i * 1800, sim.GRID_VERSION,
                          json.dumps(g), g[0] - 0.03 if kind == "trade" else None, g[0] if kind == "trade" else None,
                          json.dumps(feats), now))
        base = {"take_profit": 0.4, "stop_loss": 0.2, "trail_activate": 0.3, "trail_drawdown": 0.15, "max_hold_min": 60,
                "inflow_exit": 0}
        for k in range(4):
            accepted = k in (1, 3)
            params = {**base, "take_profit": [0.4, 0.6, 0.6, 0.6][k], "stop_loss": [0.2, 0.2, 0.2, 0.3][k],
                      "inflow_exit": 1 if k >= 1 and strategy == "whale" else 0, "block": {"tier": ["none"]} if k == 3 else {}}
            conn.execute("""INSERT INTO ct_param_versions(ts, strategy, scope, params, n_train, n_test, train_gain, test_gain,
                            baseline_test_gain, accepted, active, notes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                         (now - 86400 * 2 + k * 36000, strategy, "mode", json.dumps(params), 28 + 8 * k, 12 + 3 * k,
                          0.06 + 0.02 * k, 0.04 + 0.015 * k if accepted else 0.01, 0.02 + 0.01 * k, int(accepted),
                          int(k == 3), json.dumps({"calibration_gap": round(0.02 + 0.01 * k, 3)})))
    have = {c[1] for c in conn.execute("PRAGMA table_info(ct_positions)")}
    if "param_version" in have:
        vers = {st: [v[0] for v in conn.execute("SELECT id FROM ct_param_versions WHERE strategy=? AND accepted=1 ORDER BY id", (st,))]
                for st in ("whale", "signal")}
        for pid, st, opened in conn.execute("SELECT id, strategy, opened_at FROM ct_positions").fetchall():
            vs = vers.get(st) or []
            if vs and r.random() < 0.7:
                conn.execute("UPDATE ct_positions SET param_version=? WHERE id=?", (vs[0] if opened < now - 86400 else vs[-1], pid))
    conn.commit()

SAFETY_REASONS = [
    ("holders", 35, "Top-10 Halter besitzen 48 % des Angebots"),
    ("insiders", 40, "6 frische Wallets unter den Top-Haltern, 3 mit demselben Geldgeber"),
    ("bundles", 30, "18 % des Angebots im Startblock gekauft (Bundle)"),
    ("dev", 45, "Entwickler hat 70 % seiner Tokens schon verkauft"),
    ("liquidity", 25, "Liquidität nur 4 % der Market Cap"),
    ("lp", 30, "LP nicht gesperrt oder verbrannt"),
    ("socials", 10, "keine Website und keine Socials"),
    ("sell_sim", 20, "Rundreise-Verlust 9 % über dem erwarteten Slippage"),
]
SAFETY_HARD = [("authorities", 95, "Freeze-Authority aktiv: Tokens können eingefroren werden"),
               ("sell_sim", 95, "Kein Verkauf möglich (Honeypot)")]
SAFETY_LABELS = {"authorities": "Mint/Freeze-Rechte", "sell_sim": "Verkaufs-Test", "holders": "Halter-Verteilung",
                 "insiders": "Insider", "bundles": "Bundles beim Start", "dev": "Entwickler", "liquidity": "Liquidität",
                 "lp": "LP-Sperre", "wash": "Wash-Trading", "socials": "Socials", "rugcheck": "RugCheck"}


def _safety_demo(conn, r, tokens, now):
    """Rug-protection results (safety/ tables) for every coin, plus the copies signals/ and copytrade/ store."""
    try:
        from safety import store as sstore
    except Exception:
        return
    sstore.ensure_schema(conn)
    level = lambda sc: "critical" if sc >= 75 else "high" if sc >= 50 else "caution" if sc >= 25 else "low"
    info = {}
    for i, tok in enumerate(tokens):
        hard = i % 9 == 4
        picks = r.sample(SAFETY_REASONS, r.choice([0, 1, 1, 2, 2, 3]))
        reasons = [(c, rk * r.uniform(.5, 1.1), t) for c, rk, t in picks]
        if hard:
            reasons.insert(0, r.choice(SAFETY_HARD))
        prod = 1.0
        for _, rk, _ in reasons:
            prod *= 1 - rk / 100
        score = 100 * (1 - prod)
        if hard:
            score = max(score, 90)
        lv = level(score)
        reasons.sort(key=lambda x: -x[1])
        checks = {c: {"label": lab, "status": "ok" if r.random() > .08 else "unknown", "risk": 0.0, "hard": False, "data": {}}
                  for c, lab in SAFETY_LABELS.items()}
        for c, rk, _ in reasons:
            checks[c].update(risk=round(rk, 1), status="ok", hard=rk >= 90)
        pos = [x for x in ("Mint-Authority abgegeben", "LP verbrannt", "Verkauf möglich, Steuer 0 %", "Website und X vorhanden")
               if r.random() < .6 and not (hard and x.startswith("Verkauf"))]
        rs = [{"check": c, "risk": round(rk, 1), "text": t} for c, rk, t in reasons]
        conn.execute("INSERT OR REPLACE INTO safety_tokens(mint, ts, score, level, blocked, coverage, hard_flags_json, reasons_json, "
                     "positives_json, checks_json, creator, pair_created_at, version, calls) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (tok["mint"], now - r.randint(60, 1800), round(score, 1), lv, int(lv == "critical"), .91,
                      json.dumps([rs[0]["text"]] if hard else []), json.dumps(rs), json.dumps(pos), json.dumps(checks),
                      None, tok["born"], "safety-v1", r.randint(15, 60)))
        h, t = score, now - 3 * 86400
        while t < now:
            h = min(100, max(0, h + r.uniform(-6, 6)))
            conn.execute("INSERT OR REPLACE INTO safety_history VALUES (?,?,?,?)", (tok["mint"], t, round(h, 1), level(h)))
            t += r.randint(900, 7200)
        conn.execute("INSERT OR REPLACE INTO safety_history VALUES (?,?,?,?)", (tok["mint"], now, round(score, 1), lv))
        info[tok["mint"]] = (round(score, 1), lv, [x["text"] for x in rs[:3]])
    sstore.set_override(conn, tokens[1]["mint"], "block", "Dev-Wallet bekannt von früheren Rugs")
    crit = [m for m, v in info.items() if v[1] == "critical"]
    if crit:
        sstore.set_override(conn, crit[0], "allow", "geprüft, Freeze-Authority gehört dem Launchpad")
    cols = lambda tb: {c[1] for c in conn.execute(f"PRAGMA table_info({tb})")}
    if "safety_level" in cols("signals"):
        for sid, mint, score, cj in conn.execute("SELECT id, mint, score, contributions_json FROM signals").fetchall():
            sc, lv, why = info.get(mint, (None, "unchecked", []))
            f = 0.0 if lv == "critical" else 1.0 if sc is None or sc <= 25 else round(1 - .8 * min(1, (sc - 25) / 50), 3)
            contrib = json.loads(cj or "{}")
            if f < 1 and isinstance(contrib, dict):  # same shape as signals.engine.apply_safety
                contrib["safety"] = {"weight": None, "value": f, "points": round(score * f - score, 1),
                                     "why": f"Risiko {sc:.0f} ({lv}): " + "; ".join(why)}
            conn.execute("UPDATE signals SET score_raw=?, score=?, safety_factor=?, safety_risk=?, safety_level=?, safety_reasons_json=?, "
                         "contributions_json=? WHERE id=?", (score, round(score * f, 1), f, sc, lv, json.dumps(why), json.dumps(contrib), sid))
    if "safety_level" in cols("ct_positions"):
        for pid, mint in conn.execute("SELECT id, mint FROM ct_positions").fetchall():
            sc, lv, why = info.get(mint, (None, "unchecked", []))
            conn.execute("UPDATE ct_positions SET safety_level=?, safety_score=?, safety_reasons=? WHERE id=?", (lv, sc, json.dumps(why), pid))
    for aid, mint in conn.execute("SELECT id, mint FROM ct_alerts").fetchall():
        sc, lv, why = info.get(mint, (None, "unchecked", []))
        conn.execute("UPDATE ct_alerts SET data=? WHERE id=?",
                     (json.dumps({"safety": {"level": lv, "score": sc, "blocked": lv == "critical", "reasons": why}}), aid))
    for m in crit[1:3]:
        tok = next(t for t in tokens if t["mint"] == m)
        conn.execute(_named(conn, "INSERT INTO ct_events VALUES (?,?,?,?,?)"),
                     (now - r.randint(300, 7000), "info", "skip", f"[whale] {tok['symbol']}: Rug-Schutz blockiert ({info[m][2][0]})", None))
    # last learning run (safety/learn.py) with the walk-forward launch backtest (safety/backtest.summary)
    def op(caught, rugs, prec, dbl, dbl_n, thr):
        return {"fa_target": 0.1, "threshold": thr, "recall": round(caught / rugs, 3), "rugs_caught": caught, "rugs_total": rugs,
                "precision": prec, "doublers_flagged": dbl, "doublers_total": dbl_n}
    sstore.cache_put(conn, "learn_report", {
        "ts": now - 1800, "n_labelled": 412, "base_rug_rate": 0.31, "horizon_h": 24, "rug_drop_pct": 90,
        "levels": {"low": {"n": 220, "rugged": 31, "rug_rate": 0.141}, "caution": {"n": 104, "rugged": 38, "rug_rate": 0.365},
                   "high": {"n": 61, "rugged": 41, "rug_rate": 0.672}, "critical": {"n": 27, "rugged": 24, "rug_rate": 0.889}},
        "thresholds": {}, "launches": None,
        "launch_backtest": {"n": 1840, "rugs": 702, "doublers": 96, "test_n": 920, "scores": {
            "learned": {"auc": 0.81, "at_10pct_fa": op(208, 351, 0.86, 4, 48, 0.62)},
            "rules": {"auc": 0.72, "at_10pct_fa": op(141, 351, 0.79, 4, 48, 55)},
            "sniper": {"auc": 0.66, "at_10pct_fa": op(98, 351, 0.74, 5, 48, 40)}}}})
    conn.commit()


SNIPER_NAMES = ["FROGZ", "MOONPUP", "GIGACAT", "WOJAK2", "BONKER", "TRUMPY", "SOLDOGE", "PNUTX", "CHILLAX", "RIZZ",
                "MEOWW", "HAMSTR", "LUMPY", "ZOOMR", "GOOBER", "TOAD", "FWOG2", "BRRR", "PEPU", "SKIBI"]


def _sniper_demo(conn, r, now):
    """Launch-sniper tables (sniper/): stage-1 and deep-checked launches with outcomes, paper positions."""
    try:
        from sniper import store as sst
    except Exception:
        return
    sst.ensure_schema(conn)
    sst.LAUNCH_COLS = None
    start, size, bal = 5.0, 0.1, 5.0
    t, i = now - 30 * 3600, 0
    pos = []
    while t < now - 20:
        t += r.randint(8, 70)
        i += 1
        mint = "".join(r.choice("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz") for _ in range(40)) + "pump"
        src = r.choices(["pumpportal", "migration", "geckoterminal"], [0.9, 0.06, 0.04])[0]
        sym = r.choice(SNIPER_NAMES) + ("" if r.random() < .5 else str(r.randint(2, 99)))
        deep = r.random() < 0.12
        row = {"mint": mint, "source": src, "pool": "pump", "symbol": sym, "name": sym.title(), "creator": f"Dev{r.randint(1, 400)}",
               "created_ts": t - 20, "decided_ts": t, "stage": 2 if deep else 1, "n_buyers": r.randint(1, 40),
               "net_sol": round(r.uniform(-1, 12), 2), "dev_buy_pct": round(r.uniform(0, 18), 1), "dev_sold_pct": 0.0,
               "unseen_pct": round(r.uniform(0, 30), 1), "bundle_pct": round(r.uniform(0, 25), 1), "bundle_wallets": r.randint(0, 6),
               "top10_pct": round(r.uniform(15, 70), 1), "fast_buyers": r.randint(0, 5), "curve_progress": round(r.uniform(.02, .4), 3),
               "mcap_decide_sol": round(r.uniform(28, 120), 1), "sol_usd": 182.0, "calls_rpc": r.randint(0, 3) if deep else None,
               "calls_api": r.randint(3, 4) if deep else None}
        row["mcap_decide_usd"] = round(row["mcap_decide_sol"] * 182, 0)
        reasons, positives = [], []
        if row["bundle_pct"] >= 15:
            reasons.append({"check": "bundle", "risk": 70.0, "text": f"{row['bundle_pct']:.1f} % des Supplies im Erstellungs-Slot gekauft (Bundle)."})
        if row["dev_buy_pct"] >= 10:
            reasons.append({"check": "dev", "risk": 35.0, "text": f"Dev hält {row['dev_buy_pct']:.1f} %."})
        if row["top10_pct"] >= 50:
            reasons.append({"check": "holders", "risk": 30.0, "text": f"Top-10 halten {row['top10_pct']:.0f} %."})
        if row["bundle_pct"] < 5:
            positives.append(f"Kein Bundle: im Erstellungs-Slot nur {row['bundle_pct']:.1f} % gekauft.")
        if row["dev_buy_pct"] < 5:
            positives.append(f"Dev hält nur {row['dev_buy_pct']:.1f} %.")
        if row["n_buyers"] > 20:
            positives.append(f"{row['n_buyers']} organische Käufer in 20 s.")
        if not deep:
            dec, why = "skip", ("activity" if row["n_buyers"] < 6 else r.choice(["activity", "dev_sold", "budget"]))
        else:
            row["bundle_flag"] = int(row["bundle_pct"] >= 15)
            row["insider_flag"] = int(r.random() < .15)
            row["momentum"] = round(r.uniform(20, 95), 1)
            row["risk"] = round(min(95, sum(x["risk"] for x in reasons) * .8 + r.uniform(0, 20)), 1)
            row["score"] = round(row["momentum"] * (1 - row["risk"] / 100), 1)
            if row["bundle_flag"]:
                dec, why = "skip", "bundle"
            elif row["insider_flag"]:
                dec, why = "skip", "insider"
                reasons.append({"check": "funders", "risk": 60.0, "text": "3 erste Käufer mit gemeinsamer Geldquelle."})
            elif row["risk"] > 45:
                dec, why = "skip", "risk"
            elif row["score"] < 50:
                dec, why = "skip", "score"
            else:
                dec, why = "enter", None
        row.update(decision=dec, skip_reason=why, reasons_json=json.dumps(reasons, ensure_ascii=False),
                   positives_json=json.dumps(positives, ensure_ascii=False))
        # outcome relative to the decision market cap; bundles and insiders rug more often
        bad = row.get("bundle_flag") or row.get("insider_flag") or why == "dev_sold"
        good = dec == "enter"
        mx = r.choice([r.uniform(-.2, .5), r.uniform(.5, 3.5) if r.random() < (.35 if good else .12) else r.uniform(0, .6)])
        end = r.uniform(-.95, -.8) if r.random() < (.55 if bad else .25 if good else .4) else r.uniform(-.7, mx)
        if t + 3600 <= now:
            row.update(ret_1m=round(r.uniform(-.3, .4), 3), ret_5m=round(r.uniform(-.5, mx), 3), ret_15m=round(r.uniform(-.7, mx), 3),
                       ret_60m=round(end, 3), max_ret_60m=round(max(mx, end), 3), min_ret_60m=round(min(end, -.1), 3),
                       rugged=int(end <= -.8), done=1)
        if deep and r.random() < .12:  # enrichment errors as sniper/analyze.py keeps them (features.errors)
            row["features_json"] = json.dumps({"errors": r.sample([
                "429 Too Many Requests for url: https://frontend-api-v3.pump.fun/coins/x?api-key=demo",
                "rpc early: getSignaturesForAddress timed out", "funder 7xKXtg: getTransaction returned null",
                "pump.fun holders: 503 Service Unavailable"], r.randint(1, 2))})
        sst.save_launch(conn, row)
        if dec == "enter":
            pos.append((t, mint, sym, row, mx, end))
    for k, (t, mint, sym, row, mx, end) in enumerate(pos):
        p_in = row["mcap_decide_sol"] / 1e9
        is_open = t > now - 25 * 60 and k >= len(pos) - 3
        if is_open:
            last = p_in * (1 + r.uniform(-.3, .9))
            conn.execute("""INSERT INTO sniper_positions(mint, symbol, mode, opened_ts, entry_price_sol, entry_mcap_sol, entry_mcap_usd,
                            size_sol, tokens, remaining, fees_sol, fill_src, score, peak_price_sol, last_price_sol, status, params_json,
                            events_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                         (mint, sym, "paper", t + 2, p_in, row["mcap_decide_sol"], row["mcap_decide_usd"], size, size / p_in,
                          size / p_in, .004, "curve", row["score"], max(p_in, last), last, "open", "{}",
                          json.dumps([{"ts": t + 2, "kind": "buy", "sol": size}])))
            bal -= size
            continue
        if mx >= .6:
            reason, pct = r.choice(["trail", "trail", "tp1"]), r.uniform(.25, min(mx, 1.8))
        elif end <= -.5:
            reason, pct = r.choice(["stop", "insider_sell"]), r.uniform(-.38, -.22)
        else:
            reason, pct = "time", r.uniform(-.15, .1)
        pnl = round(size * pct - .006, 4)
        closed = t + r.randint(120, 3000)
        bal += pnl
        conn.execute("""INSERT INTO sniper_positions(mint, symbol, mode, opened_ts, entry_price_sol, entry_mcap_sol, entry_mcap_usd, size_sol,
                        tokens, remaining, fees_sol, fill_src, score, peak_price_sol, last_price_sol, tp1_done, realized_sol, status,
                        closed_ts, exit_reason, pnl_sol, pnl_pct, params_json, events_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                     (mint, sym, "paper", t + 2, p_in, row["mcap_decide_sol"], row["mcap_decide_usd"], size, size / p_in, 0, .006,
                      r.choice(["curve", "jupiter"]), row["score"], p_in * (1 + max(mx, 0)), p_in * (1 + pct), int(mx >= .6),
                      size + pnl, "closed", closed, reason, pnl, round(pnl / size, 4), "{}",
                      json.dumps([{"ts": t + 2, "kind": "buy", "sol": size}, {"ts": closed, "kind": "sell", "reason": reason, "sol": size + pnl}])))
    conn.execute("INSERT OR REPLACE INTO sniper_state VALUES ('paper_balance_sol', ?)", (str(round(bal, 4)),))
    # heartbeat as sniper/service.py writes it every 10 s (counters are totals since the service started)
    conn.execute("INSERT OR REPLACE INTO sniper_state VALUES ('service_status', ?)", (json.dumps({
        "ts": int(now), "connected": True, "ws_msgs": 412_877, "subscribed": 38, "in_memory": 640, "queue": 2, "open_positions": 3,
        "budget": {"rpc": {"used_last_min": 41, "per_min": 120, "total": 58_210}, "api": {"used_last_min": 22, "per_min": 30, "total": 21_904},
                   "deep": {"used_last_min": 3, "per_min": 4, "total": 3_310}},
        "launch_sources": {"pumpportal": {"enabled": True, "connected": True, "per_min": 11.8, "first_per_min": 7.4, "last_ts": int(now) - 3},
                           "rpc": {"enabled": True, "connected": True, "per_min": 11.2, "first_per_min": 4.2, "last_ts": int(now) - 5}},
        "active_launch_source": "pumpportal", "launches_per_min": 11.6, "trade_source": "rpc", "rpc_ws_connected": True, "rpc_ws_subs": 112, "rpc_ws_msgs": 88_410,
        "counters": {"creates": 27_480, "trades": 391_200, "migrations": 212, "stage1": 9_870, "stage2": 3_310, "entries": 604},
        "pump_api_ok": True}),))
    conn.commit()


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
    _inflight_demo(conn, r, tokens, now)
    _live_demo(conn, r, tokens, now)
    control.set_mode(conn, "signal", "semi")
    try:  # signals/ syncs the active model's tiers into wallets every rescore (core.scoring)
        from core.scoring import sync_model_tiers
        sync_model_tiers(conn)
        conn.commit()
    except Exception:
        pass
    _learn_demo(conn, r, now)
    _safety_demo(conn, r, tokens, now)
    _sniper_demo(conn, r, now)
    _edge_demo(conn, r)
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
