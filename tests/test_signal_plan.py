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
    c.execute("INSERT INTO token_prices(mint, ts, price_usd) VALUES ('B',?,0.45)", (now - 2 * 3600,))  # old crossing
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
    assert "Liquidität $20,000" in txt and "Ziel <b>+50 %</b> (0.003 $)" in txt and "Notstopp -50 % (0.001 $)" in txt
    assert "Trail: ab +20 % raus bei 20 % unter dem Hoch" in txt
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


def test_thin_pool_is_not_called():
    import dataclasses
    from core.config import settings
    cfg = dataclasses.replace(settings, signal_min_wallets=2, signal_min_net_usd=100, signal_min_score=10)
    m = dict(smart_buyers=2, entities=2, net_usd=500, score=50, liquidity_usd=1500)
    assert engine._reason(m, cfg) == "thin_liq"
    assert engine._reason(dict(m, liquidity_usd=None), cfg) is None      # unknown liquidity: not blocked
    assert engine._reason(dict(m, liquidity_usd=8000), cfg) is None
    assert engine._reason(dict(m, ev=0.1), cfg) == "thin_liq"            # also when the EV table decides


def test_smart_bought_coins_fetched_first():
    from signals.run import due_for_trades, refetch_interval
    now = 10_000
    quiet = dict(mint="q", last_trades_fetch=now - 2000, txns_m5=0, txns_h1=0, smart_n=0)
    hot = dict(mint="h", last_trades_fetch=now - 100, txns_m5=0, txns_h1=0, smart_n=2)
    assert refetch_interval(hot, 90) == 90
    assert [r["mint"] for r in due_for_trades([quiet, hot], now, 90)] == ["h", "q"]


def test_lag_and_hit_150_reported(tmp_path):
    c = conn(tmp_path)
    now = 300_000
    c.execute("INSERT INTO signals(ts, mint, score, price_usd, ret_1h, max_ret_1h, lag_first_s, lag_last_s) "
              "VALUES (?,?,?,?,?,?,?,?)", (now - 7200, "Z", 35, 1.0, 0.2, 1.8, 400, 95))
    c.execute("INSERT INTO signal_metrics(ts, mint, net_usd, smart_buyers, score, model_exp_net, contributions_json) "
              "VALUES (?,?,?,?,?,?,?)", (now - 60, "Z", 900, 3, 90, -0.2, '{"x": {"points": 1}}'))
    c.commit()
    a = {b["band"]: b for b in funnel.outcomes(c, 24, now=now)}["all"]
    assert a["hit_150"] == 1.0 and a["median_lag_last_s"] == 95 and a["median_lag_first_s"] == 400
    assert funnel.funnel(c, 4, now=now)["blocked_loss_veto"] == 1
    s = c.execute("SELECT * FROM signals").fetchone()
    assert "letzter Smart-Kauf vor 1 min 35 s" in notifier.format_signal(dict(s, wallets_json="[]", avg_quality=50,
                                                                                smart_buyers=2, net_usd=300))


def test_single_bad_print_is_no_high(tmp_path):
    from signals.backtest import confirmed, robust_max
    assert robust_max([1.0, 1.05, 3.0, 1.02]) == 1.05                   # one stray print
    assert robust_max([1.0, 1.2, 1.45, 1.7, 1.6]) == 1.7                 # a real run, print by print
    assert confirmed([1.0, 3.0, 1.0], [False, True, False])[1] == 3.0    # snapshots always stand
    c = conn(tmp_path)
    now = 100_000
    t = now - 2 * 3600
    c.execute("INSERT INTO signals(ts, mint, score, price_usd) VALUES (?,?,?,?)", (t, "B", 40, 1.0))
    for dt, p in ((90, 1.1), (300, 1.15), (310, 2.4), (320, 1.12), (1200, 1.3)):
        c.execute("INSERT INTO trades(sig, mint, wallet, side, ts, price_usd) VALUES (?,?,?,?,?,?)",
                  (f"s{dt}", "B", "W", "buy", t + dt, p))
    c.commit()
    engine.update_outcomes(c, now=now)
    s = c.execute("SELECT * FROM signals").fetchone()
    assert round(s["max_ret_1h"], 3) == round(1.3 / 1.1 - 1, 3)        # 2.4 ignored, entry after the delay at 1.1


def test_unmeasured_counted_and_plan_hit_rate_out_of_sample(tmp_path):
    c = conn(tmp_path)
    now = 2_000_000
    peaks = [0.1, 0.9, 0.2, 1.0, 0.3, 1.1, 0.6, 0.7, 0.05, 0.08, 0.04, 0.02]
    for i, mx in enumerate(peaks):
        c.execute("INSERT INTO signals(ts, mint, score, price_usd, ret_1h, max_ret_1h) VALUES (?,?,?,?,?,?)",
                  (now - 86400 + i * 600, f"M{i}", 40, 1.0, 0.0, mx))
    c.execute("INSERT INTO signals(ts, mint, score, price_usd) VALUES (?,?,?,?)", (now - 5 * 3600, "GAP", 40, 1.0))
    c.commit()
    lv = plan.levels(c, now)
    assert lv["n_unmeasured"] == 1 and lv["hit_rate_oos"] == 0.0        # the newest third all missed the old target
    a = {b["band"]: b for b in funnel.outcomes(c, 48, now=now)}["all"]
    assert a["n_unmeasured"] == 1 and a["hit_50_with_unmeasured"] == round(5 / 13, 3)


