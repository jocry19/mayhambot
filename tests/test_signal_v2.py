import pytest

from core import db
from signals import accum, engine, funnel, notifier, plan

NOW = 2_000_000


class FakeQC:
    """Tiers by hand: the real QualityCache needs model versions or wallet histories."""
    sol_usd = 150.0

    def __init__(self, conn, cfg=None, tiers=None):
        self.tiers = tiers or {}
        self.model_tiers = {}
        self.meta = {}

    def get(self, wallet, mint):
        t = self.tiers.get(wallet)
        return (80.0, t, 10) if t else (0.0, None, 0)

    def entity(self, wallet):
        return wallet


@pytest.fixture
def c(tmp_path, monkeypatch):
    conn = db.connect(str(tmp_path / "v2.db"))
    conn.execute("CREATE TABLE IF NOT EXISTS research_funding (wallet TEXT PRIMARY KEY, funder TEXT, ts INTEGER, "
                 "sol REAL, status TEXT, checked_at INTEGER)")
    conn.execute("INSERT INTO tokens(mint, symbol, price_usd, mcap_usd, liquidity_usd, last_updated) "
                 "VALUES ('M','MEME',0.001,500000,40000,?)", (NOW - 60,))
    tiers = {}
    for w, t in (("S1", "smart"), ("S2", "smart"), ("S3", "ultra"), ("S4", "smart")):
        conn.execute("INSERT INTO wallets(address, tier) VALUES (?,?)", (w, t))
        tiers[w] = t
    conn.commit()
    monkeypatch.setattr(engine, "QualityCache", lambda conn, cfg=None: FakeQC(conn, cfg, tiers))
    monkeypatch.setattr(engine, "_safety", None)
    return conn


def trade(c, w, side, usd, ago_min, mint="M"):
    c.execute("INSERT INTO trades(sig, wallet, mint, side, usd_amount, sol_amount, ts) VALUES (?,?,?,?,?,?,?)",
              (f"{w}{side}{ago_min}{mint}", w, mint, side, usd, usd / 150, NOW - ago_min * 60))
    c.commit()


def test_accumulation_over_hours_fires_once(c):
    trade(c, "S1", "buy", 800, 300)
    trade(c, "S2", "buy", 700, 150)
    trade(c, "S3", "buy", 900, 20)
    new = accum.evaluate(c, now=NOW)
    assert len(new) == 1 and new[0]["kind"] == "v2" and new[0]["smart_buyers"] == 3
    assert new[0]["lag_first_s"] == 300 * 60 and new[0]["lag_last_s"] == 20 * 60
    assert new[0]["target_ret"] == plan.DEFAULT_TARGET
    assert accum.evaluate(c, now=NOW + 600) == [] and accum.last_funnel["cooldown"] == 1
    text = notifier.format_signal(new[0])
    assert "Akkumulation (v2)" in text and "hat gekauft" in text


def test_burst_and_sellers_are_not_accumulation(c):
    for w, ago in (("S1", 30), ("S2", 25), ("S3", 20)):   # all within 10 min: a v1 burst, not v2
        trade(c, w, "buy", 800, ago)
    assert accum.evaluate(c, now=NOW) == [] and accum.last_funnel["short_span"] == 1
    trade(c, "S1", "sell", 700, 10)                        # one owner dumped most of it: 2 of 3 still hold
    assert accum.evaluate(c, now=NOW) == [] and accum.last_funnel["few_holders"] == 1


def test_receiver_wallet_counts_for_its_funder(c):
    c.execute("INSERT INTO research_funding(wallet, funder) VALUES ('R1','S3')")
    c.commit()
    trade(c, "S1", "buy", 800, 300)
    trade(c, "S2", "buy", 700, 150)
    trade(c, "R1", "buy", 900, 20)                         # S3's money, bought through a fresh wallet
    new = accum.evaluate(c, now=NOW)
    assert len(new) == 1 and "Empfänger-Wallet" in new[0]["reason"] and new[0]["n_ultra"] == 1
    assert "Empfänger" in notifier.format_signal(new[0])


def test_hub_funded_wallets_are_no_receivers(c):
    c.executemany("INSERT INTO research_funding(wallet, funder) VALUES (?, 'S3')", [(f"X{i}",) for i in range(20)])
    c.commit()
    assert accum.receivers(c, {"S3"}) == {}


def test_stale_price_and_old_bag_do_not_fire(c):
    trade(c, "S1", "buy", 800, 300)
    trade(c, "S2", "buy", 700, 200)
    trade(c, "S3", "buy", 900, 90)                         # nobody added in the last hour
    assert accum.evaluate(c, now=NOW) == []
    trade(c, "S4", "buy", 500, 5)
    c.execute("UPDATE tokens SET last_updated=?", (NOW - 3600,))
    c.commit()
    assert accum.evaluate(c, now=NOW) == [] and accum.last_funnel["stale_price"] == 1


def test_v1_and_v2_kept_apart(c):
    c.execute("INSERT INTO signals(ts, mint, score, price_usd, max_ret_1h, ret_1h, kind) VALUES (?,?,?,?,?,?,?)",
              (NOW - 9000, "M", 50, 1.0, 1.2, 0.3, "v2"))
    c.execute("INSERT INTO signals(ts, mint, score, price_usd, max_ret_1h, ret_1h) VALUES (?,?,?,?,?,?)",
              (NOW - 9000, "N", 50, 1.0, 0.1, -0.2))
    c.commit()
    assert plan.levels(c, NOW, kind="v2")["n"] == 1 and plan.levels(c, NOW)["n"] == 1
    assert funnel.outcomes(c, 24, now=NOW, kind="v2")[-1]["hit_50"] == 1.0
    assert funnel.outcomes(c, 24, now=NOW)[-1]["hit_50"] == 0.0


