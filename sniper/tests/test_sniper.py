"""sniper/ on fixture data (no network): clean launch -> paper trade with tp1 and trailing stop, bundled
launch, shared funders, dev dump, dead launch, insider-sell exit, outcomes, stats, parsers, call budgets."""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pytest  # noqa: E402

from sniper import enrich, feed, store  # noqa: E402
from sniper import analytics as sstats  # noqa: E402
from sniper.config import SniperSettings  # noqa: E402
from sniper.engine import Engine  # noqa: E402
from sniper.state import curve_buy, curve_sell  # noqa: E402

T0 = 1_760_000_000.0
DEV = "DEVwallet1111111111111111111111111111111111"


class Curve:
    def __init__(self, mint):
        self.mint, self.v_sol, self.v_tok, self.n = mint, 30.0, 1_073_000_000.0, 0

    def _msg(self, wallet, side, sol, tok, bal):
        self.n += 1
        return {"signature": f"sig-{self.mint}-{self.n}", "mint": self.mint, "traderPublicKey": wallet, "txType": side,
                "tokenAmount": tok, "solAmount": sol, "newTokenBalance": bal, "bondingCurveKey": "curve",
                "vSolInBondingCurve": self.v_sol, "vTokensInBondingCurve": self.v_tok,
                "marketCapSol": self.v_sol / self.v_tok * 1e9, "pool": "pump"}

    def buy(self, wallet, sol, bal=None, seen=True):
        tok = curve_buy(self.v_sol, self.v_tok, sol, 0)
        self.v_sol += sol
        self.v_tok -= tok
        return self._msg(wallet, "buy", sol, tok, bal if bal is not None else tok) if seen else tok

    def sell(self, wallet, tok, bal=0.0):
        sol = curve_sell(self.v_sol, self.v_tok, tok, 0)
        self.v_sol -= sol
        self.v_tok += tok
        return self._msg(wallet, "sell", sol, tok, bal)

    def create(self, dev_sol=0.5):
        tok = curve_buy(self.v_sol, self.v_tok, dev_sol, 0)
        self.v_sol += dev_sol
        self.v_tok -= tok
        self.dev_tokens = tok
        return {"signature": f"create-{self.mint}", "mint": self.mint, "traderPublicKey": DEV, "txType": "create",
                "initialBuy": tok, "solAmount": dev_sol, "bondingCurveKey": "curve", "vTokensInBondingCurve": self.v_tok,
                "vSolInBondingCurve": self.v_sol, "marketCapSol": self.v_sol / self.v_tok * 1e9, "name": "Test",
                "symbol": "TST", "uri": "ipfs://x", "pool": "pump"}


def cfg(**kw):
    c = SniperSettings()
    c.use_jupiter = False
    c.safety_gate = True
    for k, v in kw.items():
        setattr(c, k, v)
    return c


class FakeEnricher:
    def __init__(self, safety=None):
        self.safety = safety or {"factor": 1.0, "blocked": False, "risk": 10, "level": "low", "reasons": []}
        self.gates = 0

    def safety_gate(self, conn, mint):
        self.gates += 1
        return self.safety


@pytest.fixture
def conn():
    d = tempfile.mkdtemp()
    c = store.connect(os.path.join(d, "t.db"))
    yield c
    c.close()


def organic(eng, cv, start, n=30, sol=0.6, step=0.5):
    for i in range(n):
        eng.handle(cv.buy(f"buyer{i:03d}", sol + 0.01 * i), start + i * step)


def early_from(cv, slot0=100, bundle=()):
    """Early list as pump.fun would return it: creation slot with the dev (+ bundle wallets), then organic."""
    out = [{"sig": f"create-{cv.mint}", "wallet": DEV, "side": "buy", "tokens": cv.dev_tokens, "sol": 0.5,
            "slot": slot0, "ts": T0}]
    for w, tok in bundle:
        out.append({"sig": f"b-{w}", "wallet": w, "side": "buy", "tokens": tok, "sol": 1.0, "slot": slot0, "ts": T0})
    return out


