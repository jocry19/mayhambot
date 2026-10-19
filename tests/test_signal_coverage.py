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


def test_sol_only_trades_count_as_usd_inflow(tmp_path):
    from signals import engine
    c = setup(tmp_path)
    now = 100_000
    c.execute("INSERT INTO trades(sig, wallet, mint, side, ts, sol_amount, usd_amount) VALUES ('p','G1','X','buy',?,1,150)",
              (now - 10,))
    for sig, w in (("s1", "S1"), ("s2", "S2")):
        c.execute("INSERT INTO trades(sig, wallet, mint, side, ts, sol_amount, usd_amount) VALUES (?,?,'M','buy',?,2,0)",
                  (sig, w, now - 30))
    c.commit()
    assert engine.recent_sol_usd(c) == 150

    class QC:
        sol_usd = 150.0

        def get(self, w, mint):
            return 70.0, {"S1": "smart", "S2": "smart"}.get(w, "good"), 5

        def entity(self, w):
            return w
    m = engine.token_metrics(c, "M", QC(), now)
    assert m["net_usd"] == 600.0 and m["smart_buyers"] == 2


def test_net_minimum_follows_real_smart_buy_size(tmp_path):
    from signals import engine
    cfg = dataclasses.replace(run.settings, signal_min_net_usd=500, signal_net_floor_usd=100, signal_min_wallets=2)
    c = setup(tmp_path)
    now = 200_000
    assert engine.min_net_usd(c, cfg, now) == 500                      # too little history: configured value
    for i in range(30):
        c.execute("INSERT INTO trades(sig, wallet, mint, side, ts, usd_amount) VALUES (?,'S1','M','buy',?,?)",
                  (f"b{i}", now - 60 * i, 140))
    c.execute("INSERT INTO trades(sig, wallet, mint, side, ts, usd_amount) VALUES ('g','G1','M','buy',?,9000)", (now,))
    c.commit()
    assert engine.min_net_usd(c, cfg, now) == 280                      # 2 x median Smart buy
    assert engine.min_net_usd(c, dataclasses.replace(cfg, signal_min_net_usd=200), now) == 200   # capped
    assert engine.min_net_usd(c, dataclasses.replace(cfg, signal_net_floor_usd=300), now) == 300  # floor


def test_backfill_leaves_helius_reserve(monkeypatch):
    from core import http
    monkeypatch.setattr(http, "_service", "signals", raising=False)
    monkeypatch.setattr(http, "credits", lambda svc: {"credits_day_budget": 1000})
    monkeypatch.setattr(http, "CREDIT_BURST", {})
    monkeypatch.setattr(run, "HELIUS_RESERVE", 0.6)
    monkeypatch.setattr(http, "credits_left_today", lambda service=None: 700)
    assert run.helius_ok()
    monkeypatch.setattr(http, "credits_left_today", lambda service=None: 500)   # 40 % used: backfill goes public
    assert not run.helius_ok()
    monkeypatch.setattr(http, "credits_left_today", lambda service=None: None)
    assert run.helius_ok()


def test_snapshots_thinned_except_followed(tmp_path, monkeypatch):
    import time as _t
    from core import db
    from core.models import Token
    conn = db.connect(str(tmp_path / "p.db"))
    now = [3_000_000_000]
    monkeypatch.setattr(_t, "time", lambda: now[0])
    conn.execute("INSERT INTO signals(ts, mint, score, price_usd) VALUES (?, 'A', 50, 1.0)", (now[0] - 600,))
    young = Token(mint="Y", symbol="Y", price_usd=1.0, liquidity_usd=100, pool_address="x", dex="d",
                  pair_created_at=(now[0] - 600) * 1000)
    assert run.dense_mints(conn, [young], now[0]) == {"A", "Y"}
    for _ in range(3):
        toks = [Token(mint=m, symbol=m, price_usd=1.0, liquidity_usd=100, pool_address="x", dex="d")
                for m in ("A", "B")]
        db.upsert_tokens(conn, toks + [young], tracked=False, snap_every=300,
                         dense=run.dense_mints(conn, toks + [young], now[0]))
        now[0] += 60
    n = dict(conn.execute("SELECT mint, COUNT(*) FROM token_prices GROUP BY mint").fetchall())
    assert n == {"A": 3, "B": 1, "Y": 3}


def test_held_and_bought_coins_get_prices(tmp_path):
    from core import db
    conn = db.connect(str(tmp_path / "h.db"))
    now = 3_000_000_000
    conn.execute("CREATE TABLE brain_positions (id INTEGER PRIMARY KEY, mint TEXT, status TEXT, opened_at INTEGER)")
    conn.executemany("INSERT INTO brain_positions(mint, status, opened_at) VALUES (?,?,?)",
                     [("OPEN", "open", now - 99999), ("JUSTSOLD", "closed", now - 600), ("OLD", "closed", now - 99999)])
    assert run.held_mints(conn, now) == ["OPEN", "JUSTSOLD"]
    assert {"OPEN", "JUSTSOLD"} <= run.followed_mints(conn, now)


def test_price_paths_for_migrated_sniper_coins_and_opportunities(tmp_path):
    from signals import run
    c = db.connect(str(tmp_path / "p.db"))
    now = 9_000_000
    c.execute("CREATE TABLE sniper_launches (mint TEXT, migrated_ts INTEGER)")
    c.execute("CREATE TABLE opportunities (mint TEXT, ts INTEGER)")
    c.executemany("INSERT INTO sniper_launches VALUES (?,?)",
                  [("YOUNG", now - 600), ("OLD", now - 5 * 3600), ("OLDFRESH", now - 5 * 3600), ("GONE", now - 30 * 3600)])
    c.executemany("INSERT INTO opportunities VALUES (?,?)", [("OPP", now - 3600 * 2), ("OPPGONE", now - 5 * 3600)])
    c.execute("INSERT INTO tokens(mint, last_updated, tracked) VALUES ('OLDFRESH', ?, 0)", (now - 60,))
    c.commit()
    assert run.price_mints(c, now) == ["YOUNG", "OLD", "OPP"]  # young first; a fresh old one waits for its 5 min


def test_price_paths_without_sniper_or_brain_tables(tmp_path):
    from signals import run
    assert run.price_mints(db.connect(str(tmp_path / "q.db")), 9_000_000) == []


def test_price_load_in_report(tmp_path, monkeypatch):
    from core import http
    from signals import funnel
    c = db.connect(str(tmp_path / "l.db"))
    now = 9_000_000
    c.executemany("INSERT INTO token_prices(mint, ts, price_usd) VALUES (?,?,1)", [("A", now - 60), ("B", now - 7200)])
    c.commit()
    monkeypatch.setattr(http, "usage", lambda all_services=False: {
        "signals": {"groups": {"api.dexscreener.com": {"last_min": 40}}}, "brain": {"groups": {}}})
    monkeypatch.setattr(funnel, "PRICE_LOAD", {"extra_mints_last_round": 700})
    pl = funnel.price_load(c, now)
    assert pl["token_prices_rows_1h"] == 1 and pl["extra_mints_last_round"] == 700
    assert pl["dexscreener_last_min"] == {"signals": 40} and pl["dexscreener_last_min_total"] == 40
