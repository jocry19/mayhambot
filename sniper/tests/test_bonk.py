"""Bonk (Raydium LaunchLab) trades: event decoding on a real transaction, the invoke-stack filter, pool PDA,
traders attached afterwards, the service mapping pool -> launch, per-launch fees in paper fills and research."""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pytest  # noqa: E402

from sniper import research, rpcws  # noqa: E402
from sniper.state import LaunchState  # noqa: E402
from sniper.tests.test_sniper import cfg  # noqa: E402

T0 = 1_760_000_000.0
# BuyExactIn of 0.2 SOL, mainnet 10/2026 (signature 4KhRga1J..., pool EWqkud...), copied from the chain
BUY = ("vdt/007mYe7IzOc7biPkukt4T1w1AXw7tA8inyng650DEIRNjCm2bwAgDwwSBQMASYJbfLqqAwC2xYkyAQAAAHRZg61iUwIAoBSQEwIAAADC"
       "hA9p7VcCAMBczB4CAAAAAMLrCwAAAABOK4y7igQAACChBwAAAAAAgJaYAAAAAABAQg8AAAAAAAAAAAAAAAAAAAAB")
POOL = "EWqkudpR6VTbipbaTGojUCLnpuZk23aAKfpCw7avRfuY"
MINT = "HusbHkKToLsCBDZEQKmQrxs2NB1UjBfvWQWiBUpKNGSR"


def note(logs, sig="SIGB"):
    return {"method": "logsNotification", "params": {"result": {"context": {"slot": 9}, "value": {
        "signature": sig, "err": None, "logs": logs}}}}


def test_decode_real_buy():
    ev = rpcws.decode_launchlab(BUY)
    assert ev["pool"] == POOL and ev["is_buy"] and ev["status"] == 0
    assert ev["sol"] == pytest.approx(0.2) and ev["tokens"] == pytest.approx(4_993_898.52347)
    assert ev["v_sol"] / ev["v_tok"] * 1e9 == pytest.approx(38.25, abs=0.01)   # market cap in SOL after the buy
    assert ev["fee_pct"] == pytest.approx(5.75)
    msgs = rpcws.parse_launchlab(note(["Program data: " + BUY]))
    assert len(msgs) == 1 and msgs[0]["mint"] == POOL and msgs[0]["txType"] == "buy" and msgs[0]["pool"] == "bonk"
    # the constant product on the effective reserves gives the tokens of the event for the SOL after fees
    from sniper.state import curve_buy
    before_sol, before_tok = ev["v_sol"] - 0.2 * (1 - 0.0575), ev["v_tok"] + ev["tokens"]
    assert curve_buy(before_sol, before_tok, 0.2, 5.75) == pytest.approx(ev["tokens"], rel=1e-6)


def test_events_split_by_program():
    """Same discriminator in both launchpads: an aggregator tx touching both must not feed the other's decoder."""
    logs = [f"Program {rpcws.LAUNCHLAB_PROGRAM} invoke [1]", "Program log: Instruction: BuyExactIn",
            "Program TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA invoke [2]",
            "Program TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA success", "Program data: " + BUY,
            f"Program {rpcws.LAUNCHLAB_PROGRAM} success", f"Program {rpcws.PUMP_PROGRAM} invoke [1]",
            "Program log: something failed: no", f"Program {rpcws.PUMP_PROGRAM} success"]
    n0 = rpcws.REJECTS["trade"]
    assert rpcws.parse_notification(note(logs)) == [] and rpcws.REJECTS["trade"] == n0
    assert rpcws.program_data(logs, rpcws.LAUNCHLAB_PROGRAM) == [BUY]
    assert len(rpcws.parse_launchlab(note(logs))) == 1


def test_pool_address():
    pytest.importorskip("solders")
    assert rpcws.bonk_pool(MINT) == POOL


def test_attach_wallet():
    st = LaunchState(mint=MINT, created_ts=T0, pool="bonk")
    m = rpcws.parse_launchlab(note(["Program data: " + BUY]))[0]
    m["mint"] = MINT
    st.apply_trade(m, T0 + 3)
    assert not st.bought and st.n_seen == 1 and st.fee_pct == pytest.approx(5.75)
    assert st.attach_wallet("SIGB", "Wallet1") and not st.attach_wallet("SIGB", "Wallet2")
    assert st.bought["Wallet1"] == pytest.approx(4_993_898.52347) and st.first_buy_ts["Wallet1"] == T0 + 3


def test_service_maps_pool_to_launch(tmp_path):
    from sniper.service import Service
    svc = Service(cfg(), db_path=str(tmp_path / "b.db"))
    svc.engine.handle({"txType": "create", "mint": MINT, "pool": "bonk", "traderPublicKey": "DEV", "signature": "C"},
                      T0)
    svc.bonk_pools[POOL], svc.bonk_mints[MINT] = MINT, POOL
    m = rpcws.parse_launchlab(note(["Program data: " + BUY]))[0]
    svc._on_bonk_trade(dict(m, _rx=T0 + 2))
    st = svc.engine.launches[MINT]
    assert st.n_seen == 1 and st.mcap_sol == pytest.approx(38.25, abs=0.01) and st.fee_pct == pytest.approx(5.75)
    assert list(svc.bonk_wallet_q) == [(MINT, "SIGB", T0 + 2, 0)]
    svc._on_bonk_trade(dict(m, mint="UNKNOWNPOOL"))          # a pool of no launch of ours
    assert st.n_seen == 1
    svc._on_bonk_trade(dict(m, signature="SIG2", poolStatus=1))   # the curve is sold out: migration
    assert st.migrated and svc.bonk_stats["migrations"] == 1
    svc.engine.attach_wallet(MINT, "SIGB", "W1")
    assert "W1" in st.bought