def test_clean_launch_trades_tp1_and_trailing_stop(conn):
    c = cfg()
    eng = Engine(c, conn, enricher=FakeEnricher())
    cv = Curve("CLEAN")
    eng.handle(cv.create(), T0)
    assert "CLEAN" in eng.to_sub
    organic(eng, cv, T0 + 1, n=30)
    cands = eng.tick(T0 + c.decide_sec + 1)
    assert [s.mint for s in cands] == ["CLEAN"], conn.execute("SELECT skip_reason, prescore FROM sniper_launches").fetchall()
    st = cands[0]
    enr = {"early": early_from(cv), "funders": {f"buyer{i:03d}": f"funder{i}" for i in range(6)},
           "coin": {"twitter": "x.com/t"}, "creator_hist": {"n": 1}, "dev_coins": {"n": 1, "n24": 1, "n_complete": 0}}
    a, reason, pos = eng.finalize(st, enr, now=T0 + 22)
    assert reason is None and pos, (reason, a.reasons, a.score)
    assert not a.bundle and not a.insider
    assert pos["fill_src"] == "bonding_curve" and pos["tokens"] > 0
    assert eng.enricher.gates == 1
    bal0 = eng.book.balance(conn)
    # pump +70 % -> first profit push sells half
    tt = T0 + 30
    while cv.v_sol / cv.v_tok * 1e9 < pos["entry_mcap_sol"] * 1.75:
        eng.handle(cv.buy(f"late{int(tt)}", 2.0), tt)
        tt += 1
    p = conn.execute("SELECT * FROM sniper_positions").fetchone()
    assert p["tp1_done"] == 1 and p["status"] == "open" and p["remaining"] < p["tokens"]
    # fall 30 % from the peak -> trailing stop closes the rest
    k = 0
    while conn.execute("SELECT status FROM sniper_positions").fetchone()[0] == "open":
        eng.handle(cv.sell(f"late{int(T0 + 30 + k)}", 20_000_000), tt)
        tt += 1
        k += 1
        assert k < 200
    p = conn.execute("SELECT * FROM sniper_positions").fetchone()
    assert p["exit_reason"] == "trail" and p["pnl_sol"] > 0
    assert eng.book.balance(conn) > bal0
    # outcomes 1/5/15/60 min after the decision, measured from the decision mcap
    for m in (1, 5, 15, 60):
        eng.tick(T0 + 21 + m * 60 + 1)
    r = conn.execute("SELECT * FROM sniper_launches WHERE mint='CLEAN'").fetchone()
    assert r["decision"] == "enter" and r["stage"] == 2 and r["done"] == 1
    assert all(r[f"ret_{h}m"] is not None for h in (1, 5, 15, 60))
    assert r["max_ret_60m"] >= 0.7


def test_bundled_launch_is_skipped_and_still_measured(conn):
    c = cfg()
    eng = Engine(c, conn, enricher=FakeEnricher())
    cv = Curve("BUNDLE")
    eng.handle(cv.create(), T0)
    bundle = [(f"bndl{i}", cv.buy(f"bndl{i}", 3.0, seen=False)) for i in range(4)]  # same block, before our sub
    organic(eng, cv, T0 + 1, n=30)
    st = eng.tick(T0 + c.decide_sec + 1)
    stage1 = conn.execute("SELECT unseen_pct, bundle_pct FROM sniper_launches WHERE mint='BUNDLE'").fetchone()
    assert stage1["unseen_pct"] > 15  # the curve dropped more than the trades we saw explain
    assert len(st) == 1 and st[0].stage1_reason == "bundle"  # strong flow: queued, but marked by the stream
    a, reason, pos = eng.finalize(st[0], {"early": early_from(cv, bundle=bundle)}, now=T0 + 22)
    assert reason == "bundle" and pos is None
    assert a.features["bundle_wallets"] == 4 and a.features["bundle_pct"] > 15
    assert any(r["check"] == "bundle" for r in a.reasons)
    roles = {w["role"] for w in conn.execute("SELECT role FROM sniper_launch_wallets WHERE mint='BUNDLE'")}
    assert "bundle" in roles
    for m in (1, 5, 15, 60):
        eng.tick(T0 + 21 + m * 60 + 1)
    r = conn.execute("SELECT * FROM sniper_launches WHERE mint='BUNDLE'").fetchone()
    assert r["decision"] == "skip" and r["ret_60m"] is not None


