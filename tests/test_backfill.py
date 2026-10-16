import time

from core import db, wallets
from signals import run


def _conn(tmp_path):
    conn = db.connect(str(tmp_path / "b.db"))
    for w in ("A", "B", "C"):
        conn.execute("INSERT INTO wallets(address, is_bot, backfilled_at) VALUES (?, 0, 0)", (w,))
        conn.execute("INSERT INTO wallet_positions(wallet, mint, buy_usd, roi, peak_multiple) VALUES (?, 'M', 1000, 1, 3)",
                     (w,))
    conn.execute("CREATE TABLE research_wallets (address TEXT PRIMARY KEY)")
    conn.execute("CREATE TABLE model_wallet_stats (address TEXT PRIMARY KEY)")
    conn.execute("INSERT INTO research_wallets VALUES ('A')")
    conn.execute("INSERT INTO model_wallet_stats VALUES ('B')")
    return conn


def test_candidates_skip_what_research_covers(tmp_path):
    conn = _conn(tmp_path)
    assert wallets.backfill_candidates(conn, 10) == ["C"]
    assert sorted(wallets.backfill_candidates(conn, 10, skip_research=False)) == ["A", "B", "C"]


def test_backfill_respects_deadline_and_uses_helius_rpc_lane(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    seen = []
    monkeypatch.setattr(run.wallet_source, "wallet_trades",
                        lambda w, **kw: seen.append((w, kw)) or [])
    assert run.backfill(conn, deadline=time.time() - 1) == 0 and seen == []
    run.backfill(conn, deadline=time.time() + 30)
    assert seen and seen[0][1]["helius_rpc"] is True and seen[0][1]["enhanced_fallback"] is False


def test_lanes_order(monkeypatch, tmp_path):
    from core import http
    monkeypatch.setattr(http, "_LOCK_DIR", str(tmp_path))
    assert http.LANE_RANK["live"] > http.LANE_RANK["normal"] > http.LANE_RANK["bulk"]
    assert http.POOL_SLOWDOWN_MAX <= 2 and http.POOL_MAX_SLEEP_429 <= 3 and http.POOL_TIMEOUT <= 8