def test_paper_and_research_use_the_launch_fee():
    from sniper.paper import PaperBook
    c = cfg(adverse_slippage=0.0)
    st = LaunchState(mint=MINT, created_ts=T0, pool="bonk", v_sol=20.0, v_tok=500e6, mcap_sol=40.0)
    book = PaperBook.__new__(PaperBook)
    book.cfg = c
    cheap = book.fill_buy(st, 1.0)[0]
    st.fee_pct = 5.75
    assert book.fill_buy(st, 1.0)[0] < cheap * 0.96
    side, tx = research.costs(c)
    row = {"mint": "X", "created_ts": T0, "decided_ts": T0 + 20, "mcap_decide_sol": 40.0, "ret_60m": 0.0}
    path = {"X": [[0, 40.0, 40.0, 40.0], [20, 40.0, 40.0, 40.0], [3600, 40.0, 40.0, 40.0]]}
    rule = (100, 0.5, 30, 40, 30, 60)
    flat = research.trade_returns([row], path, rule, side, tx)[0]
    dear = research.trade_returns([dict(row, fee_pct=5.75)], path, rule, side, tx)[0]
    assert dear < flat - 0.08


def test_rejected_pump_events_are_counted_by_mint_and_instruction():
    import base64
    import struct
    raw = rpcws.TRADE_DISC + bytes(range(1, 33)) + struct.pack("<QQ", 0, 2_000_000_000_000) + b"\1" + bytes(32) \
        + struct.pack("<qQQQQ", 1, 0, 886_000_000_000_000, 0, 0) + bytes(200) + struct.pack("<I", 3) + b"buy"
    n = rpcws.REJECT_IX.get("buy", 0)
    assert rpcws.decode_trade(base64.b64encode(raw).decode()) is None
    assert rpcws.REJECT_IX["buy"] == n + 1 and rpcws.REJECTED[-1][1] == rpcws.b58(bytes(range(1, 33)))


# three rejected pump.fun TradeEvents from the night of 17/18.10.2026 (public chain data): 30 virtual SOL is not
# every curve's start any more, the token side is the standard one
LOW = ["vdt/007mYe70Ud4h6UiUm418+ZarBg3tTVNqZOXacu/T+xt9/NxY3+lSRQAAAAAAWQybV8A3AAABCduhPqUZDZWUd0Hj8cUSmLKWuCnNHZ4AcqkbPQjKkZfV+NNqAAAAAOpexgIAAAAACEcBOIoDAgAgQsABAAAAAAiv7uv4BAEAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABfAAAAAAAAAJmoAAAAAAAA3zALMhmqEPMNAnhiPJKMGtxjwOdrhB9bglwKP4yd+y8eAAAAAAAAAD41AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAwAAAGJ1eQEAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAOlSRQAAAAAA6l7GAgAAAAAgQsABAAAAAAAAAAAAAAAAAAAAAAAAAACC0TAAAAAAAA==",  # noqa: E501
       "vdt/007mYe6caVOXx/ba5dzpenutGPrGcN5R2WcK9giVR6X9R/W6D6IrYgAAAAAAJK6jtw4BAAAAootf0mq0eaapzGy/awsj62GIWjceASCsqRO+7z0TinjV+NNqAAAAAEslnygBAAAAP6uQmNmOAwDe8FYDAAAAAD8TfkxIkAIA6JMUH7GOnxV02BDheOGeMGBOMXWqLkoy38hgByfRBwkAAAAAAAAAAAAAAAAAAAAA1D2YZ7NESE++EWuay0dUa1+HC1mbtb+SXmTuOt3T2XMAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABAAAAHNlbGwBAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACiK2IAAAAAAEslnygBAAAA3vBWAwAAAAAAAAAAAAAAAAAAAAAAAAAAnlEFAAAAAAA=",  # noqa: E501
       "vdt/007mYe4jdIqoMPM7KIX1SFyuc9LcppgXTEFAhLPPC0prNlEXb1Y+oQEAAAAAiYYtoysBAAAASP2yUsfuvZN2fL8ZkhjZz60UGENi2qC6U3W9hgSYVnzV+NNqAAAAAIixWkIFAAAAwZjDDwnIAwAqSvsMAAAAAMEAscN3yQIA6JMUH7GOnxV02BDheOGeMGBOMXWqLkoy38hgByfRBwlfAAAAAAAAAL32AwAAAAAAnjr50NQBgRM+974cj22yYOF1RiclgtRTHtUWAve3ogEeAAAAAAAAAHJAAQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABAAAAHNlbGwBAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABWPqEBAAAAAIixWkIFAAAAKkr7DAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="]  # noqa: E501


