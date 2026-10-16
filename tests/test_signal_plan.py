import json

from core import db
from signals import engine, funnel, notifier, plan


def conn(tmp_path):
    return db.connect(str(tmp_path / "p.db"))


def test_peak_after_call_measured(tmp_path):
    c = conn(tmp_path)
    now = 100_000
    t = now - 5 * 3600
    c.execute("INSERT INTO signals(ts, mint, score, price_usd) VALUES (?,?,?,?)", (t, "A", 40, 1.0))
    c.execute("INSERT INTO tokens(mint, liquidity_usd) VALUES ('A', 50000)")
    for dt, p in ((120, 0.8), (600, 1.4), (1800, 2.0), (3000, 1.2), (3 * 3600, 2.5)):
        c.execute("INSERT INTO token_prices(mint, ts, price_usd) VALUES ('A',?,?)", (t + dt, p))
    c.commit()
    engine.update_outcomes(c, now=now)
    s = c.execute("SELECT * FROM signals").fetchone()
    assert round(s["max_ret_15m"], 3) == 0.4 and round(s["max_ret_1h"], 3) == 1.0 and round(s["max_ret_4h"], 3) == 1.5
    assert s["peak_min_1h"] == 30.0 and round(s["dd_before_peak_1h"], 3) == -0.2   # dipped 20 % before the 1 h high


def test_levels_default_then_history(tmp_path):
    c = conn(tmp_path)
    now = 1_000_000
    assert plan.levels(c, now)["target"] == plan.DEFAULT_TARGET and plan.levels(c, now)["source"] == "default"
    peaks = [0.1, 0.2, 0.3, 0.6, 0.8, 1.0, 1.2, 1.5, 2.0, 3.0]
    for i, mx in enumerate(peaks):
        c.execute("INSERT INTO signals(ts, mint, score, price_usd, max_ret_1h, dd_before_peak_1h, peak_min_1h) "
                  "VALUES (?,?,?,?,?,?,?)", (now - 3600 * (i + 2), f"M{i}", 40, 1.0, mx, -0.05 * (i % 4), 10 + i))
    c.commit()
    lv = plan.levels(c, now)
    assert lv["source"] == "history" and lv["target"] == 0.8           # the peak 60 % of calls reached
    assert lv["hit_rate"] == 0.6 and -0.5 <= lv["stop"] <= -0.1 and lv["peak_min"] is not None


def test_first_cross_in_time_order():
    pts = [(1, 1.1), (2, 0.65), (3, 1.6)]
    assert plan.first_cross(pts, 1.0, 0.5, -0.3)[0] == "stop"         # stop came first
    assert plan.first_cross(pts, 1.0, 0.5, -0.4)[:2] == ("target", 3)
    assert plan.first_cross(pts[:1], 1.0, 0.5, -0.3) is None


def test_check_levels_alerts_once_and_only_fresh(tmp_path):
    c = conn(tmp_path)
    now = 50_000
    c.execute("INSERT INTO signals(ts, mint, symbol, score, price_usd, target_ret, stop_ret, call_no) "
              "VALUES (?,?,?,?,?,?,?,?)", (now - 1200, "A", "AAA", 40, 1.0, 0.5, -0.3, 2))
    c.execute("INSERT INTO signals(ts, mint, symbol, score, price_usd, target_ret, stop_ret) VALUES (?,?,?,?,?,?,?)",
              (now - 3 * 3600, "B", "BBB", 40, 1.0, 0.5, -0.3))
    c.execute("INSERT INTO token_prices(mint, ts, price_usd) VALUES ('A',?,1.6)", (now - 300,))
    c.execute("INSERT INTO token_prices(mint, ts, price_usd) VALUES ('B',?,0.6)", (now - 2 * 3600,))  # old crossing
    c.commit()
    ev = plan.check_levels(c, now)
    assert [(e["symbol"], e["kind"], e["minutes"]) for e in ev] == [("AAA", "target", 15)]
    assert c.execute("SELECT stop_hit_ts FROM signals WHERE mint='B'").fetchone()[0] == now - 2 * 3600  # stored silently
    assert plan.check_levels(c, now) == []                              # never twice
    txt = notifier.format_followup(ev[0])
    assert "Ziel +60 % erreicht" in txt and "Call Nr. 2" in txt


def test_fresh_buyers_for_recall():
    last = {"wallets_json": json.dumps([{"wallet": "W1"}, {"wallet": "W2"}])}
    m = {"wallets": [{"wallet": "W1", "tier": "smart"}, {"wallet": "W3", "tier": "ultra"}, {"wallet": "W4", "tier": "good"}]}
    assert engine.fresh_buyers(m, last) == 1
    assert engine.fresh_buyers(m, None) == 0


def test_alert_has_plan_and_liquidity(tmp_path):
    c = conn(tmp_path)
    c.execute("INSERT INTO signals(ts, mint, symbol, score, price_usd, mcap_usd, net_usd, smart_buyers, avg_quality, "
              "wallets_json, liquidity_usd, target_ret, stop_ret, call_no) VALUES (1,'MINT','XY',40,0.002,1e5,300,2,70,"
              "'[]',20000,0.5,-0.3,1)")
    s = c.execute("SELECT * FROM signals").fetchone()
    txt = notifier.format_signal(s)
    assert "Liquidität $20,000" in txt and "Ziel <b>+50 %</b> (0.003 $)" in txt and "Stopp -30 % (0.0014 $)" in txt
    assert "Call Nr." not in txt


def test_outcomes_show_peak_stats(tmp_path):
    c = conn(tmp_path)
    now = 200_000
    for i, (mx, r1) in enumerate(((1.2, -0.2), (0.3, 0.1))):
        c.execute("INSERT INTO signals(ts, mint, score, price_usd, ret_1h, max_ret_1h, peak_min_1h, dd_before_peak_1h, "
                  "target_hit_ts) VALUES (?,?,?,?,?,?,?,?,?)", (now - 7200 - i, f"M{i}", 35, 1.0, r1, mx, 12, -0.1,
                                                                 now - 7000 if mx > 0.5 else None))
    c.commit()
    out = {b["band"]: b for b in funnel.outcomes(c, 24, now=now)}
    a = out["all"]
    assert a["n"] == 2 and a["hit_25"] == 1.0 and a["hit_100"] == 0.5 and a["median_peak_min_1h"] == 12
    assert a["target_hits"] == 1 and out["30-40"]["n"] == 2
