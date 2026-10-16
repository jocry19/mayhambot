"""intel/ on a synthetic DB (no network): point-in-time features, entities, events, service round, view,
outcome labels and the per-feature evaluation."""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pytest  # noqa: E402

import intel  # noqa: E402
from intel import base, coin, evaluate, events, service, store, wallet  # noqa: E402
from intel import panel as iview  # noqa: E402

NOW = 1_792_152_000
MINT, OTHER = "MintAAAA1111111111111111111111111111111pump", "MintBBBB2222222222222222222222222222222pump"
U1, U2, S1, S2, R = "Ultra1", "Ultra2", "Smart1", "Smart2", "Retail"


@pytest.fixture()
def conn():
    d = tempfile.mkdtemp()
    c = store.connect(os.path.join(d, "t.db"))
    base.clear_cache()
    c.executemany("INSERT INTO wallets(address, tier) VALUES (?,?)",
                  [(U1, "ultra"), (U2, "ultra"), (S1, "smart"), (S2, "smart")])
    c.execute("UPDATE wallets SET cluster_id='grp' WHERE address IN (?,?)", (S1, S2))  # one entity
    c.executemany("INSERT INTO tokens(mint, symbol, pair_created_at, tracked, last_updated) VALUES (?,?,?,1,?)",
                  [(MINT, "AAA", NOW - 7200, NOW), (OTHER, "BBB", NOW - 86400, NOW)])
    from market import store as mstore
    from safety import store as sfstore
    from sniper import store as snstore
    mstore.ensure_schema(c)
    sfstore.ensure_schema(c)
    c.executescript(snstore.SCHEMA)
    c.execute("INSERT INTO market_sol(ts, price_usd) VALUES (?, 150)", (NOW - 600,))
    yield c
    c.close()


def trade(c, sig, w, m, side, ts, usd, price=1.0, tok=None):
    c.execute("INSERT INTO trades(sig, wallet, mint, side, ts, token_amount, sol_amount, usd_amount, price_usd, source) "
              "VALUES (?,?,?,?,?,?,?,?,?, 'rpc')", (sig, w, m, side, ts, tok if tok is not None else usd / price, usd / 150, usd, price))


def prices(c, m, pts):
    c.executemany("INSERT INTO token_prices(mint, ts, price_usd, mcap_usd, liquidity_usd) VALUES (?,?,?,?,?)",
                  [(m, t, p, p * 1e6, liq) for t, p, liq in pts])


def test_point_in_time_and_entities(conn):
    trade(conn, "a", U1, MINT, "buy", NOW - 600, 300)
    trade(conn, "b", S1, MINT, "buy", NOW - 500, 200)
    trade(conn, "c", S2, MINT, "buy", NOW - 400, 200)  # same cluster as S1: one person
    for i in range(10):
        trade(conn, f"r{i}", f"{R}{i}", MINT, "buy", NOW - 300 + i, 50)
    trade(conn, "late", U2, MINT, "buy", NOW + 60, 5000)  # after ts: must not count
    conn.commit()
    f = coin.coin_features(conn, MINT, NOW)["features"]
    assert f["vol_trades_60m"] == 13
    assert f["flow_smart_entities_60m"] == 2  # U1 + cluster(S1,S2)
    assert f["flow_smart_net_usd_60m"] == pytest.approx(700)
    assert f["vol_entities_60m"] == 12
    assert f["flow_retail_buyers_60m"] == 10
    assert f["flow_buy_ratio_15m"] == 1.0
    assert f["hold_top10"] is None  # no rug check yet: unknown, not 0


def test_wash_and_insider_sells(conn):
    conn.execute("INSERT INTO sniper_launch_wallets(mint, wallet, role, ts) VALUES (?,?, 'bundle', ?)", (MINT, "Bund", NOW - 7000))
    for i in range(6):
        trade(conn, f"wb{i}", "Washer", MINT, "buy", NOW - 1000 + i * 10, 100)
        trade(conn, f"ws{i}", "Washer", MINT, "sell", NOW - 995 + i * 10, 100)
    trade(conn, "bs", "Bund", MINT, "sell", NOW - 100, 400)
    conn.commit()
    f = coin.coin_features(conn, MINT, NOW)["features"]
    assert f["vol_roundtrip_60m"] == pytest.approx(1200 / 1600)
    assert f["flow_insider_sell_usd_60m"] == pytest.approx(400)
    ev = events.detect(conn, NOW)
    assert any(e["kind"] == "insider_sell" and e["mint"] == MINT for e in ev)