def test_backtest_counts_unmeasured_setups():
    from signals import backtest
    m = dict(entities=2, net=300, sellers=0, score=50, wallets=2)
    snaps = [(1000, "A", m, None), (1000, "B", m, None)]
    prices = {"A": [(1010, 1.0), (1500, 1.6), (4600, 1.2)]}
    st = {}
    res = backtest.replay(snaps, prices, dict(wallets=1, net=0, score=0, p=None, sellers=None), stats=st)
    assert [r["mint"] for r in res] == ["A"] and st["unmeasured"] == 1


def test_big_cluster_is_a_chain_not_one_owner():
    qc = object.__new__(engine.QualityCache)
    qc._sizes = None
    qc.model_tiers = {"A": ("smart", 1, "c1", None), "B": ("smart", 1, "c1", None),
                      **{f"W{i}": ("ultra", 1, "big", None) for i in range(6)}, "G": ("good", 1, "c1", None)}
    assert qc.entity("A") == qc.entity("B") == "cluster:c1"            # 2 tiered wallets: one owner
    assert qc.entity("W0") == "W0" and qc.entity("W1") == "W1"          # 6 in one cluster: a chain, counted apart
    assert qc.entity("X") == "X"


def test_single_bad_low_print_is_no_dip():
    from signals.backtest import confirmed
    assert confirmed([1.0, 1.1, 0.004, 1.15, 1.3]) == [1.0, 1.1, 1.1, 1.15, 1.3]   # BERT's -99.6 % print
    assert confirmed([1.0, 0.7, 0.5, 0.45]) == [1.0, 0.7, 0.5, 0.45]                # a real slide stays


def test_wash_trading_vetoes_a_call(monkeypatch):
    import dataclasses
    from types import SimpleNamespace
    from core.config import settings
    cfg = dataclasses.replace(settings, signal_min_wallets=2, signal_min_net_usd=100, signal_min_score=10)
    m = dict(smart_buyers=2, entities=2, net_usd=500, score=50, liquidity_usd=36000, contributions={})
    a = SimpleNamespace(override=None, reasons=[{"check": "wash", "risk": 34, "text": "27 % Hin-und-her"}])
    fake = SimpleNamespace(signal_penalty=lambda c, x: {"factor": 0.86, "risk": 34, "checked": True, "reasons": []},
                           get=lambda c, x: a)
    monkeypatch.setattr(engine, "_safety", fake)
    out = engine.apply_safety(None, "K", m)
    assert out["safety"]["red_flags"] and engine._reason(out, cfg) == "safety_drop"
    assert engine._reason(dict(out, ev=0.2), cfg) == "safety_drop"          # also when the EV table decides
    a.override = "allow"                                                     # manual release wins
    assert engine._reason(engine.apply_safety(None, "K", m), cfg) is None


def test_token_refresh_keeps_watch_flags(tmp_path, monkeypatch):
    from core.models import Token
    from signals import run
    c = conn(tmp_path)
    c.execute("INSERT INTO tokens(mint, tracked, last_updated) VALUES ('W', 1, 0)")
    c.execute("INSERT INTO tokens(mint, tracked, last_updated) VALUES ('S', 0, 0)")
    c.execute("INSERT INTO tokens(mint, tracked, last_updated) VALUES ('F', 0, ?)", (int(__import__('time').time()) + 3120,))
    c.execute("INSERT INTO signals(ts, mint, score, price_usd) VALUES (?, 'S', 40, 1.0)", (int(__import__('time').time()) - 600,))
    c.commit()
    asked = []

    def info(mints):
        asked.extend(mints)
        return [Token(mint=m, symbol=m, price_usd=2.0, price_change_m5=7.0, txns_m5=40) for m in mints]
    monkeypatch.setattr(run.market, "token_info", info)
    assert run.refresh_tokens(c) == 2 and sorted(asked) == ["S", "W"]
    r = {x["mint"]: x for x in c.execute("SELECT * FROM tokens")}
    assert r["S"]["tracked"] == 0 and r["W"]["tracked"] == 1 and r["W"]["txns_m5"] == 40 and r["S"]["last_updated"] > 0
    assert r["F"]["last_updated"] <= int(__import__('time').time())    # stamped 52 min ahead: pulled back


def test_staged_pump_exit_alert_once(tmp_path, monkeypatch):
    c = conn(tmp_path)
    now = 60_000
    c.execute("INSERT INTO signals(ts, mint, symbol, score, price_usd) VALUES (?,?,?,?,?)", (now - 900, "P", "PMP", 40, 1.0))
    c.execute("INSERT INTO tokens(mint, price_usd) VALUES ('P', 1.8)")
    c.commit()
    monkeypatch.setattr(plan, "_staged", lambda conn, mint: mint == "P")
    ev = plan.check_staged(c, now)
    assert [(e["kind"], round(e["ret"], 2), e["minutes"]) for e in ev] == [("staged", 0.8, 15)]
    assert "inszenierter Pump" in notifier.format_followup(ev[0])
    assert plan.check_staged(c, now + 60) == []


def test_trail_exit_after_plus_20_and_no_tight_stop():
    pts = [(10, 0.85), (20, 1.3), (30, 1.1), (40, 1.0)]       # -15 % dip is no exit, +30 % high, then -20 % off it
    assert plan.first_cross(pts, 1.0, 0.5, plan.EMERGENCY_STOP)[:2] == ("trail", 40)
    assert plan.first_cross([(10, 0.55), (20, 0.49)], 1.0, 0.5, plan.EMERGENCY_STOP)[:2] == ("stop", 20)
    assert plan.first_cross([(10, 1.1), (20, 0.9)], 1.0, 0.5, plan.EMERGENCY_STOP) is None  # +10 % high: no trail yet