def test_active_model_is_the_only_quality_source():
    qc = FakeQC(None, tiers={"H": "smart"})                 # heuristic says smart ...
    qc.model_tiers = {"S": ("ultra", 81.0, None, None), "H": (None, 40.0, None, None)}
    qc.meta = {"MAN": {"manual": 1, "is_bot": 0}}
    assert accum.quality(qc, "H", "M") == (0.0, None)      # ... but the active model scored it below smart
    assert accum.quality(qc, "S", "M") == (81.0, "ultra")
    assert accum.quality(qc, "U", "M") == (0.0, None)      # not scored by the model: not counted
    assert accum.quality(qc, "MAN", "M")[1] == "smart"
    assert accum.tiered_wallets(None, qc) == {"S", "MAN"}


def test_vanished_coin_counts_as_dead_not_unmeasured(tmp_path):
    c = db.connect(str(tmp_path / "d.db"))
    now = 100_000
    t = now - 3 * 3600
    c.execute("INSERT INTO signals(ts, mint, score, price_usd) VALUES (?,?,?,?)", (t, "R", 40, 1.0))
    c.execute("INSERT INTO tokens(mint, liquidity_usd, last_updated) VALUES ('R', 40000, ?)", (t + 600,))
    c.execute("INSERT INTO tokens(mint, liquidity_usd, last_updated) VALUES ('OK', 40000, ?)", (now - 60,))
    c.execute("INSERT INTO token_prices(mint, ts, price_usd) VALUES ('R', ?, 1.3)", (t + 300,))
    c.commit()
    engine.update_outcomes(c, now=now)
    s = c.execute("SELECT * FROM signals").fetchone()
    assert s["ret_1h"] == -0.95 and s["outcome_flag"] == "dead"
    assert 0 < s["cost"] < 0.5 and s["ret_4h"] is None             # own round trip (lab.costs); 4 h not due yet
    c.execute("UPDATE tokens SET last_updated=? WHERE mint='OK'", (t + 600,))   # bot was asleep: unknown, not dead
    c.execute("UPDATE signals SET ret_1h=NULL, outcome_flag=NULL")
    c.commit()
    engine.update_outcomes(c, now=now)
    assert c.execute("SELECT ret_1h FROM signals").fetchone()[0] is None


def test_bonding_curve_coins_are_not_called(c):
    c.execute("UPDATE tokens SET dex='pumpfun'")
    c.commit()
    trade(c, "S1", "buy", 800, 300)
    trade(c, "S2", "buy", 700, 150)
    trade(c, "S3", "buy", 900, 20)
    assert accum.evaluate(c, now=NOW) == [] and accum.last_funnel["bonding_curve"] == 1
    assert engine._reason(dict(smart_buyers=2, dex="pumpfun", net_usd=1e4, score=90), None) == "bonding_curve"


def test_inflow_band_fires_as_own_class_without_the_score():
    m = dict(smart_buyers=2, entities=2, net_usd=900, score=20)
    assert engine.in_band(m) and not engine.in_band(dict(m, net_usd=3000)) and not engine.in_band(dict(m, entities=1))
    assert "low_score" in engine.BAND_OVERRIDES and "thin_liq" not in engine.BAND_OVERRIDES
    row = dict(kind="band", symbol="B", mint="M" * 32, score=20, mcap_usd=1e6, price_usd=1.0, net_usd=900, net_sol=6,
               n_ultra=0, n_smart=2, n_good=0, avg_quality=70, wallets_json="[]", smart_buyers=2)
    assert "Zufluss (Band" in notifier.format_signal(type("R", (dict,), {"keys": dict.keys})(row))


def test_band_call_through_evaluate(tmp_path, monkeypatch):
    import dataclasses
    import time as _t
    from core import scoring
    from core.config import settings
    from core.models import Token, Trade
    monkeypatch.setattr(engine, "_safety", None)
    conn = db.connect(str(tmp_path / "b.db"))
    now = int(_t.time())

    def T(sig, w, mint, side, ts, usd, price):
        return Trade(sig, w, mint, side, ts, usd / price, usd / 150, usd, price, "test")
    tr = []
    for k, w in enumerate(["SMART1", "SMART2", "SMART3"]):
        for j, mm in enumerate(["OLD1", "OLD2", "OLD3"]):
            t0 = now - 86400 + j * 3600
            tr += [T(f"b{w}{mm}", w, mm, "buy", t0, 200, 0.001), T(f"s{w}{mm}", w, mm, "sell", t0 + 1800, 200 * (3 + k), 0.001 * (3 + k))]
    for k, w in enumerate(["SMART1", "SMART2", "SMART3"]):
        tr.append(T(f"n{w}", w, "NEW", "buy", now - 300 + k, 300, 0.0002))      # $900 net: inside the band
    db.insert_trades(conn, tr)
    db.upsert_tokens(conn, [Token(mint=m, symbol=m, price_usd=p, liquidity_usd=50000, pool_address="x", dex="pumpswap")
                            for m, p in [("OLD1", 0.003), ("OLD2", 0.003), ("OLD3", 0.003), ("NEW", 0.0002)]])
    scoring.rescore_all(conn)
    cfg = dataclasses.replace(settings, signal_min_score=99.0)              # score alone would block it
    sigs = engine.evaluate(conn, ["NEW"], cfg=cfg, now=now)
    assert len(sigs) == 1 and sigs[0]["kind"] == "band" and "Zufluss-Band" in sigs[0]["reason"]
    assert engine.evaluate(conn, ["NEW"], cfg=cfg, now=now + 60) == []        # shares the v1 cooldown
