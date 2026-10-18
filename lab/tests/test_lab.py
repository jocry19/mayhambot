import json
import os
import sqlite3
import time

import numpy as np
import pandas as pd

from lab import costs, hypotheses, pruefstand


def _db():
    c = sqlite3.connect(":memory:")
    c.executescript("""
    CREATE TABLE token_prices (mint TEXT, ts INTEGER, price_usd REAL, mcap_usd REAL, liquidity_usd REAL);
    CREATE TABLE tokens (mint TEXT PRIMARY KEY, dex TEXT, pair_created_at INTEGER);
    CREATE TABLE sniper_launches (mint TEXT, migrated INTEGER, migrated_ts INTEGER, mcap_decide_sol REAL, sol_usd REAL);
    CREATE TABLE sniper_paths (mint TEXT, created_ts INTEGER, decided_ts INTEGER, points_json TEXT);
    CREATE TABLE opportunities (source TEXT, mint TEXT, ts INTEGER, liq_usd REAL, mcap_usd REAL, features TEXT,
        label_at INTEGER, label_rug INTEGER, label_rules TEXT);
    """)
    return c


def test_costs_by_venue_liquidity_and_stake():
    curve = costs.roundtrip("pumpfun", None, 0.1, "launch")
    deep = costs.roundtrip("pumpswap", 5e5, 0.1, "market")
    thin = costs.roundtrip("pumpswap", 5e3, 0.1, "market")
    assert deep < thin < curve
    assert 0.01 < deep < 0.03 and 0.05 < curve < 0.12
    assert costs.roundtrip("pumpswap", 5e5, 0.03) > costs.roundtrip("pumpswap", 5e5, 0.3)  # fixed tx cost share


def test_exit_counts_rug_and_unknown_path():
    ts = np.array([0, 600, 1200, 1800, 2400, 3000, 3600, 4200])
    p = np.array([1.0, 1.2, 0.05, 0.05, 0.05, 0.05, 0.05, 0.05])
    assert hypotheses._exit(ts, p, 0, 3600) == -1.0
    p2 = np.array([1.0, 1.1, 1.4, 1.2, 1.0, 0.9, 0.8, 0.8])
    assert abs(hypotheses._exit(ts, p2, 0, 3600) - (-0.2)) < 1e-9
    assert abs(hypotheses._exit(ts, p2, 0, 3600, tp=0.3) - 0.4) < 1e-9  # sold at the first snapshot over +30 %
    assert hypotheses._exit(ts[:3], p2[:3], 0, 3600) is None  # the path ends early: not counted


def test_panel_point_in_time_and_pump_reversal():
    c = _db()
    t0 = 1_800_000_000
    c.execute("INSERT INTO tokens VALUES ('A', 'pumpswap', ?)", (t0 - 86400,))
    rows = []
    for k in range(0, 5 * 3600, 60):  # flat, then +100 % in the third hour, then back down
        px = 1.0 if k < 7200 else (2.0 if k < 10800 else 1.0)
        px *= 1 + 0.001 * ((k // 60) % 3)  # never stale
        rows.append(("A", t0 + k, px, px * 1e6, 1e5))
    c.executemany("INSERT INTO token_prices VALUES (?,?,?,?,?)", rows)
    P = hypotheses.panel(c, 0)
    assert len(P) > 0 and P.t.is_monotonic_increasing
    hot = P[P.r60 > 0.5]
    assert len(hot) and (hot.t >= t0 + 7200).all()
    E = hypotheses.panel_entries(P)
    pump = E[E.hyp == "nach_pump_60m"]
    assert len(pump) and (pump.gross < 0).all()


def test_snipe_target_stop_and_rug():
    pts = [[0, 30, 30, 30], [20, 40, 40, 40], [60, 45, 39, 130], [300, 20, 20, 50]]
    assert hypotheses._snipe(pts, 20, 3, 0.5) == 2  # x3 reached before the stop
    assert abs(hypotheses._snipe(pts, 20, 10, 0.3) - ((0.7 * 0.97) - 1)) < 1e-9
    rug = [[0, 30, 30, 30], [20, 40, 40, 40], [300, 2, 2, 2]]
    assert hypotheses._snipe(rug, 20, 1e9, 0.99) == -1.0


def test_evaluate_needs_all_thirds_and_enough_coins():
    rng = np.random.default_rng(1)
    n = 600
    good = pd.DataFrame({"hyp": "gut", "mint": [f"m{i}" for i in range(n)], "t": np.arange(n), "dex": "raydium",
                         "liq": 1e6, "kind": "market", "gross": 0.12 + rng.normal(0, 0.1, n)})
    late = good.assign(hyp="spaet", gross=np.where(np.arange(n) < 200, -0.3, 0.2))
    res = {r["hyp"]: r for r in pruefstand.evaluate(pruefstand.net_of_costs(pd.concat([good, late])))}
    assert res["gut"]["verdict"] == "traegt" and res["gut"]["brain_kind"] == "market"
    assert pruefstand.brain_kind("label_sniper_hold60") == "pool" and pruefstand.brain_kind("launch_mc30-50_halten") == "launch"
    assert res["spaet"]["verdict"] == "kandidat" and res["spaet"]["thirds"][0] < 0


def test_run_once_writes_report_even_when_a_family_fails(tmp_path):
    db = tmp_path / "x.db"
    c = sqlite3.connect(db)
    c.executescript("CREATE TABLE token_prices (mint TEXT, ts INTEGER, price_usd REAL, mcap_usd REAL, "
                    "liquidity_usd REAL); CREATE TABLE tokens (mint TEXT PRIMARY KEY, dex TEXT, pair_created_at INTEGER);")
    c.close()
    res = pruefstand.run_once(str(db), tmp_path / "lab", now=1_800_000_000)
    rep = json.loads((tmp_path / "lab" / "report.json").read_text())
    assert res == [] and rep["results"] == [] and any(k.startswith("error_") for k in rep["info"])
    assert (tmp_path / "lab" / "report.md").read_text().startswith("# Prüfstand")


def test_run_waits_for_the_last_report_and_a_running_retrain(tmp_path):
    from lab.__main__ import next_round
    out, db = tmp_path / "lab", tmp_path / "mayham.db"
    out.mkdir()
    assert next_round(out, db, 6 * 3600, now=1_000_000) == 0  # no report yet: run now
    (out / "report.json").write_text(json.dumps({"at": 1_000_000 - 3600}))
    assert next_round(out, db, 6 * 3600, now=1_000_000) == 5 * 3600  # a restart does not start a fresh round
    assert next_round(out, db, 6 * 3600, now=1_000_000 + 6 * 3600) == 0
    now = time.time()
    (tmp_path / "TRAINING").write_text("")
    os.utime(tmp_path / "TRAINING", (now, now))
    assert next_round(out, db, 6 * 3600, now=now) == 600  # report due, but a retrain runs: wait
    assert next_round(out, db, 6 * 3600, now=now + 2 * 3600) == 0  # marker older than 90 min: stale