def test_liquidity_price_exec(conn):
    prices(conn, MINT, [(NOW - 7200, 1.0, 100_000), (NOW - 3600, 1.2, 100_000), (NOW - 900, 1.5, 80_000), (NOW - 60, 1.4, 50_000)])
    conn.commit()
    f = coin.coin_features(conn, MINT, NOW)["features"]
    assert f["liq_usd"] == 50_000 and f["liq_peak_7d"] == 100_000
    assert f["liq_off_peak"] == pytest.approx(0.5)
    assert f["liq_chg_15m"] == pytest.approx(50_000 / 80_000 - 1)
    assert f["px_ret_60m"] == pytest.approx(1.4 / 1.2 - 1)
    assert f["px_off_high_24h"] == pytest.approx(1 - 1.4 / 1.5)
    assert f["px_age_h"] == pytest.approx(2.0)
    assert all(0 < f[k] < 0.2 for k in ("exec_rt_0_1", "exec_rt_0_25", "exec_rt_0_5"))
    assert f["exec_rt_0_1"] > f["exec_rt_0_5"]  # the fixed network fee weighs more on a small trade
    # the future never leaks into an earlier ts
    g = coin.coin_features(conn, MINT, NOW - 800)["features"]
    assert g["liq_usd"] == 80_000 and g["px_off_high_24h"] == 0


def test_events_smart_entry_and_rotation(conn):
    trade(conn, "s1", U1, OTHER, "sell", NOW - 800, 400)
    trade(conn, "b1", U1, MINT, "buy", NOW - 500, 380)
    trade(conn, "b2", S1, MINT, "buy", NOW - 400, 200)
    conn.commit()
    ev = events.detect(conn, NOW)
    kinds = {e["kind"] for e in ev}
    assert {"ultra_buy", "smart_entry", "rotation"} <= kinds
    rot = next(e for e in ev if e["kind"] == "rotation")
    assert rot["data"]["from"] == OTHER and rot["data"]["to"] == MINT
    assert store.add_events(conn, ev) == len(ev)
    assert store.add_events(conn, events.detect(conn, NOW + 30)) == 0  # dedup


def test_wallet_features(conn):
    trade(conn, "w1", U1, MINT, "buy", NOW - 5000, 100, price=1.0)
    trade(conn, "w2", U1, MINT, "sell", NOW - 4880, 150, price=1.5)  # 2 min: scalp
    trade(conn, "w3", U1, OTHER, "buy", NOW - 3000, 300, price=2.0)
    trade(conn, "w4", U1, OTHER, "sell", NOW - 1200, 300, price=2.0)  # 30 min
    trade(conn, "x1", R, OTHER, "buy", NOW - 3010, 50, price=2.0)  # co-buy within 60 s
    prices(conn, MINT, [(NOW - 4950, 2.0, 0)])
    conn.commit()
    f = wallet.wallet_features(conn, U1, NOW)["features"]
    assert f["w_n_coins_30d"] == 2 and f["w_n_closed"] == 2
    assert f["w_hold_median_min"] == pytest.approx((2 + 30) / 2)
    assert f["w_scalp_share"] == 0.5
    assert f["w_concentration"] == pytest.approx(0.75)
    assert f["w_cobuy_share"] == 0.5
    assert f["w_exit_capture"] == pytest.approx((0.75 + 1.0) / 2)  # sold at 1.5 with a 2.0 peak; sold at the top


