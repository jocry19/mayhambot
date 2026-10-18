"""Bonk (Raydium LaunchLab) trades: event decoding on a real transaction, the invoke-stack filter, pool PDA,
traders attached afterwards, the service mapping pool -> launch, per-launch fees in paper fills and research."""
import os
import sys

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
