"""market/ on fixtures (no network): parsers of recorded DexScreener/CoinGecko answers, sector and narrative
detection, a full analysis round on a synthetic DB, point-in-time reads, consumer switches, evaluation, service."""
import json
import os
import random
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pytest  # noqa: E402

from core import db  # noqa: E402
from market import analysis, context, evaluate, narratives, service, sources, store  # noqa: E402
from market.config import MarketSettings  # noqa: E402
from market.view import view  # noqa: E402

FX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
NOW = 1_792_152_000


def fx(name):
    with open(os.path.join(FX, name)) as f:
        return json.load(f)


def cfg(**kw):
    c = MarketSettings()
    for k, v in kw.items():
        setattr(c, k, v)
    return c


# ---------------------------------------------------------------- parsers
def test_parse_pairs_best_per_mint_and_solana_only():
    rows = sources.best_pairs(fx("dexscreener_tokens.json"))
    assert set(rows) == {"MintTesla1111111111111111111111111111111pump", "MintCat22222222222222222222222222222222222"}
    t = rows["MintTesla1111111111111111111111111111111pump"]
    assert t["pool"] == "poolA"  # highest liquidity wins
    assert (t["buys_h1"], t["sells_h1"], t["volume_h1"]) == (610, 420, 98000.25)
    assert t["n_socials"] == 3 and t["boosted"] == 10 and t["pair_created_at"] == 1792140000
    nf = analysis.net_flow(t)
    assert nf == pytest.approx(98000.25 * 190 / 1030)
    assert analysis.net_flow(rows["MintCat22222222222222222222222222222222222"]) < 0


def test_parse_promoted_and_coingecko():
    p = sources.parse_promoted(fx("dexscreener_boosts.json"), "boost")
    assert set(p) == {"MintTesla1111111111111111111111111111111pump", "MintGrok333333333333333333333333333333333"}
    assert "roadster" in p["MintTesla1111111111111111111111111111111pump"]["description"]
    assert p["MintTesla1111111111111111111111111111111pump"]["boosted"] == 30
    pts = sources.parse_market_chart(fx("coingecko_market_chart.json"))
    assert len(pts) == 10  # two points in the same hour collapse to one
    assert pts[-1] == (1792152000, 188.5)
    m = sources.parse_categories(fx("coingecko_categories.json"))
    assert m["id"] == "solana-meme-coins" and m["change_24h"] == 6.4
    assert sources.parse_categories([]) is None and sources.parse_market_chart(None) == []


# ---------------------------------------------------------------- narratives
@pytest.mark.parametrize("name,symbol,desc,want", [
    ("Flying Tesla Roadster", "FTR", "", "celeb"),
    ("depressed cat", "depcat", "", "cat"),
    ("Grok Agent", "GROKLET", "", "ai"),
    ("United States Water Reserve", "USWR", "", "politics"),
    ("SUBZERONE", "SUB-01", "", "other"),
    ("Mystery", "MYS", "the first sentient AI agent", "ai"),
    ("TOILETJAK", "TOILETJAK", "", "culture"),
    ("Pepe the Frog", "PEPE", "", "frog"),
])
def test_classify(name, symbol, desc, want):
    assert narratives.classify(name, symbol, desc)[0] == want


def test_terms_and_emergent():
    assert {"flying", "tesla", "roadster", "flying tesla", "tesla roadster", "ftr"} <= narratives.terms_of("Flying Tesla Roadster", "FTR")
    assert "the" not in narratives.terms_of("The Coin", "COIN")
    rnd = random.Random(1)
    filler = [f"Thing{i} {rnd.choice(['alpha', 'beta', 'gamma', 'delta'])}{i}" for i in range(400)]
    base = [(f"b{i}", n, "") for i, n in enumerate(filler)]
    recent = [(f"r{i}", f"Tesla Roadster {i}", "") for i in range(30)] + [(f"x{i}", filler[i], "") for i in range(70)]
    outcomes = [("Tesla Roadster", "", 1 if i < 6 else 0, 0) for i in range(20)] + \
               [(filler[i], "", 1 if i % 10 == 0 else 0, 0) for i in range(200)]
    rows = narratives.emergent_terms(recent, base + recent, outcomes, {"tesla roadster": 50000}, min_count=8)
    assert rows and rows[0]["term"] == "tesla roadster"
    top = rows[0]
    assert top["lift"] > 2 and top["n_recent"] == 30 and top["hit2x"] > 0.1 and len(top["mints"]) == 8
    assert not any(r["term"] in ("tesla", "roadster") for r in rows)  # live inside the bigram