def test_service_round_view_label_eval(conn, tmp_path):
    db = conn.execute("PRAGMA database_list").fetchone()[2]
    for i in range(3):
        trade(conn, f"u{i}", [U1, U2, S1][i], MINT, "buy", NOW - 300 + i, 200)
    prices(conn, MINT, [(NOW - 600, 1.0, 40_000), (NOW, 1.0, 40_000), (NOW + 1200, 1.8, 60_000), (NOW + 3500, 1.2, 50_000)])
    conn.execute("INSERT INTO safety_tokens(mint, ts, score, blocked, checks_json) VALUES (?,?,?,?,?)",
                 (MINT, NOW - 100, 20, 0, json.dumps({"holders": {"status": "ok", "data": {"top10_pct": 35.0, "top1_pct": 8.0}},
                                                      "wash": {"status": "unknown", "data": {"roundtrip_share": 0.9}}})))
    conn.commit()
    s = service.Service(db_path=db)
    out = s.round(NOW)
    assert out["coins"] >= 1 and out["holders"] == 1 and out["events"] >= 2
    lat = intel.latest(s.conn, "coin", MINT)
    assert lat["features"]["hold_top10"] == pytest.approx(0.35)
    assert lat["features"]["vol_roundtrip_60m"] is None  # too few trades to judge
    v = iview.view(s.conn, now=NOW)
    assert v["available"] and v["service"]["up"]
    assert v["coins"][0]["mint"] == MINT and v["coins"][0]["flow_smart_entities_60m"] == 3
    assert v["flows"]["inflow"][0]["mint"] == MINT
    assert any(e["kind"] == "smart_entry" for e in v["events"])
    cv = iview.coin_view(s.conn, MINT, now=NOW)
    assert cv["available"] and len(cv["history"]) == 1
    # one hour later the snapshot gets its outcome
    assert evaluate.label(s.conn, NOW + 4000) == 1
    row = s.conn.execute("SELECT max_ret_1h, min_ret_1h FROM intel_snapshots WHERE kind='coin' AND key=?", (MINT,)).fetchone()
    assert row[0] == pytest.approx(0.8) and row[1] == pytest.approx(0.2)
    ev = evaluate.evaluate(s.conn, now=NOW + 4000, min_n=1)
    assert ev["n"] == 1 and ev["base_rate"] == 1.0
    tab = intel.feature_table(s.conn, "coin", NOW - 10)
    assert (MINT, NOW, "flow_smart_entities_60m", 3) in tab
    assert intel.features_at("market", "solana", NOW, s.conn)["coverage"]["all"] == 0  # no market snapshot: unknown


def test_evaluation_separates_a_real_signal():
    xs = list(range(400))
    data = [({"flow_net_usd_15m": float(x)}, 0.8 if x % 5 == 0 or x > 320 else 0.1, -0.1) for x in xs]
    rows = {}
    import intel.evaluate as ev_mod
    orig = ev_mod.samples
    ev_mod.samples = lambda conn, days, now: data
    try:
        class C:
            def execute(self, *a):
                class R:
                    def fetchone(self_inner):
                        return (1,)
                return R()
        res = ev_mod.evaluate(C(), min_n=100)
    finally:
        ev_mod.samples = orig
    rows = {r["feature"]: r for r in res["features"]}
    r = rows["flow_net_usd_15m"]
    assert r["clear"] and r["z"] > 2 and r["ic"] > 0 and r["direction"] == "höher ist besser"
    assert rows["liq_usd"]["n"] == 0


def test_microstructure(conn):
    # steady buying that pushes the price up: positive OFI, positive lambda, one-sided flow = high VPIN
    p = 1.0
    for i in range(40):
        side = "buy" if i % 5 else "sell"
        usd = 500 if side == "buy" else 100
        p *= 1.004 if side == "buy" else 0.999
        trade(conn, f"m{i}", f"W{i}", MINT, side, NOW - 2400 + i * 60, usd, price=p)
    conn.commit()
    f = coin.coin_features(conn, MINT, NOW)["features"]
    assert f["micro_ofi_15m"] > 0.5
    assert f["micro_lambda"] > 0
    assert 0.5 < f["micro_vpin"] <= 1
    assert f["micro_absorption"] is not None and f["micro_absorption"] < f["micro_ofi_15m"]