def test_low_start_pump_curves_are_decoded():
    from sniper.state import base_floor
    evs = [rpcws.decode_trade(e) for e in LOW]
    assert all(evs)
    a, b, c = evs
    assert a["is_buy"] and not b["is_buy"] and not c["is_buy"]
    assert rpcws.ix_name(__import__("base64").b64decode(LOW[0])) == "buy"
    for ev in evs:   # the trade price is the reserves' price: a real quote, not a misread offset
        price = ev["sol_lamports"] / 1e9 / (ev["token_raw"] / 1e6)
        assert price == pytest.approx(ev["v_sol_lamports"] / 1e9 / (ev["v_tok_raw"] / 1e6), rel=0.12)
    m = rpcws.to_stream_msg(c, "S3")
    assert m["marketCapSol"] == pytest.approx(21.22, abs=0.01) and m["rSolInBondingCurve"] == pytest.approx(0.2178, abs=1e-4)
    st = LaunchState(mint=m["mint"], created_ts=T0)
    st.apply_trade(m, T0 + 5)
    assert st.start_mcap_sol == pytest.approx(20.85, abs=0.01)     # (22.588 - 0.218) / 1.073e9 * 1e9
    assert st.base_floor() == pytest.approx(0.99 * 20.85, abs=0.01) and st.mcap_sol > st.base_floor()
    assert base_floor(None) == 27.9 and base_floor(0.01) == 2.0   # dust curves stay untradable
    # garbage stays out: a token side that is not pump.fun's
    assert not rpcws.curve_ok(5.0, 900e6, 0.1, 100e6) and not rpcws.curve_ok(0.0, 900e6, 0.0, 620.1e6)


def test_bonk_stream_never_shares_the_pump_rpc(tmp_path):
    from sniper.service import Service
    assert Service(cfg(bonk_ws_url=""), db_path=str(tmp_path / "a.db")).logs_bonk is None
    c = cfg()
    c.bonk_ws_url = c.ws_url2 or c.ws_url
    assert Service(c, db_path=str(tmp_path / "b.db")).logs_bonk is None


def test_coverage_counts_decisions_and_migrations(tmp_path):
    from sniper import store
    from sniper.engine import Engine
    eng = Engine(cfg(), store.connect(str(tmp_path / "c.db")), enricher=None)
    eng.cover["decide"].extend([(T0, True), (T0, False), (T0, False), (T0 - 7200, True)])
    eng.cover["migrate"].append((T0, True))
    c = eng.coverage(T0 + 10)
    assert c["decide"] == {"n": 3, "share": 0.333} and c["migrate"] == {"n": 1, "share": 1.0}


def test_db_coverage(tmp_path):
    from sniper import analytics, store
    conn = store.connect(str(tmp_path / "d.db"))
    rows = [("A", "pump", 5, 1), ("B", "pump", 0, 0), ("C", "bonk", 0, 0), ("D", "pump", 3, 0)]
    for mint, pool, n, mig in rows:
        store.save_launch(conn, {"mint": mint, "pool": pool, "source": "pumpportal", "decided_ts": int(T0),
                                 "n_trades": n, "migrated": mig, "migrated_ts": int(T0 + 600) if mig else None})
    store.save_launch(conn, {"mint": "G", "pool": "raydium", "source": "geckoterminal", "decided_ts": int(T0)})
    c = analytics.coverage(conn, hours=1, now=T0 + 900)
    assert c["launches"] == 4 and c["launch_rate"] == 0.5 and c["migrations"] == 1 and c["migration_rate"] == 1.0
    assert c["by_pool"]["pump"]["launch_rate"] == 0.667 and c["by_pool"]["bonk"]["launches"] == 1
    assert sum(h["launches"] for h in c["hourly"]) == 4 and c["target"] == 0.9


def test_opportunities_use_the_decision_snapshot_and_net_costs(tmp_path):
    import json
    from sniper import opportunity, store
    conn = store.connect(str(tmp_path / "o.db"))
    store.save_launch(conn, {"mint": "O1", "source": "pumpportal", "pool": "pump", "created_ts": int(T0),
                             "decided_ts": int(T0 + 20), "decision": "skip", "skip_reason": "activity",
                             "n_buyers": 40, "stage1_json": json.dumps({"n_buyers": 6, "mcap_decide_sol": 55.0}),
                             "ret_60m": 1.0, "rugged": 0})
    store.save_launch(conn, {"mint": "O2", "source": "pumpportal", "created_ts": int(T0), "decided_ts": int(T0 + 20),
                             "ret_60m": 0.5, "rugged": 1})
    a, b = opportunity.recent(conn, cfg(), since=T0)
    assert a["features"]["n_buyers"] == 6 and a["features"]["mcap_decide_sol"] == 55.0 and a["decided_after_sec"] == 20
    assert 0.8 < a["outcome_net"] < 1.0 and not a["seen_trades"] and a["reason"] == "activity"
    assert b["outcome_net"] == -1.0


def test_decisions_and_results_reach_the_shared_opportunity_table(tmp_path):
    import json
    from sniper import opportunity, store
    from sniper.engine import Engine
    from sniper.tests.test_sniper import Curve, FakeEnricher
    conn = store.connect(str(tmp_path / "g.db"))
    c = cfg()
    eng = Engine(c, conn, enricher=FakeEnricher())
    cv = Curve("GELEG")
    eng.handle(cv.create(), T0)
    eng.handle(cv.buy("W1", 0.2), T0 + 2)
    eng.tick(T0 + c.decide_sec + 1)                 # one buyer: skipped as too little activity
    r = conn.execute("SELECT source_action, source_reason, features FROM opportunities WHERE ref='GELEG'").fetchone()
    assert r and r[0] == "passed" and r[1] == "activity"
    f = json.loads(r[2])
    assert f["buyers"] == 1 and f["dex"] == "pumpfun" and f["sniper.seen_trades"] is True and f["sniper.mc_sol"] > 27
    store.update_launch(conn, "GELEG", ret_60m=-0.95, rugged=1)
    assert opportunity.report_result(conn, "GELEG", c)
    assert tuple(conn.execute("SELECT source_result, source_horizon_min FROM opportunities WHERE ref='GELEG'").fetchone()) \
        == (-1.0, 60)


