import dataclasses
import time

from core import db
from signals import run


def setup(tmp_path):
    c = db.connect(str(tmp_path / "t.db"))
    for a, tier in (("U1", "ultra"), ("S1", "smart"), ("S2", "smart"), ("G1", "good")):
        c.execute("INSERT INTO wallets(address, tier) VALUES (?,?)", (a, tier))
    return c


def trade(c, sig, wallet, mint, ts, side="buy"):
    c.execute("INSERT INTO trades(sig, wallet, mint, side, ts, usd_amount) VALUES (?,?,?,?,?,100)",
              (sig, wallet, mint, side, ts))


def test_smart_active_mints_outside_watchlist(tmp_path):
    c = setup(tmp_path)
    now = 100_000
    trade(c, "a", "S1", "HOT", now - 60)
    trade(c, "b", "S2", "HOT", now - 120)
    trade(c, "c", "U1", "ONE", now - 30)
    trade(c, "d", "G1", "GOODONLY", now - 30)          # good tier alone is no smart activity
    trade(c, "e", "S1", "OLD", now - 3 * 3600)          # outside the signal window
    trade(c, "f", "S2", "SOLD", now - 30, side="sell")  # sells alone do not count
    c.commit()
    assert run.smart_active_mints(c, now) == ["HOT", "ONE"]


def test_watchlist_prefers_fresh_smart_buys_over_old_scores(tmp_path, monkeypatch):
    c = setup(tmp_path)
    now = int(time.time())
    for i, m in enumerate(("STALE1", "STALE2", "FRESH")):
        c.execute("INSERT INTO tokens(mint, symbol, pool_address, tracked, liquidity_usd, last_updated, volume_h1) "
                  "VALUES (?,?,?,1,1e6,?,?)", (m, m, "pool" + m, now, 1000 - i))
    for m in ("STALE1", "STALE2"):
        c.execute("INSERT INTO signal_metrics(ts, mint, score) VALUES (?,?,80)", (now - 60, m))
    trade(c, "x", "S1", "FRESH", now - 60)
    c.commit()
    monkeypatch.setattr(run.market, "token_info", lambda mints: [])
    monkeypatch.setattr(run, "settings", dataclasses.replace(run.settings, max_tracked_tokens=2,
                                                             discover_every_cycles=10**9))
    watch = [r["mint"] for r in run.refresh_watchlist(c, 1)]
    assert watch[0] == "FRESH" and len(watch) == 2