def test_tape_structure_insiders_and_distribution(conn):
    conn.execute("INSERT INTO sniper_launch_wallets(mint, wallet, role, ts, tokens) VALUES (?, 'Dev', 'dev', ?, 1000)", (MINT, NOW - 7000))
    trade(conn, "d0", "Dev", MINT, "buy", NOW - 7000, 100, price=0.1, tok=1000)
    for i in range(20):
        trade(conn, f"e{i}", f"Early{i}", MINT, "buy", NOW - 6000 + i, 50, price=0.1, tok=500)
    trade(conn, "sm", S1, MINT, "buy", NOW - 5000, 50, price=0.1, tok=500)
    # in the last hour the dev and half the early wallets sell to newcomers
    trade(conn, "d1", "Dev", MINT, "sell", NOW - 600, 80, price=0.1, tok=800)
    for i in range(10):
        trade(conn, f"x{i}", f"Early{i}", MINT, "sell", NOW - 500 + i, 50, price=0.1, tok=500)
        trade(conn, f"n{i}", f"New{i}", MINT, "buy", NOW - 400 + i, 50, price=0.1, tok=500)
    conn.commit()
    f = coin.coin_features(conn, MINT, NOW)["features"]
    assert f["tape_insider_held"] == pytest.approx(0.2) and f["tape_insider_sold_1h"] == pytest.approx(0.8)
    assert f["tape_new_holders_1h"] == 10 and f["tape_holders_chg_1h"] == 0
    assert f["tape_distribution"] > 0.3
    assert f["tape_smart_held"] == pytest.approx(500 / f_total(conn))


def f_total(conn):
    return conn.execute("SELECT SUM(CASE side WHEN 'buy' THEN token_amount ELSE -token_amount END) FROM trades WHERE mint=?",
                        (MINT,)).fetchone()[0]


def test_network_dev_record_and_sandwich(conn):
    conn.execute("INSERT INTO sniper_launches(mint, creator, created_ts) VALUES (?, 'Rugger', ?)", (MINT, NOW - 600))
    for i in range(4):
        conn.execute("INSERT INTO sniper_launches(mint, creator, created_ts, rugged, max_ret_60m) VALUES (?, 'Rugger', ?, ?, ?)",
                     (f"Old{i}", NOW - 86400 + i, 1 if i < 3 else 0, 1.5 if i == 3 else -0.9))
    conn.execute("INSERT INTO sniper_launches(mint, creator, created_ts, rugged) VALUES ('Fresh', 'Rugger', ?, 1)", (NOW - 100,))
    conn.execute("INSERT INTO sniper_launch_wallets(mint, wallet, role) VALUES ('Old0', 'BadBundler', 'bundle')")
    conn.execute("INSERT INTO sniper_launch_wallets(mint, wallet, role, ts) VALUES (?, 'BadBundler', 'bundle', ?)", (MINT, NOW - 590))
    for i in range(10):
        trade(conn, f"q{i}", f"Q{i}", MINT, "buy", NOW - 1000 + 10 * i, 20)
    trade(conn, "sa", "MEV", MINT, "buy", NOW - 300, 500)
    trade(conn, "vb", "Victim", MINT, "buy", NOW - 300, 100)
    trade(conn, "sb", "MEV", MINT, "sell", NOW - 299, 510)
    conn.commit()
    f = coin.coin_features(conn, MINT, NOW)["features"]
    assert f["net_dev_launches"] == 4  # the launch from 100 s ago has no known outcome yet
    assert f["net_dev_rug_rate"] == pytest.approx(0.75) and f["net_dev_hit2x_rate"] == pytest.approx(0.25)
    assert f["net_early_bad"] == 1
    assert f["net_sandwich_60m"] >= 1 and f["net_bot_share_60m"] > 0.5