def test_queued_launch_is_reported_at_the_first_look_and_supplemented_after_the_deep_check(tmp_path):
    import json
    import brain.opportunities as bo
    from sniper import opportunity, store
    from sniper.engine import Engine
    from sniper.tests.test_sniper import Curve, FakeEnricher, early_from, organic
    conn = store.connect(str(tmp_path / "q.db"))
    c = cfg()
    eng = Engine(c, conn, enricher=FakeEnricher())
    cv = Curve("BUNDLE")
    eng.handle(cv.create(), T0)
    bundle = [(f"bndl{i}", cv.buy(f"bndl{i}", 3.0, seen=False)) for i in range(4)]
    organic(eng, cv, T0 + 1, n=30)
    st = eng.tick(T0 + c.decide_sec + 1)
    r = conn.execute("SELECT ts, source_action, features FROM opportunities WHERE ref='BUNDLE'").fetchone()
    assert r and r[1] == "queued" and r[0] == T0 + c.decide_sec + 1      # reported at the first look
    assert "sniper.final_action" not in json.loads(r[2])
    eng.finalize(st[0], {"early": early_from(cv, bundle=bundle)}, now=T0 + 22)
    assert conn.execute("SELECT COUNT(*) FROM opportunities WHERE source='sniper'").fetchone()[0] == 1
    if not hasattr(bo, "supplement"):
        return   # brain without supplement: the 20-s row stays
    r = conn.execute("SELECT ts, source_action, features, supplemented_at FROM opportunities WHERE ref='BUNDLE'").fetchone()
    f = json.loads(r[2])
    assert r[0] == T0 + c.decide_sec + 1 and r[1] == "queued" and r[3]          # moment kept, supplement stamped
    assert f["sniper.final_action"] == "skip" and f["sniper.final_reason"] == "bundle" and f["sniper.deep"] is True
    assert f["bundle"] is True and f["sniper.bundle_wallets"] == 4


def test_budget_skip_of_a_queued_launch_supplements_its_row(tmp_path):
    import json
    import brain.opportunities as bo
    from sniper import store
    from sniper.engine import Engine
    from sniper.tests.test_sniper import Curve, organic
    conn = store.connect(str(tmp_path / "b.db"))
    c = cfg()
    eng = Engine(c, conn)
    cv = Curve("NOBUDGET")
    eng.handle(cv.create(), T0)
    [cv.buy(f"bndl{i}", 3.0, seen=False) for i in range(4)]
    organic(eng, cv, T0 + 1, n=30)
    st = eng.tick(T0 + c.decide_sec + 1)[0]
    assert conn.execute("SELECT source_action FROM opportunities WHERE ref='NOBUDGET'").fetchone()[0] == "queued"
    eng.skip_budget(st)
    if hasattr(bo, "supplement"):
        f = json.loads(conn.execute("SELECT features FROM opportunities WHERE ref='NOBUDGET'").fetchone()[0])
        assert f["sniper.final_action"] == "skip" and f["sniper.final_reason"] == "bundle" and f["sniper.deep"] is False


def test_silent_stream_moves_to_the_next_rpc():
    ls = rpcws.LogStream("wss://a", lambda m: None, 1, mode="program", program=rpcws.PUMP_PROGRAM,
                         fallbacks=["wss://b", "wss://a", ""])
    assert ls.urls == ["wss://a", "wss://b"]
    assert ls.next_url() == "wss://b" and ls.next_url() == "wss://a" and ls.switches == 2
    one = rpcws.LogStream("wss://a", lambda m: None, 1, mode="program", program=rpcws.PUMP_PROGRAM)
    assert one.next_url() == "wss://a"


def test_curve_live_round_trip_for_other_processes(tmp_path):
    from sniper import store
    from sniper.engine import Engine
    path = str(tmp_path / "l.db")
    eng = Engine(cfg(), store.connect(path), enricher=None)
    st = LaunchState(mint="M1", created_ts=T0)
    st.v_sol, st.v_tok, st.mcap_sol, st.n_seen, st.last_trade_ts = 40.0, 8e8, 50.0, 7, T0 + 5
    eng.launches["M1"] = st
    old = LaunchState(mint="M2", created_ts=T0 - 100)
    old.v_sol, old.v_tok, old.last_trade_ts = 30.0, 1e9, T0 - 90
    eng.launches["M2"] = old
    rows = eng.curve_live(T0)
    assert [r["mint"] for r in rows] == ["M1"]
    store.save_curve_live(eng.conn, rows)
    other = store.connect(path)                     # copytrade reads from its own connection
    c = store.curve_now(other, "M1", max_age_s=30, now=T0 + 20)
    assert c["price_sol"] == pytest.approx(5e-8) and c["v_sol"] == 40.0 and c["n_trades"] == 7
    assert c["complete"] is False and c["pool"] == st.pool
    assert store.curve_now(other, "M1", max_age_s=30, now=T0 + 60) is None
    assert store.curve_now(other, "XX") is None