def test_budget_skip_keeps_the_stream_verdict(conn):
    c = cfg()
    eng = Engine(c, conn)
    cv = Curve("NOBUDGET")
    eng.handle(cv.create(), T0)
    [cv.buy(f"bndl{i}", 3.0, seen=False) for i in range(4)]
    organic(eng, cv, T0 + 1, n=30)
    st = eng.tick(T0 + c.decide_sec + 1)[0]
    eng.skip_budget(st)
    assert conn.execute("SELECT skip_reason FROM sniper_launches").fetchone()[0] == "bundle"


def test_shared_funder_marks_insiders(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS research_funding (wallet TEXT PRIMARY KEY, funder TEXT, ts INTEGER, "
                 "sol REAL, status TEXT, checked_at INTEGER)")
    for i in range(3):
        conn.execute("INSERT INTO research_funding VALUES (?,?,0,1,'ok',0)", (f"buyer{i:03d}", "HUBwallet"))
    conn.commit()
    c = cfg()
    eng = Engine(c, conn, enricher=FakeEnricher())
    cv = Curve("INSIDE")
    eng.handle(cv.create(), T0)
    organic(eng, cv, T0 + 1, n=30, sol=1.0)
    st = eng.tick(T0 + c.decide_sec + 1)[0]
    funders = store.cached_funders(conn, [f"buyer{i:03d}" for i in range(10)])
    assert funders == {f"buyer{i:03d}": "HUBwallet" for i in range(3)}
    a, reason, _ = eng.finalize(st, {"early": early_from(cv), "funders": funders}, now=T0 + 22)
    assert a.insider and reason == "insider"
    assert any("finanziert" in r["text"] for r in a.reasons)


def test_dev_dump_and_dead_launch_skip_at_stage1(conn):
    c = cfg()
    eng = Engine(c, conn)
    cv = Curve("DUMP")
    eng.handle(cv.create(), T0)
    organic(eng, cv, T0 + 1, n=10)
    eng.handle(cv.sell(DEV, cv.dev_tokens * 0.9, bal=cv.dev_tokens * 0.1), T0 + 8)
    cv2 = Curve("DEAD")
    eng.handle(cv2.create(), T0)
    eng.handle(cv2.buy("one", 0.1), T0 + 3)
    assert eng.tick(T0 + c.decide_sec + 1) == []
    rows = {r["mint"]: r["skip_reason"] for r in conn.execute("SELECT mint, skip_reason FROM sniper_launches")}
    assert rows == {"DUMP": "dev_sold", "DEAD": "activity"}
    eng.tick(T0 + c.decide_sec + c.quiet_unsub_sec + 5)
    assert "DEAD" in eng.to_unsub and not eng.launches["DEAD"].subscribed


def test_exit_when_dev_sells_after_entry(conn):
    c = cfg()
    eng = Engine(c, conn, enricher=FakeEnricher())
    cv = Curve("DEVEXIT")
    eng.handle(cv.create(), T0)
    organic(eng, cv, T0 + 1, n=30)
    st = eng.tick(T0 + c.decide_sec + 1)[0]
    _, reason, pos = eng.finalize(st, {"early": early_from(cv)}, now=T0 + 22)
    assert pos, reason
    eng.handle(cv.sell(DEV, cv.dev_tokens * 0.5, bal=cv.dev_tokens * 0.5), T0 + 40)
    p = conn.execute("SELECT * FROM sniper_positions").fetchone()
    assert p["status"] == "closed" and p["exit_reason"] == "insider_sell"


def test_safety_block_and_capacity(conn):
    c = cfg(max_open=1)
    eng = Engine(c, conn, enricher=FakeEnricher({"factor": 0.0, "blocked": True, "risk": 90, "level": "critical",
                                                 "reasons": ["Honeypot"]}))
    cv = Curve("BLOCKED")
    eng.handle(cv.create(), T0)
    organic(eng, cv, T0 + 1, n=30)
    st = eng.tick(T0 + c.decide_sec + 1)[0]
    _, reason, pos = eng.finalize(st, {"early": early_from(cv)}, now=T0 + 22)
    assert pos is None and reason in ("hard", "safety")


def test_time_stop_and_restart_resume(conn):
    c = cfg()
    eng = Engine(c, conn, enricher=FakeEnricher())
    cv = Curve("RESUME")
    eng.handle(cv.create(), T0)
    organic(eng, cv, T0 + 1, n=30)
    st = eng.tick(T0 + c.decide_sec + 1)[0]
    _, _, pos = eng.finalize(st, {"early": early_from(cv)}, now=T0 + 22)
    assert pos
    eng2 = Engine(c, conn, enricher=FakeEnricher())  # restart
    assert "RESUME" in eng2.book.open and "RESUME" in eng2.to_sub
    eng2.handle(cv.buy("x", 0.01), T0 + 30)
    eng2.tick(T0 + 22 + c.time_stop_min * 60 + 5)
    assert conn.execute("SELECT exit_reason FROM sniper_positions").fetchone()[0] == "time"


def test_known_bundlers_raise_risk(conn):
    store.save_launch(conn, {"mint": "OLDRUG", "decided_ts": int(T0 - 9000), "stage": 2, "rugged": 1, "done": 1})
    store.save_wallets(conn, "OLDRUG", [{"wallet": "buyer001", "role": "bundle"}, {"wallet": "buyer002", "role": "insider"}])
    assert store.known_bad(conn, ["buyer001", "buyer002", "x"]) == {"buyer001": 1, "buyer002": 1}
    c = cfg()
    eng = Engine(c, conn, enricher=FakeEnricher())
    cv = Curve("REPEAT")
    eng.handle(cv.create(), T0)
    organic(eng, cv, T0 + 1, n=30)
    st = eng.tick(T0 + c.decide_sec + 1)[0]
    a, reason, _ = eng.finalize(st, {"early": early_from(cv), "known_bad": store.known_bad(conn, ["buyer001", "buyer002"])},
                                now=T0 + 22)
    assert a.insider and reason == "insider"


def test_stats_show_filter_value(conn):
    for i, (dec, reason, mx, r60) in enumerate([("enter", None, 1.5, 0.4), ("skip", "bundle", 0.1, -0.95),
                                                ("skip", "bundle", 2.0, 0.5), ("skip", "activity", -0.2, -0.9)]):
        store.save_launch(conn, {"mint": f"M{i}", "decided_ts": int(T0), "stage": 2 if reason != "activity" else 1,
                                 "decision": dec, "skip_reason": reason, "score": 60 - 10 * i, "max_ret_60m": mx,
                                 "ret_60m": r60, "ret_5m": 0.1, "calls_rpc": 2, "calls_api": 3})
    s = sstats.stats(conn, hours=0, now=T0 + 10)
    assert s["launches"] == 4 and s["entered"] == 1
    b = next(f for f in s["filters"] if f["code"] == "bundle")
    assert b["missed_winners"] == 1 and b["avoided_rugs"] == 1
    assert s["calls_per_deep_launch"]["rpc"] == 2
    assert s["groups"]["entered"]["hit_rate_2x"] == 1.0
    assert sstats.launch(conn, "M1")["skip_label"] == "Bundle beim Launch"
    assert len(sstats.recent(conn, 10)) == 4


def test_parsers():
    rows = [{"signature": "b", "mint": "M", "user": "w2", "is_buy": True, "token_amount": 5_000_000_000_000,
             "sol_amount": 1_000_000_000, "slot": 11, "timestamp": 1700000001, "tx_index": 0},
            {"signature": "a", "mint": "M", "user": "w1", "is_buy": True, "token_amount": 30_000_000_000_000,
             "sol_amount": 500_000_000, "slot": 10, "timestamp": 1700000000, "tx_index": 1}]
    e = enrich.parse_pump_trades(rows, "M")
    assert [x["wallet"] for x in e] == ["w1", "w2"] and e[0]["tokens"] == 30_000_000 and e[1]["sol"] == 1.0
    assert feed.parse('{"message":"Successfully subscribed"}') is None
    assert feed.parse(json.dumps({"mint": "M", "pool": "pump-amm", "signature": "s"}))["txType"] == "migrate"
    msgs = feed.sub_msgs({f"m{i}" for i in range(150)}, {"x"})
    assert len(msgs) == 3 and json.loads(msgs[0])["method"] == "subscribeTokenTrade"
    tx = {"slot": 5, "blockTime": 9, "transaction": {"message": {"accountKeys": [{"pubkey": "W"}, {"pubkey": "C"}]}},
          "meta": {"preBalances": [3_000_000_000, 0], "postBalances": [2_000_000_000, 1_000_000_000],
                   "preTokenBalances": [], "postTokenBalances": [
                       {"mint": "M", "owner": "W", "uiTokenAmount": {"uiAmountString": "1000"}}]}}
    r = enrich.parse_rpc_tx(tx, "M")
    assert r["wallet"] == "W" and r["tokens"] == 1000 and r["side"] == "buy" and r["sol"] == 1.0


def test_dex_prices(monkeypatch):
    def fake(url, **kw):
        return [{"baseToken": {"address": "M1"}, "quoteToken": {"address": feed.SOL_MINT}, "priceNative": "0.0000001",
                 "marketCap": 15000, "liquidity": {"usd": 9000}}]
    monkeypatch.setattr(feed.http, "get_json", fake)
    assert feed.dex_prices(["M1", "M2"], 150.0) == {"M1": 100.0}


def test_launch_fetcher_caps_rpc(monkeypatch):
    calls = []
    monkeypatch.setattr(enrich.http, "post_json", lambda url, payload, **kw: calls.append((payload, kw["priority"])) or {"result": []})
    b = enrich.Budget({"rpc": 3, "api": 10, "deep": 2})
    f = enrich.LaunchFetcher(cfg(), b, rpc_cap=2, rpc_url="https://rpc.example")
    f.rpc("getSlot", [])
    f.rpc("getSlot", [])
    with pytest.raises(enrich.BudgetExceeded):
        f.rpc("getSlot", [])  # per-launch cap
    f2 = enrich.LaunchFetcher(cfg(), b, rpc_cap=5, rpc_url="https://rpc.example")
    f2.rpc("getSlot", [])
    with pytest.raises(enrich.BudgetExceeded):
        f2.budget.take("rpc", 1) or (_ for _ in ()).throw(enrich.BudgetExceeded("minute"))
    assert len(calls) == 3 and b.left("rpc") == 0 and all(p == "live" for _, p in calls)


def test_enricher_uses_caches_before_network(conn, monkeypatch):
    """pump.fun API answers the early trades; no RPC call happens when the funders are cached."""
    conn.execute("CREATE TABLE IF NOT EXISTS research_funding (wallet TEXT PRIMARY KEY, funder TEXT, ts INTEGER, "
                 "sol REAL, status TEXT, checked_at INTEGER)")
    conn.execute("INSERT INTO research_funding VALUES ('w1','F1',0,1,'ok',0)")
    conn.commit()

    def get_json(url, params=None, headers=None, **kw):
        if "/trades/all/" in url:
            return [{"signature": "create-M", "mint": "M", "user": DEV, "is_buy": True, "token_amount": 10**13,
                     "sol_amount": 10**8, "slot": 50, "timestamp": 1, "tx_index": 0},
                    {"signature": "s1", "mint": "M", "user": "w1", "is_buy": True, "token_amount": 10**13,
                     "sol_amount": 10**8, "slot": 52, "timestamp": 2, "tx_index": 0}]
        if "/user-created-coins/" in url:
            return [{"mint": "M", "created_timestamp": 1_000}, {"mint": "OLD", "complete": True, "created_timestamp": 1_000}]
        return {"mint": "M", "twitter": "x"}
    monkeypatch.setattr(enrich.http, "get_json", get_json)
    monkeypatch.setattr(enrich.http, "post_json", lambda *a, **k: pytest.fail("no RPC expected"))
    c = cfg(funders_profiled=1, use_pump_api=True)
    e = enrich.Enricher(c)
    cv = Curve("M")
    eng = Engine(c, conn)
    st = eng.handle(cv.create(), T0)
    st.first_buy_ts["w1"] = T0 + 1
    enr = e.run(conn, st, now=T0 + 20)
    assert enr["early_src"] == "pump_api" and enr["funders"] == {"w1": "F1"} and enr["calls_rpc"] == 0
    assert enr["coin"]["twitter"] == "x" and enr["dev_coins"]["n_complete"] == 1 and enr["calls_api"] == 3


def test_launcher_has_sniper_service():
    import mayham
    assert mayham.SERVICES["sniper"][-2:] == ["sniper", "run"]


def test_no_buy_without_launch_data(conn):
    c = cfg()
    eng = Engine(c, conn, enricher=FakeEnricher())
    cv = Curve("NODATA")
    eng.handle(cv.create(), T0)
    organic(eng, cv, T0 + 1, n=30)
    st = eng.tick(T0 + c.decide_sec + 1)[0]
    _, reason, pos = eng.finalize(st, {"errors": ["pump.fun down"]}, now=T0 + 22)
    assert reason == "unverified" and pos is None


def test_heartbeat(tmp_path):
    from sniper.service import Service
    svc = Service(cfg(), db_path=str(tmp_path / "hb.db"))
    svc.connected, svc.ws_msgs = True, 7
    svc.heartbeat()
    hb = json.loads(store.get_state(svc.conn, "service_status"))
    assert hb["connected"] and hb["ws_msgs"] == 7 and hb["queue"] == 0
    assert set(hb["budget"]) == {"rpc", "api", "deep"} and hb["budget"]["rpc"]["per_min"] == 10


def _trade_event(mint_b, user_b, sol, tok, buy, vsol, vtok, ts=1_760_000_000):
    import base64
    import struct
    from sniper import rpcws
    raw = rpcws.TRADE_DISC + mint_b + struct.pack("<QQ", sol, tok) + bytes([1 if buy else 0]) + user_b \
        + struct.pack("<qQQQQ", ts, vsol, vtok, 0, 0) + b"\0" * 40
    return base64.b64encode(raw).decode()


def test_rpc_log_trade_decoding():
    from sniper import rpcws
    mint_b, user_b = bytes(range(1, 33)), bytes(range(33, 65))
    data = _trade_event(mint_b, user_b, 1_500_000_000, 2_000_000_000_000, True, 31_500_000_000, 1_020_000_000_000_000)
    note = {"method": "logsNotification", "params": {"result": {"context": {"slot": 777}, "value": {
        "signature": "SIG", "err": None, "logs": ["Program log: Instruction: Buy", "Program data: " + data,
                                                  "Program data: AAAA"]}}}}
    msgs = rpcws.parse_notification(note)
    assert len(msgs) == 1
    m = msgs[0]
    assert m["mint"] == rpcws.b58(mint_b) and m["traderPublicKey"] == rpcws.b58(user_b)
    assert m["txType"] == "buy" and m["solAmount"] == 1.5 and m["tokenAmount"] == 2_000_000 and m["slot"] == 777
    assert abs(m["marketCapSol"] - 31.5 / 1_020_000_000 * 1e9) < 1e-6
    note["params"]["result"]["value"]["err"] = {"InstructionError": [0, "x"]}
    assert rpcws.parse_notification(note) == []
    assert rpcws.b58(b"\0\0\x01") == "112"


def test_rpc_stream_feeds_engine_and_bonk_is_marked(conn):
    from sniper import rpcws
    c = cfg()
    eng = Engine(c, conn)
    eng.stream_pools = {"pump"}
    cv = Curve("MINTRPC")
    eng.handle(cv.create(), T0)
    for i in range(30):
        m = cv.buy(f"buyer{i:03d}", 0.8)
        ev = {"mint": "MINTRPC", "sol_lamports": int(m["solAmount"] * 1e9), "token_raw": int(m["tokenAmount"] * 1e6),
              "is_buy": True, "user": m["traderPublicKey"], "ts": 0, "v_sol_lamports": int(m["vSolInBondingCurve"] * 1e9),
              "v_tok_raw": int(m["vTokensInBondingCurve"] * 1e6)}
        eng.handle(rpcws.to_stream_msg(ev, m["signature"], 100 + i), T0 + 1 + i * 0.5)
    bonk = {"signature": "b", "traderPublicKey": "HE8h", "txType": "create", "mint": "BONKMINT", "tokensInPool": 0.14,
            "initialBuy": 999999999.85, "solAmount": 0, "newTokenBalance": 0.0043, "marketCapSol": 6036.3, "pool": "bonk"}
    st_b = eng.handle(bonk, T0)
    assert st_b.dev_tokens == 0.0
    cands = eng.tick(T0 + c.decide_sec + 1)
    assert [s.mint for s in cands] == ["MINTRPC"]
    assert eng.launches["MINTRPC"].n_seen == 30
    r = conn.execute("SELECT skip_reason, dev_buy_pct FROM sniper_launches WHERE mint='BONKMINT'").fetchone()
    assert r["skip_reason"] == "no_stream" and r["dev_buy_pct"] == 0


def _create_event(name, symbol, uri, mint_b, curve_b, user_b, extra=True):
    import base64
    import struct
    from sniper import rpcws
    raw = rpcws.CREATE_DISC
    for t in (name, symbol, uri):
        b = t.encode()
        raw += struct.pack("<I", len(b)) + b
    raw += mint_b + curve_b + user_b
    if extra:
        raw += user_b + struct.pack("<qQQQQ", 1_760_000_000, 1_073_000_000_000_000, 30_000_000_000, 793_100_000_000_000,
                                    1_000_000_000_000_000)
    return base64.b64encode(raw).decode()


def test_rpc_create_stream_and_dedupe(tmp_path):
    from sniper import rpcws
    from sniper.service import Service
    mint_b, curve_b, user_b = bytes([7]) * 32, bytes([8]) * 32, bytes([9]) * 32
    dev_buy = _trade_event(mint_b, user_b, 1_000_000_000, 34_000_000_000_000, True, 31_000_000_000, 1_039_000_000_000_000)
    note = {"method": "logsNotification", "params": {"result": {"context": {"slot": 5}, "value": {
        "signature": "CSIG", "err": None, "logs": ["Program log: Instruction: Create",
                                                   "Program data: " + _create_event("Moon", "MOON", "https://m/x.json",
                                                                                    mint_b, curve_b, user_b),
                                                   "Program data: " + dev_buy]}}}}
    msgs = rpcws.parse_creates(note)
    assert len(msgs) == 1
    c = msgs[0]
    assert c["mint"] == rpcws.b58(mint_b) and c["traderPublicKey"] == rpcws.b58(user_b) and c["symbol"] == "MOON"
    assert c["initialBuy"] == 34_000_000 and c["solAmount"] == 1.0 and c["vSolInBondingCurve"] == 31.0
    # old layout without creator/reserves still parses
    old = dict(note)
    old["params"] = {"result": {"context": {}, "value": {"signature": "s", "err": None, "logs": [
        "Program data: " + _create_event("A", "A", "u", mint_b, curve_b, user_b, extra=False)]}}}
    assert rpcws.parse_creates(old)[0]["vSolInBondingCurve"] == 30.0
    svc = Service(cfg(), db_path=str(tmp_path / "s.db"))
    svc._on_create(c, "rpc")
    pp = dict(c, src=None)
    svc._on_create(pp, "pumpportal")  # same mint later from PumpPortal: counted as seen, not as a new launch
    assert svc.engine.counters["creates"] == 1
    r = svc.launch_rates()
    assert r["rpc"]["first_per_min"] == 0.2 and r["pumpportal"]["per_min"] == 0.2 and r["pumpportal"]["first_per_min"] == 0
    svc.creates.connected = True
    svc.heartbeat()
    hb = json.loads(store.get_state(svc.conn, "service_status"))
    assert hb["active_launch_source"] == "rpc" and hb["launches_per_min"] == 0.2
    assert set(hb["launch_sources"]) == {"pumpportal", "rpc"}
    assert svc.creates.want == {rpcws.PUMP_MINT_AUTHORITY}