# ---------------------------------------------------------------- synthetic DB
def make_db(risk="on"):
    """Universe of 60 coins, tiered wallets buying (risk on) or selling (risk off), 30 hours of launches."""
    path = os.path.join(tempfile.mkdtemp(), "m.db")
    conn = store.connect(path)
    from sniper import store as sstore
    sstore.ensure_schema(conn)
    rnd = random.Random(7)
    names = ["Flying Tesla Roadster", "depressed cat", "Grok Agent", "Doge Rocket", "Pepe Frog", "Trump Reserve",
             "Random Thing", "Anime Chan"]
    up = risk == "on"
    toks = []
    for i in range(60):
        nm = names[i % len(names)] + f" {i}"
        chg = rnd.uniform(5, 60) if (up and i % 4) or (not up and i % 4 == 0) else rnd.uniform(-50, -5)
        toks.append({"mint": f"M{i:03d}", "symbol": f"S{i}", "name": nm, "mcap_usd": rnd.choice([5e4, 5e5, 5e6, 5e7]),
                     "liquidity_usd": 50000, "pair_created_at": NOW - rnd.choice([1800, 4 * 3600, 20 * 3600, 3 * 86400, 30 * 86400]),
                     "price_change_h1": chg, "price_change_h24": chg * 2, "volume_h1": rnd.uniform(1e4, 2e5),
                     "volume_h24": 1e6, "buys_h1": 300 if chg > 0 else 150, "sells_h1": 150 if chg > 0 else 300,
                     "n_socials": 2, "boosted": 0, "dex": "pumpswap", "source": "dexscreener", "description": "",
                     "updated_at": NOW - 60, "first_seen": NOW - 86400, "sectors_json": None, "terms_json": None})
    store.save_tokens(conn, toks)
    conn.executemany("INSERT INTO wallets(address, score, tier, is_bot) VALUES (?,?,?,0)",
                     [(f"W{k}", 80, ["ultra", "smart", "good"][k % 3]) for k in range(9)])
    trades = []
    for k in range(9):
        for j in range(3):
            m = f"M{(k * 3 + j) % 60:03d}"
            trades.append((f"sig{k}{j}", f"W{k}", m, "buy" if up else "sell", NOW - 600 - j, 0, 0, 4000.0, 0.001, "test"))
    conn.executemany("INSERT INTO trades(sig, wallet, mint, side, ts, token_amount, sol_amount, usd_amount, price_usd, source) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?)", trades)
    rows = []
    for h in range(30):
        for i in range(40 if h else 60):
            ts = NOW - h * 3600 - rnd.randint(0, 3599)
            nm = "Tesla Roadster" if (h <= 2 and i % 2 == 0) else names[i % len(names)]
            hit = rnd.random() < (0.15 if up else 0.03)
            rows.append((f"L{h}_{i}", "pumpportal", nm + f" {i}", f"T{i}", ts, ts + 20, 1, "skip" if i % 5 else "enter",
                         50.0, 1.5 if hit else -0.5, 0 if up or rnd.random() > 0.3 else 1, int(hit)))
    conn.executemany("INSERT INTO sniper_launches(mint, source, name, symbol, created_ts, decided_ts, stage, decision, score, "
                     "max_ret_60m, rugged, migrated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    store.save_sol(conn, [(NOW - h * 3600, 180 * (1.004 if up else 0.996) ** (-h)) for h in range(0, 24 * 8)])
    conn.commit()
    return conn


def test_round_risk_on_vs_off():
    on = make_db("on")
    snap, secs, terms, bks = analysis.compute(on, NOW, {"meme": {"change_24h": 8.0, "market_cap": 1e10}})
    off = make_db("off")
    snap2, *_ = analysis.compute(off, NOW, {"meme": {"change_24h": -9.0, "market_cap": 1e10}})
    assert snap["score"] > 60 > 40 > snap2["score"]
    assert snap["label"] in ("risk_on", "hot") and snap2["label"] == "risk_off"
    assert snap["coverage"] > 0.8 and snap["universe_n"] == 60
    assert snap["smart_net_usd"] == pytest.approx(9 * 3 * 4000) and snap["smart_buyers"] == 9
    assert snap["launch_hit2x"] > snap2["launch_hit2x"]
    assert snap["top_term"] == "tesla roadster"
    assert {s["sector"] for s in secs} >= {"celeb", "cat", "ai"}
    assert all(s["heat"] is None or 0 <= s["heat"] <= 100 for s in secs)
    assert sum(s["vol_share"] for s in secs) == pytest.approx(1.0, abs=1e-3)
    assert {(b["kind"], b["bucket"]) for b in bks} >= {("mcap", "<100k"), ("age", "<1h"), ("age", ">7d")}
    comp = json.loads(snap["components_json"])
    assert sum(c["points"] for c in comp.values()) == pytest.approx(snap["score"], abs=0.2)


def test_point_in_time_and_consumers():
    conn = make_db("off")
    snap, secs, terms, bks = analysis.compute(conn, NOW, {"meme": {"change_24h": -9.0}})
    store.save_snapshot(conn, snap, secs, terms, bks)
    assert context.phase(conn, NOW - 1) is None          # nothing before the first snapshot
    assert context.phase(conn, NOW + 60)["label"] == "risk_off"
    assert context.phase(conn, NOW + 99999, max_age=1800) is None
    c = context.coin_context(conn, "M000", NOW + 30)
    assert c["sector"] == "celeb" and c["phase"]["label"] == "risk_off" and c["fresh"]
    assert c["rs_sector_h1"] is not None and c["terms"]
    f = context.features(conn, "M000", NOW + 30)
    assert set(f) == set(context.FEATURES) and f["mkt_phase"] == snap["score"]
    # switches are off by default: no effect at all
    assert context.signal_factor(conn, "M000", NOW + 30) is None
    assert context.sniper_adjust(conn, "Tesla Roadster", "TR", NOW + 30)["delta"] == 0
    f = context.signal_factor(conn, "M000", NOW + 30, cfg=cfg(signal_weight=True))
    assert 0.85 <= f["factor"] < 1.0 and "Risk-off" in f["why"]
    a = context.sniper_adjust(conn, "Some Dog", "DOG", NOW + 30, cfg=cfg(sniper_gate=True))
    assert a["delta"] == 10 and "Risk-off" in a["why"]
    assert context.sniper_adjust(conn, "x", "y", NOW + 99999, cfg=cfg(sniper_gate=True))["delta"] == 0  # stale


def test_signals_apply_market_off_and_on(monkeypatch):
    from signals import engine
    conn = make_db("on")
    snap, secs, terms, bks = analysis.compute(conn, NOW, {"meme": {"change_24h": 8.0}})
    store.save_snapshot(conn, snap, secs, terms, bks)
    m = {"score": 60.0, "contributions": {"money": {"points": 60.0}}}
    assert engine.apply_market(conn, "M001", m, now=NOW + 10) == m      # MARKET_SIGNAL_WEIGHT off
    import market.config
    monkeypatch.setattr(market.config.settings, "signal_weight", True)
    out = engine.apply_market(conn, "M001", m, now=NOW + 10)
    assert out["score"] > 60 and out["market"]["factor"] > 1
    assert sum(c["points"] for c in out["contributions"].values()) == pytest.approx(out["score"], abs=0.11)


def test_backfill_evaluate_and_view(monkeypatch):
    conn = make_db("on")
    monkeypatch.setattr(sources, "apply_budgets", lambda c: None)
    s = service.Service(cfg(), os.path.join(tempfile.mkdtemp(), "unused.db"))
    s.conn = conn
    n = s.backfill(hours=24, now=NOW)
    assert n >= 20
    hist = conn.execute("SELECT COUNT(*), MIN(backfilled) FROM market_snapshots").fetchone()
    assert hist[0] == n and hist[1] == 1
    assert s.backfill(hours=24, now=NOW) == 0  # idempotent
    ev = evaluate.evaluate(conn, days=2, now=NOW)
    la = ev["launches"]
    assert la["n"] > 500 and la["by_phase"] and la["by_term"]
    assert "gate" in la and la["gate"]["candidates"] > 0
    assert set(ev["significant"]) == {"by_phase", "by_sector_heat", "by_term", "by_term_stage"}
    # one live round with the network replaced by fixtures
    monkeypatch.setattr(sources, "promoted", lambda: (sources.parse_promoted(fx("dexscreener_boosts.json"), "boost"), 3))
    monkeypatch.setattr(sources, "pairs_for", lambda mints: (sources.best_pairs(fx("dexscreener_tokens.json")), 1))
    monkeypatch.setattr(sources, "sol_history", lambda days=7: sources.parse_market_chart(fx("coingecko_market_chart.json")))
    monkeypatch.setattr(sources, "meme_sector", lambda: sources.parse_categories(fx("coingecko_categories.json")))
    monkeypatch.setattr(sources, "sol_now", lambda: 188.0)
    snap = s.round(now=NOW + 300)
    assert snap["meme_mcap_chg_24h"] == 6.4 and snap["backfilled"] == 0
    row = conn.execute("SELECT * FROM market_tokens WHERE mint='MintTesla1111111111111111111111111111111pump'").fetchone()
    assert json.loads(row["sectors_json"])["primary"] == "celeb" and "Mars" in row["description"]
    hb = store.get_state(conn, "service_status")
    assert hb["ts"] == NOW + 300 and hb["calls"]["dexscreener"] == 4 and hb["calls"]["coingecko"] == 2
    v = view(conn, now=NOW + 400)
    assert v["available"] and v["phase"]["components"] and v["sectors"] and v["history"]
    assert v["switches"] == {"signal_weight": False, "sniper_gate": False, "signal_max_adj": 0.15,
                             "sniper_riskoff_bump": 10.0, "sniper_hot_term_bonus": 5.0, "learned_weights": False}
    json.dumps(v, default=str)
    assert context.summary(conn, NOW + 400)["label"] == snap["label"]


def test_view_without_tables():
    conn = db.connect(os.path.join(tempfile.mkdtemp(), "e.db"))
    assert view(conn)["available"] is False
    assert context.phase(conn) is None and context.summary(conn) is None
    assert evaluate.evaluate(conn)["snapshots"] == 0
    assert context.coin_context(conn, "X")["phase"] is None


def test_sniper_market_gate(monkeypatch):
    from types import SimpleNamespace
    from sniper import engine as seng
    from sniper.config import SniperSettings
    conn = make_db("off")
    snap, secs, terms, bks = analysis.compute(conn, NOW, {"meme": {"change_24h": -9.0}})
    store.save_snapshot(conn, snap, secs, terms, bks)
    sc = SniperSettings()
    st = SimpleNamespace(name="Some Dog", symbol="DOG", mint="MintX")
    a = SimpleNamespace(score=sc.min_score + 5, reasons=[], positives=[])
    assert seng.market_gate(conn, st, a, None, sc, NOW + 10) is None              # gate off: unchanged
    import market.config
    monkeypatch.setattr(market.config.settings, "sniper_gate", True)
    assert seng.market_gate(conn, st, a, None, sc, NOW + 10) == "market"         # risk-off: +10 needed
    assert a.reasons and a.reasons[0]["check"] == "market"
    assert seng.market_gate(conn, st, a, "bundle", sc, NOW + 10) == "bundle"     # never overrides other findings
    a2 = SimpleNamespace(score=sc.min_score + 15, reasons=[], positives=[])
    assert seng.market_gate(conn, st, a2, None, sc, NOW + 10) is None


def test_web_market_endpoints(monkeypatch):
    conn = make_db("on")
    snap, secs, terms, bks = analysis.compute(conn, NOW, {})
    store.save_snapshot(conn, snap, secs, terms, bks)
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    conn.close()
    monkeypatch.setenv("MAYHAM_DB_PATH", path)
    monkeypatch.delenv("WEB_PASSWORD", raising=False)
    from web import db as wdb
    from web.server import app
    from web.tests.asgi import Client
    wdb.cache.clear()
    c = Client(app, client=("127.0.0.1", 5000))
    r = c.get("/api/market")
    assert r.status_code == 200 and r.json()["available"] and r.json()["sectors"]
    r = c.get("/api/market/coin/M000")
    assert r.status_code == 200 and r.json()["sector"] == "celeb"
    wdb.cache.clear()


def test_walk_forward_confirms_only_out_of_sample():
    rnd = random.Random(3)
    hit = lambda r: (r["max_ret_60m"] or 0) >= 1.0  # noqa: E731
    rows = []
    for i in range(800):
        lab = "risk_on" if i % 2 else "risk_off"
        p = 0.25 if lab == "risk_on" else 0.05
        rows.append({"decided_ts": i, "created_ts": i, "label": lab, "sector_heat": None, "term_heat": None,
                     "max_ret_60m": 1.5 if rnd.random() < p else -0.5})
    wf = evaluate.walk_forward(rows, hit)
    assert wf["by_phase"]["ok"] and wf["by_phase"]["best"] == "Risk-on"
    assert not wf["by_sector_heat"]["ok"]
    # same rates everywhere: nothing confirmed
    flat = [dict(r, max_ret_60m=1.5 if rnd.random() < 0.1 else -0.5) for r in rows]
    assert not evaluate.walk_forward(flat, hit)["by_phase"]["ok"]


# ---------------------------------------------------------------- dynamics
def test_regime_hysteresis_slope_and_transitions():
    from market import dynamics
    c = cfg()
    assert dynamics.label_hyst("neutral", 56, c) == "neutral"      # 1 point over 55: stays
    assert dynamics.label_hyst("neutral", 59, c) == "risk_on"
    assert dynamics.label_hyst("risk_on", 53, c) == "risk_on"      # 2 below: stays
    assert dynamics.label_hyst("risk_on", 50, c) == "neutral"
    pts = [(i * 300, 40 + i) for i in range(13)]                    # +12 points per hour
    assert dynamics.slope(pts, 1) == pytest.approx(12, abs=0.01)
    conn = store.connect(os.path.join(tempfile.mkdtemp(), "r.db"))
    rows = []
    for h in range(48):  # alternating 6 h risk_off / 6 h risk_on, hourly
        sc = 20 if (h // 6) % 2 == 0 else 70
        rows.append({"ts": NOW - (48 - h) * 3600, "score": sc, "label": "risk_off" if sc == 20 else "risk_on",
                     "components_json": "{}"})
    store._insert(conn, "market_snapshots", rows)
    r = dynamics.regime(conn, NOW, 72, c)
    assert r["regime"] == "risk_on" and r["regime_since"] < NOW and r["trend"] in ("stabil", "steigt")
    r2 = dynamics.regime(conn, NOW, 30, c)
    assert r2["regime"] == "risk_off" and r2["regime_since"] == NOW and r2["slope_1h"] is None and r2["slope_3h"] is not None
    tr = dynamics.transitions(conn, NOW)
    assert tr["n"] == 47 and tr["p"]["risk_off"]["risk_off"] > 0.6 and tr["p"]["risk_on"]["risk_off"] > 0.1


def test_term_lifecycle():
    from market import dynamics
    t = {"n_recent": 30}
    assert dynamics.lifecycle(t, [], NOW)["stage"] == "new"
    hist = [(NOW - 10800, 10), (NOW - 3600, 20)]
    assert dynamics.lifecycle(t, hist, NOW)["stage"] == "growing"
    assert dynamics.lifecycle({"n_recent": 10}, [(NOW - 10800, 40), (NOW - 3600, 30)], NOW)["stage"] == "fading"
    lc = dynamics.lifecycle({"n_recent": 39}, [(NOW - 10800, 40), (NOW - 3600, 38)], NOW)
    assert lc["stage"] == "peak" and lc["peak_n"] == 40 and lc["age_h"] == 3.0


def test_sector_lead_finds_a_real_lead_only():
    from market import dynamics
    conn = store.connect(os.path.join(tempfile.mkdtemp(), "s.db"))
    rnd = random.Random(5)
    rows, chg = [], {}
    sectors_ = ["ai", "dog", "cat", "frog", "celeb", "politics", "anime"]
    for h in range(60):
        ts = NOW - (60 - h) * 3600
        for s in sectors_:
            prev = chg.get(s, 0.0)
            new = rnd.uniform(-0.05, 0.05)
            rows.append({"ts": ts, "sector": s, "vol_share_chg": new, "heat": rnd.uniform(0, 100), "smart_net_usd": 0.0,
                         "net_flow_usd": None, "median_ret_h1": 200 * prev + rnd.uniform(-1, 1)})  # last hour's share growth leads
            chg[s] = new
    store._insert(conn, "market_sectors", rows)
    lead = dynamics.sector_lead(conn, NOW)
    assert lead["vol_share_chg"]["confirmed"] and lead["vol_share_chg"]["ic"] > 0.5
    assert not lead["heat"]["confirmed"]


def test_component_power_and_learned_weights():
    from market import dynamics
    conn = make_db("on")
    rnd = random.Random(9)
    snaps = []
    conn.execute("DELETE FROM sniper_launches")
    launches = []
    for h in range(60):
        ts = NOW - (60 - h) * 3600
        good = rnd.random()
        comps = {"launch_hit": {"value": good, "weight": 0.5}, "sol_trend": {"value": rnd.random(), "weight": 0.5}}
        snaps.append({"ts": ts, "score": 50.0, "label": "neutral", "components_json": json.dumps(comps)})
        for i in range(40):  # launches in the next hour: 2x rate follows launch_hit, not sol_trend
            launches.append((f"P{h}_{i}", "x", "x", ts + 60 + i * 60, 1.5 if rnd.random() < 0.05 + 0.3 * good else -0.5))
    store._insert(conn, "market_snapshots", snaps)
    conn.executemany("INSERT INTO sniper_launches(mint, name, symbol, created_ts, max_ret_60m) VALUES (?,?,?,?,?)", launches)
    conn.commit()
    p = dynamics.component_power(conn, NOW)
    assert p["n_hours"] >= 50
    assert p["rows"]["launch_hit"]["confirmed"] and p["rows"]["launch_hit"]["ic"] > 0.4
    assert not p["rows"]["sol_trend"]["confirmed"]
    lw = p["learned"]
    assert lw and lw["weights"]["launch_hit"] > 0.14 and lw["n_new"] >= 25


def test_service_dynamics_state(monkeypatch):
    conn = make_db("on")
    monkeypatch.setattr(sources, "apply_budgets", lambda c: None)
    s = service.Service(cfg(), os.path.join(tempfile.mkdtemp(), "unused.db"))
    s.conn = conn
    s.backfill(hours=24, now=NOW)
    d = s.dynamics(NOW)
    assert d and set(d) == {"ts", "transitions", "sector_lead", "components"}
    assert store.get_state(conn, "dynamics")["ts"] == NOW
    snap, secs, terms, bks = analysis.compute(conn, NOW + 300, {})
    assert snap["regime"] and snap["trend"] and all(t.get("stage") for t in terms)
    store.save_snapshot(conn, snap, secs, terms, bks)
    v = view(conn, now=NOW + 400)
    assert v["dynamics"]["ts"] == NOW and "streak" in v["sectors"][0]
    f = context.features(conn, "M000", NOW + 300)
    assert set(f) == set(context.FEATURES) and f["mkt_term_early"] in (0, 1)


def test_coin_context_never_uses_a_coin_row_from_the_future():
    conn = make_db("off")
    snap, secs, terms, bks = analysis.compute(conn, NOW, {"meme": {"change_24h": -9.0}})
    store.save_snapshot(conn, snap, secs, terms, bks)
    now = context.coin_context(conn, "M000", NOW + 30)
    assert now["fresh"] and now["rs_sector_h1"] is not None
    # the coin row gets rewritten 10 min after t: for t it is the future and must not count
    tbl = "market_tokens" if store.has(conn, "market_tokens") and conn.execute(
        "SELECT 1 FROM market_tokens WHERE mint='M000'").fetchone() else "tokens"
    col = "updated_at" if tbl == "market_tokens" else "last_updated"
    conn.execute(f"UPDATE {tbl} SET {col}=? WHERE mint='M000'", (NOW + 630,))
    early = context.coin_context(conn, "M000", NOW + 30)
    assert early["phase"] is not None and early["sector_heat"] is not None   # snapshot at or before t still counts
    assert not early["fresh"] and early.get("rs_sector_h1") is None
    assert early["net_flow_usd"] is None and early["liq_mcap"] is None
    assert context.features(conn, "M000", NOW + 30)["mkt_rs_sector_h1"] is None


def test_market_tokens_index_on_existing_db():
    conn = make_db("off")
    conn.execute("DROP INDEX IF EXISTS idx_market_tokens_updated")
    store.ensure_schema(conn)
    plan = " ".join(str(tuple(r)) for r in conn.execute(
        "EXPLAIN QUERY PLAN SELECT mint FROM market_tokens WHERE updated_at<?", (NOW,)))
    assert "idx_market_tokens_updated" in plan