def test_stream_2_goes_to_helius_first_and_leaves_it_at_the_reserve(tmp_path, monkeypatch):
    from core import http as core_http
    from sniper import service
    monkeypatch.setenv("HELIUS_API_KEY", "k123")
    svc = service.Service(cfg(), db_path=str(tmp_path / "h.db"))
    l2 = svc.logs2
    assert l2.url.startswith("wss://mainnet.helius-rpc.com/") and svc.cfg.ws_url2 in l2.urls
    booked, used = [], {"used": 0, "limit": 1000}
    monkeypatch.setattr(core_http, "_charge", lambda c, force=False: booked.append((c, force)))
    monkeypatch.setattr(core_http, "credits_status", lambda: dict(used))
    monkeypatch.setattr(core_http, "plan_pace", lambda: {"used": 0, "on_pace": 1, "ratio": 0.0}, raising=False)
    l2.bytes = 250_000
    svc.helius_meter()
    assert booked == [(5.0, True)] and not svc.helius_paused and l2.url == svc.helius_ws
    used["used"] = 950                                  # past the 10 % reserve: off Helius, onto the public RPC
    svc.helius_meter()
    assert svc.helius_paused and not l2.url.startswith("wss://mainnet.helius") and l2._kick
    assert l2.next_url() != svc.helius_ws               # skipped while paused
    used["used"] = 100
    svc.helius_meter()
    assert not svc.helius_paused and svc.helius_ws not in l2.skip
    pace = {"used": 100, "on_pace": 100, "ratio": 1.05}  # ahead of the calendar: step back, return below 0.95
    monkeypatch.setattr(core_http, "plan_pace", lambda: dict(pace), raising=False)
    svc.helius_meter()
    assert "Kalender" in svc.helius_paused
    pace["ratio"] = 0.97
    svc.helius_meter()
    assert svc.helius_paused
    pace["ratio"] = 0.9
    svc.helius_meter()
    assert not svc.helius_paused
    pace["ratio"] = 3.0                                  # pace switched off by operations: only reserve and day cap
    svc.cfg.ws_helius_pace = False
    svc.helius_meter()
    assert not svc.helius_paused
    svc.cfg.ws_helius_credits_day = 1.0                 # day budget: off Helius once today's credits are spent
    l2.bytes += 100_000
    svc.helius_meter()
    assert "Tagesgrenze" in svc.helius_paused and not l2.url.startswith("wss://mainnet.helius")
    monkeypatch.setenv("HELIUS_API_KEY", "")
    assert not service.Service(cfg(), db_path=str(tmp_path / "n.db")).logs2.url.startswith("wss://mainnet.helius")


def test_stream_errors_never_carry_the_key():
    assert "k123" not in rpcws._redact("server rejected WebSocket connection wss://x/?api-key=k123: HTTP 401")


# real TradeEvents of 18.10. (sniper.log) of curves quoted in another token: PUMP and a pump.fun coin
QUOTED_PUMP = ("vdt/007mYe4366GPIfGooYV2XtP60qIvrIiVphzPNx2tZp2hedtX/wAAAAAAAAAA6MudppEDAAABZp57pIP7glBlAA+qJJuCe7l+yEsD"
               "G4GwIXsPmhKBR+7oKdRqAAAAAAAAAAAAAAAA7aJir1FXAQAAAAAAAAAAAO0KUGPAWAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
               "AAAAAABfAAAAAAAAACMKogwAAAAArP1lbfo0oKcXm1KZQUhqwlnr+VczXeg14NpXa9Ce06vIAAAAAAAAACGBmBoAAAAAAAAAAAAAAAAA"
               "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAEgAAAGJ1eV9leGFjdF9xdW90ZV9pbgAAAAAAAAAAAAAAAAAAAAAAiBMAAAAAAAARBVEGAAAA"
               "AAAAAAAMRfffjZ5ylWKEkz9tmLdXAy6D34RgT7XhF//2HVsS+W84yTEFAAAArI8R4vgBAACZsalDRwEAAAAAAAAAAAAAAAAAAAAAAADl"
               "6CfdBAAAAA==")
QUOTED_COIN = ("vdt/007mYe61JjCNQKyas1ovSwLTBonx8jmWcADGpo5LNmiQr8EQjwAAAAAAAAAAGWK2ug0uAAAB3fbOecXTXpMydlpcDzEsbjMu"
               "6NNv/V8WQXlsi5Z4nP2XHtRqAAAAAAAAAAAAAAAA560hjdWhAwAAAAAAAAAAAOcVD0FEowIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
               "AAAAAAAAAABfAAAAAAAAAE5cCdgBAAAAF8kqXw9jUQ8cPTDN8qTMIwRqMO8TFCCAKEsjjRmo6HwsAQAAAAAAAOhLpNIFAAAAAAAAAAAA"
               "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAwAAAGJ1eQAAAAAAAAAAAAAAAAAAAAAAiBMAAAAAAAAnrgTsAAAAAAAAAAB4JNJflfqv"
               "HgOaqQ2uXu9nqsnqtspb9puh525fVRJ+D3g3DxjCAAAAB65E5hAQAAB4Nw8YwgAAACwBAAAAAAAA6Euk0gUAAADoS6TSBQAAAA==")