def test_jupiter_quotes_macro_attention(conn):
    from intel import config, sources
    sources.ensure(conn)

    def get(url, params=None):
        if "quote" in url:
            if params["inputMint"] == sources.SOL_MINT:
                return {"outAmount": str(params["amount"] * 1000), "priceImpactPct": "0.01", "routePlan": [{}]}
            return {"outAmount": str(int(params["amount"] / 1000 * 0.94)), "priceImpactPct": "0.02", "routePlan": [{}]}
        if "coingecko" in url:
            return {"bitcoin": {"usd": 60000, "usd_24h_change": -3.0}, "ethereum": {"usd": 3000, "usd_24h_change": 1.0},
                    "solana": {"usd": 150, "usd_24h_change": 2.0}}
        return {"data": [{"value": "22", "value_classification": "Extreme Fear"}]}
    store.save_features(conn, "coin", [{"key": MINT, "ts": NOW, "coverage": 1,
                                        "features": {"vol_usd_60m": 1.0, "flow_smart_entities_60m": 2}}], False)
    cfg = config.IntelSettings()
    cfg.quote_coins = 3
    assert sources.quote_round(conn, NOW, cfg, get) == 1  # smart-money coin: one size only
    f = coin.coin_features(conn, MINT, NOW + 60)["features"]
    assert f["exec_q_0_25"] == pytest.approx(0.06, abs=1e-6) and f["exec_q_noroute"] == 0
    assert coin.coin_features(conn, MINT, NOW - 60)["features"]["exec_q_0_25"] is None  # quote is from later
    assert sources.macro_round(conn, NOW, get) == 1
    m = intel.features_at("market", "solana", NOW + 10, conn)["features"]
    assert m["m_btc_chg_24h"] == pytest.approx(-0.03) and m["m_fng"] == 22
    conn.execute("INSERT INTO market_tokens(mint, boosted, n_socials, source, updated_at) VALUES (?, 50, 2, 'boost', ?)", (MINT, NOW))
    conn.commit()
    assert sources.save(conn, "intel_attention", sources.attention_rows(conn, NOW)) == 1
    assert sources.attention_rows(conn, NOW + 60) == []  # unchanged: no new row
    a = coin.coin_features(conn, MINT, NOW + 3700)["features"]
    assert a["att_boost"] == 50 and a["att_promoted_h"] == pytest.approx(3700 / 3600)


def test_liquidity_flash(conn):
    prices(conn, MINT, [(NOW - 3000, 1.0, 20_000), (NOW - 2000, 1.1, 60_000), (NOW - 600, 1.0, 21_000), (NOW - 60, 1.0, 20_500)])
    conn.commit()
    f = coin.coin_features(conn, MINT, NOW)["features"]
    assert f["liq_flash_60m"] == pytest.approx(2.0)


def test_deep_playbook_walk_forward_and_bh():
    from intel import deep
    import random
    rnd = random.Random(7)
    data = []
    for i in range(900):
        smart = rnd.random() < 0.3
        wash = rnd.random() < 0.3
        f = {"flow_smart_entities_60m": 3 if smart else 0, "vol_roundtrip_60m": 0.6 if wash else 0.05,
             "px_mcap": rnd.uniform(1e4, 1e6), "px_age_h": rnd.uniform(0, 48), "liq_usd": rnd.uniform(1e3, 1e5),
             "noise": rnd.random()}
        p = 0.05 + (0.35 if smart and not wash else 0) - (0.04 if wash else 0)
        mx = 0.8 if rnd.random() < p else 0.1
        mn = -0.7 if wash and rnd.random() < 0.5 else -0.1
        data.append((f, mx, mn))
    pb = deep.playbook(data)
    good = {tuple(s["setup"]) for s in pb["good"] if s["confirmed"]}
    bad = {tuple(s["setup"]) for s in pb["bad"] if s["confirmed"]}
    assert ("smart2", "clean_vol") in good or ("smart2",) in good
    assert ("wash",) in bad
    assert deep.matching({"flow_smart_entities_60m": 3, "vol_roundtrip_60m": 0.05}, pb)
    rob = {r["feature"]: r for r in deep.robustness(data)}
    assert rob["flow_smart_entities_60m"]["verdict"] == "robust"
    assert deep.bh([0.01, 0.04, 0.5]) == pytest.approx([0.03, 0.06, 0.5])


def test_event_study(conn):
    store.add_events(conn, [dict(ts=NOW - 7200 + i * 60, kind="smart_entry", mint=MINT, title="x", dedup=f"d{i}") for i in range(3)])
    prices(conn, MINT, [(NOW - 7300, 1.0, 1e4), (NOW - 6900, 1.2, 1e4), (NOW - 6300, 1.6, 1e4), (NOW - 3700, 1.1, 1e4)])
    conn.commit()
    from intel import deep
    ev = deep.event_study(conn, NOW, base_rate=0.1)
    r = ev[0]
    assert r["kind"] == "smart_entry" and r["n"] == 1  # three events within 30 min on one coin count once
    assert r["med_max"] == pytest.approx(0.6) and r["hit_rate"] == 1


