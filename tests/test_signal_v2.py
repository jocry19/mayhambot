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