def test_quoted_curves_are_priced_in_sol(monkeypatch):
    import base64
    monkeypatch.setattr(rpcws, "QUOTE_PRICE", None)
    q = rpcws.quote_fields(base64.b64decode(QUOTED_PUMP))
    assert q == (rpcws.PUMP_TOKEN_MINT, 22310107247, 2168456318892, 1405589500313)
    assert rpcws.quote_fields(base64.b64decode(QUOTED_COIN))[0] == "95zVNJn96PE9aEvUwgg5AWa6TTQNj9v26DrmSnQbpump"
    assert rpcws.decode_trade(QUOTED_PUMP) is None           # no price yet: held back, not a decoder reject
    assert rpcws._UNPRICED[0] == rpcws.PUMP_TOKEN_MINT
    monkeypatch.setattr(rpcws, "QUOTE_PRICE", lambda m: 2.5e-5)
    m = rpcws.to_stream_msg(rpcws.decode_trade(QUOTED_PUMP), "S")
    assert m["quoteMint"] == rpcws.PUMP_TOKEN_MINT and m["txType"] == "buy"
    assert m["feePct"] == pytest.approx(0.95 + 2.0 + rpcws.QUOTE_SWAP_FEE_PCT)   # protocol + creator + swap leg
    assert m["solAmount"] == pytest.approx(22310.107247 * 2.5e-5, rel=1e-6)
    assert m["vSolInBondingCurve"] == pytest.approx(2168456.318892 * 2.5e-5, rel=1e-6)
    # the trade's own price sits just below the curve's price after it (a buy moves the price up)
    assert 0.9 < (m["solAmount"] / m["tokenAmount"]) / (m["marketCapSol"] / 1e9) < 1.0
    st = LaunchState(mint=m["mint"], created_ts=T0)
    st.apply_trade(dict(m, txType="buy"), T0 + 1)
    assert st.start_mcap_sol == pytest.approx((2168456.318892 - 1405589.500313) * 2.5e-5 / 1.073e9 * 1e9, rel=1e-3)


def test_held_back_quoted_trades_replay_once_priced(tmp_path, monkeypatch):
    from sniper import feed, service
    svc = service.Service(cfg(), db_path=str(tmp_path / "q.db"))
    note_ = {"method": "logsNotification", "params": {"result": {"context": {"slot": 7}, "value": {
        "signature": "SIGQ", "err": None,
        "logs": ["Program 6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P invoke [1]", "Program data: " + QUOTED_PUMP,
                 "Program 6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P success"]}}}}
    svc.quote_px.pop(rpcws.PUMP_TOKEN_MINT, None)
    assert rpcws.parse_notification(note_) == [] and rpcws.QUOTE_PENDING[rpcws.PUMP_TOKEN_MINT]
    seen = []
    monkeypatch.setattr(svc, "_on_rpc_trade", seen.append)
    monkeypatch.setattr(svc, "sol_usd", lambda: 160.0)
    monkeypatch.setattr(feed, "token_prices_sol", lambda mints, sol_usd: {m: 2.5e-5 for m in mints})
    svc.quote_round()
    assert [x["signature"] for x in seen] == ["SIGQ"] and seen[0]["_rx"] > 0
    assert svc.quote_price(rpcws.PUMP_TOKEN_MINT) == 2.5e-5 and rpcws.PUMP_TOKEN_MINT not in rpcws.QUOTE_PENDING
    # a quote that is a coin we follow live prices itself from its own curve
    st = LaunchState(mint="QC", created_ts=time.time())
    st.v_sol, st.v_tok, st.last_trade_ts = 40.0, 8e8, time.time()
    svc.engine.launches["QC"] = st
    assert svc.quote_price("QC") == pytest.approx(5e-8)


def test_quote_decimals_and_the_dev_buy_of_a_quoted_create(tmp_path, monkeypatch):
    from core import http as core_http
    from sniper import feed, service
    assert rpcws.quote_decimals("XYZpump") == 6 and rpcws.quote_decimals(rpcws.WSOL_MINT) == 9
    assert rpcws.quote_decimals("PEPEunknownMint111") is None
    monkeypatch.setattr(rpcws, "QUOTE_PRICE", lambda m: 1e-5)
    monkeypatch.setattr(rpcws, "quote_decimals", lambda m: None)   # decimals not known yet: held back as well
    assert rpcws.decode_trade(QUOTED_PUMP) is None and rpcws._UNPRICED[0] == rpcws.PUMP_TOKEN_MINT
    t = rpcws.trade_tokens(QUOTED_PUMP)                 # the dev buy's size counts without any price
    assert t["is_buy"] and t["token_raw"] == 3924100500456 and t["v_sol_lamports"] is None
    monkeypatch.undo()
    svc = service.Service(cfg(), db_path=str(tmp_path / "d.db"))
    calls = []
    monkeypatch.setattr(svc, "sol_usd", lambda: 160.0)
    monkeypatch.setattr(feed, "token_prices_sol", lambda mints, sol_usd: {m: 1e-3 for m in mints})
    monkeypatch.setattr(core_http, "post_json", lambda url, p, **kw: calls.append(p["params"][0])
                        or {"result": {"value": {"decimals": 9}}})
    monkeypatch.setitem(rpcws.QUOTE_WANTED, "QuoteX1", time.time())
    svc.quote_round()
    assert calls == ["QuoteX1"] and rpcws.QUOTE_DECIMALS.pop("QuoteX1") == 9