def test_no_future_in_cobuys_and_tiers(conn):
    from research import store as rstore
    rstore.ensure_schema(conn)
    trade(conn, "a", U1, MINT, "buy", NOW - 30, 100)
    trade(conn, "b", R, MINT, "buy", NOW + 20, 100)  # co-buy after ts: invisible at ts
    conn.commit()
    assert wallet.wallet_features(conn, U1, NOW)["features"]["w_cobuy_share"] == 0
    assert wallet.wallet_features(conn, U1, NOW + 60)["features"]["w_cobuy_share"] == 1
    if store.has(conn, "model_versions"):
        conn.execute("INSERT INTO model_versions(id, ts, active) VALUES (1, ?, 0)", (NOW - 7200,))  # trained, never went live
        conn.execute("INSERT INTO model_versions(id, ts, active) VALUES (2, ?, 1)", (NOW - 7000,))
        conn.execute("INSERT INTO model_tier_history VALUES (2, 'Live', 'ultra', 80)")
        conn.commit()
        base.clear_cache()
        tiers, pit = base.tiers_at(conn, NOW)
        assert pit and tiers == {"Live": "ultra"}
        base.clear_cache()
        assert base.tiers_at(conn, NOW - 6000)[1] is False  # v2 was still training then


def test_network_features_on_demand(conn):
    conn.execute("INSERT INTO sniper_launches(mint, creator, created_ts) VALUES (?, 'D', ?)", (MINT, NOW - 30))
    conn.execute("INSERT INTO sniper_launches(mint, creator, created_ts, rugged, max_ret_60m) VALUES ('P', 'D', ?, 1, -0.9)", (NOW - 9000,))
    conn.commit()
    r = intel.network_features(conn, MINT, NOW)
    assert r["features"]["net_dev_launches"] == 1 and r["features"]["net_dev_rug_rate"] == 1.0 and r["meta"]["creator"] == "D"


def test_quote_round_spares_jupiter(conn):
    """Only what a later stage needs, each only when its last measurement is stale, and a throttle pauses quoting."""
    from intel import config, sources
    sources.ensure(conn)
    conn.execute("CREATE TABLE ct_positions (mint TEXT, status TEXT)")
    conn.execute("INSERT INTO ct_positions VALUES (?, 'open')", (MINT,))
    conn.execute("INSERT INTO signals(ts, mint) VALUES (?, ?)", (NOW - 600, OTHER))
    conn.commit()
    calls = []
    mode = {"fail": None}

    def get(url, params=None):
        calls.append(params["inputMint"])
        if mode["fail"]:
            raise RuntimeError(mode["fail"])
        if params["inputMint"] == sources.SOL_MINT:
            return {"outAmount": str(params["amount"] * 1000), "priceImpactPct": "0.01", "routePlan": [{}]}
        return {"outAmount": str(int(params["amount"] / 1000 * 0.95)), "priceImpactPct": "0.01", "routePlan": [{}]}
    cfg = config.IntelSettings()
    cfg.quote_max_calls = 12
    assert sources.quote_round(conn, NOW, cfg, get) == 4  # position: 3 sizes, signal: 0.25 SOL
    assert len(calls) == 8
    calls.clear()
    assert sources.quote_round(conn, NOW + 300, cfg, get) == 0 and calls == []  # still fresh
    assert sources.quote_round(conn, NOW + 700, cfg, get) == 3  # the position is due again, the candidate is not
    calls.clear()
    mode["fail"] = "429 for https://lite-api.jup.ag/swap/v1/quote"
    assert sources.quote_round(conn, NOW + 2000, cfg, get) == 0 and len(calls) == 1  # first throttle ends the round
    bo = store.get_state(conn, "quotes_backoff")
    assert bo["n"] == 1 and bo["until"] == NOW + 2000 + 2 * cfg.quotes_every_sec
    calls.clear()
    assert sources.quote_round(conn, NOW + 2100, cfg, get) == 0 and calls == []  # paused
    mode["fail"] = None
    assert sources.quote_round(conn, bo["until"], cfg, get) > 0
    assert store.get_state(conn, "quotes_backoff") == {}
    cfg.quote_max_calls = 3
    calls.clear()
    assert sources.quote_round(conn, NOW + 9000, cfg, get) == 1 and len(calls) == 2  # hard cap per round


def test_time_indexes(conn):
    assert "idx_trades_ts" in store.ensure_indexes(conn)
    assert store.ensure_indexes(conn) == []