def test_coverage_tells_missed_from_untraded(tmp_path):
    from sniper import store
    from sniper.engine import Engine
    eng = Engine(cfg(), store.connect(str(tmp_path / "z.db")), enricher=None)
    t = T0 - 600
    eng.cover["decide"].extend([(t, True), (t, True), (t, False), (t, False), (t, False)])
    eng.late_first.extend([(t, 40.0), (t, 900.0)])    # one traded 40 s after the decision, one only after 15 min
    lt = eng.coverage(T0)["late"]
    assert lt == {"zero": 3, "late_5m": 1, "never_5m": 2, "share_of_traded": 0.667}


HIGH_START = ("vdt/007mYe6/fif5RRQ9e8wLnzqCv/5OPigT88eMe8EWUGA2tJ3A3xDh3hYAAAAA0sCA9kUBAAAA8/9O0K/ujOjIDlPbLnMBTftCWxzM"
              "+5k8zagF6DknvHP6T9RqAAAAAO34yxNBAAAAkzVAQcmgAwDyvuFjAAAAAJOdLfU3ogIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
              "AAAAAABfAAAAAAAAABGfNwAAAAAAY7+VOaK3oNAaM4ZhuI2qK/lMEWHxadQylSiGEF1YY5weAAAAAAAAAI2QEQAAAAAAAAAAAAAAAAAA"
              "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABAAAAHNlbGwBAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
              "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAQ4d4WAAAAAO34yxNBAAAA8r7hYwAAAAAAAAAAAAAAAAAAAAAAAAAA9dBsAAAAAAA=")


def test_curve_starting_near_278_virtual_sol_is_accepted():
    ev = rpcws.decode_trade(HIGH_START)      # real sell of 18.10. 06:50 (DtWSid...pump), sol_high before
    m = rpcws.to_stream_msg(ev, "S")
    assert m["txType"] == "sell" and m["vSolInBondingCurve"] == pytest.approx(279.505, abs=1e-3)
    assert (m["solAmount"] / m["tokenAmount"]) / (m["marketCapSol"] / 1e9) == pytest.approx(1.0, abs=0.01)


def _tx(keys, pre, post, tok_pre, tok_post, mint="MASCHE"):
    tb = lambda xs: [{"mint": mint, "owner": o, "accountIndex": i, "uiTokenAmount": {"uiAmount": a}}  # noqa: E731
                     for i, (o, a) in enumerate(xs)]
    return {"meta": {"err": None, "preBalances": pre, "postBalances": post, "preTokenBalances": tb(tok_pre),
                     "postTokenBalances": tb(tok_post)},
            "transaction": {"message": {"accountKeys": [{"pubkey": k} for k in keys]}}}


def test_launch_block_buyers_from_the_chain():
    from sniper import launchblock
    # create: the creator buys 79.31 % (excluded by name); the curve gains its rest without paying
    create = _tx(["DEV", "CURVE"], [100e9, 0], [14e9, 86e9], [], [("DEV", 793.1e6), ("CURVE", 206.9e6)])
    # setup buy on the AMM in the same block: 160 SOL from the buyer's own account; the pool vault receives SOL
    setup = _tx(["SETUP", "POOL"], [200e9, 86e9], [40e9, 246e9], [("POOL", 206.9e6)], [("SETUP", 150e6), ("POOL", 56.9e6)])
    later = _tx(["LATE"], [5e9], [4e9], [], [("LATE", 1e6)])
    sigs = [{"signature": "C", "slot": 100}, {"signature": "S", "slot": 100}, {"signature": "L", "slot": 140}]
    txs = {"C": create, "S": setup, "L": later}

    def rpc(method, params):
        if method == "getTransaction" and params[1].get("maxSupportedTransactionVersion", -1) < 1:
            raise RuntimeError("-32015 Transaction version (1) is not supported")   # what the RPC answers for v1
        return sigs if method == "getSignaturesForAddress" else txs[params[0]]
    rows = launchblock.block_buyers(rpc, "MASCHE", 100, exclude=("DEV",))
    assert [(r["wallet"], r["role"], round(r["sol"]), round(r["tokens"] / 1e6, 1)) for r in rows] == \
        [("SETUP", "launch_block", 160, 150.0)]


def test_only_launches_that_migrate_at_once_get_a_block_read(tmp_path):
    from sniper import store
    from sniper.engine import Engine
    eng = Engine(cfg(), store.connect(str(tmp_path / "b.db")), enricher=None)
    fast, slow = LaunchState(mint="F", created_ts=T0), LaunchState(mint="S", created_ts=T0)
    for st, mig in ((fast, T0 + 2), (slow, T0 + 900)):
        st.migrated, st.migrated_ts, st.decided_ts = True, mig, T0 + 75
        eng.launches[st.mint] = st
    assert [s.mint for s in eng.block_due(T0 + 1000)] == ["F"] and eng.block_due(T0 + 1000) == []


def test_coverage_line_runs_by_the_clock_and_survives_a_restart(tmp_path, caplog):
    import logging
    from sniper import service
    assert service.until_hour(3600 * 10 + 60) == 360                      # 10:01 -> 10:07
    assert service.until_hour(3600 * 10 + 7 * 60) == 3600                  # at :07 -> next hour
    assert service.until_hour(3600 * 10 + 30 * 60) == 37 * 60               # 10:30 -> 11:07
    svc = service.Service(cfg(), db_path=str(tmp_path / "c.db"))
    now = svc.started + 1320                                                 # deploy 22 min ago
    svc.engine.cover["decide"].extend([(now - 600, True), (now - 500, False)])
    with caplog.at_level(logging.INFO, logger="sniper"):
        svc.coverage_round(now)
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Abdeckung")]
    assert lines and lines[0].startswith("Abdeckung (22 min seit Start): Launches mit Trades beim Entscheid 50 % von 2")


def test_launch_block_counts_spent_sol_not_wrapped_sol():
    from sniper import launchblock
    W = launchblock.WSOL
    tb = lambda xs: [{"mint": m, "owner": o, "accountIndex": i, "uiTokenAmount": {"uiAmount": a}}  # noqa: E731
                     for i, (m, o, a) in enumerate(xs)]
    # wraps 5000 SOL, spends 1 SOL of it on the AMM, keeps 4999 in its WSOL account
    keep = {"meta": {"err": None, "preBalances": [5001e9, 0], "postBalances": [1e9, 0],
                     "preTokenBalances": tb([(W, "BIG", 0.0), ("M", "POOL", 100e6)]),
                     "postTokenBalances": tb([(W, "BIG", 4999.0), ("M", "POOL", 99e6), ("M", "BIG", 1e6), (W, "POOL", 1.0)])},
            "transaction": {"message": {"accountKeys": [{"pubkey": "BIG"}, {"pubkey": "POOL"}]}}}
    # wrapped earlier: pays 150 SOL in WSOL only, native lamports just the fee
    wrapped = {"meta": {"err": None, "preBalances": [1e9], "postBalances": [0.99999e9],
                        "preTokenBalances": tb([(W, "OLD", 150.0)]), "postTokenBalances": tb([(W, "OLD", 0.0), ("M", "OLD", 5e6)])},
               "transaction": {"message": {"accountKeys": [{"pubkey": "OLD"}]}}}
    assert [(w, round(sol, 3)) for w, _t, sol in launchblock.tx_buyers(keep, "M")] == [("BIG", 1.0)]
    assert [(w, round(sol, 2)) for w, _t, sol in launchblock.tx_buyers(wrapped, "M")] == [("OLD", 150.0)]


def test_launch_block_caps_the_spend_at_what_the_pool_received():
    from sniper import launchblock
    W = launchblock.WSOL
    tb = lambda xs: [{"mint": m, "owner": o, "accountIndex": i, "uiTokenAmount": {"uiAmount": a}}  # noqa: E731
                     for i, (m, o, a) in enumerate(xs)]
    # 166 SOL leave the buyer, but the pool's WSOL vault gains 0.9: the rest went to a side account (SIDE)
    tx = {"meta": {"err": None, "preBalances": [170e9, 0, 0], "postBalances": [4e9, 0, 165.1e9],
                   "preTokenBalances": tb([("M", "POOL", 100e6), (W, "POOL", 85.0)]),
                   "postTokenBalances": tb([("M", "POOL", 99e6), (W, "POOL", 85.9), ("M", "BUY", 1e6)])},
          "transaction": {"message": {"accountKeys": [{"pubkey": "BUY"}, {"pubkey": "POOL"}, {"pubkey": "SIDE"}]}}}
    assert [(w, round(sol, 2)) for w, _t, sol in launchblock.tx_buyers(tx, "M")] == [("BUY", 0.9)]


def test_migration_coverage_counts_curves_completed_by_the_create_apart(tmp_path):
    from sniper import store
    from sniper.engine import Engine
    from sniper.tests.test_sniper import Curve, FakeEnricher
    eng = Engine(cfg(), store.connect(str(tmp_path / "m.db")), enricher=FakeEnricher())
    masche, normal, missed = Curve("MASCHE"), Curve("NORMAL"), Curve("MISSED")
    eng.handle(masche.create(dev_sol=86.0), T0)                 # the dev buys the whole curve in the create
    eng.handle(normal.create(), T0)
    eng.handle(normal.buy("W1", 1.0), T0 + 2)
    eng.handle(missed.create(), T0)                              # traded, but we saw none of it
    for m in ("MASCHE", "NORMAL", "MISSED"):
        eng.handle({"txType": "migrate", "mint": m, "pool": "pump-amm"}, T0 + 5)
    c = eng.coverage(T0 + 10)
    assert c["migrate"] == {"n": 2, "share": 0.5} and c["migrate_instant"]["n"] == 1


def test_trade_before_its_create_is_kept_and_replayed(tmp_path):
    from sniper import store
    from sniper.engine import Engine
    from sniper.tests.test_sniper import Curve, FakeEnricher
    eng = Engine(cfg(), store.connect(str(tmp_path / "o.db")), enricher=FakeEnricher())
    cv = Curve("PROINU")
    create = cv.create()
    big = cv.buy("W75n", 68.7)                                  # same slot, delivered before the create
    assert eng.handle(big, T0) is None
    st = eng.handle(create, T0 + 0.4)
    assert st.n_seen == 1 and st.created_ts == T0 and "W75n" in st.balances
    eng.handle(cv.buy("OLD", 0.1) | {"mint": "UNKNOWN"}, T0 + 1)  # an untracked coin's trade stays an orphan
    eng.handle(cv.buy("LATE", 0.1) | {"mint": "UNKNOWN"}, T0 + 100)
    assert len(eng.orphans["UNKNOWN"]) == 2 and "PROINU" not in eng.orphans
