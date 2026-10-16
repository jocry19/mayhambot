"""Logic tests on synthetic trades (no network). Real-data checks: python -m signals.run --once"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import db, scoring  # noqa: E402
from core.models import Token, Trade  # noqa: E402
from signals import engine  # noqa: E402


def T(sig, w, mint, side, ts, usd, price):
    return Trade(sig, w, mint, side, ts, usd / price, usd / 150, usd, price, "test")


def test_smart_wallet_and_signal():
    conn = db.connect(os.path.join(tempfile.mkdtemp(), "t.db"))
    now = int(time.time())
    trades = []
    # "smart" wallets: bought 3 old tokens early, sold 3-5x higher
    for k, w in enumerate(["SMART1", "SMART2", "SMART3"]):
        for j, m in enumerate(["OLD1", "OLD2", "OLD3"]):
            t0 = now - 86400 + j * 3600
            trades += [T(f"b{w}{m}", w, m, "buy", t0, 200, 0.001), T(f"s{w}{m}", w, m, "sell", t0 + 1800, 200 * (3 + k), 0.001 * (3 + k))]
    # "dumb" wallet: buys tops, sells lower
    for m in ["OLD1", "OLD2", "OLD3"]:
        trades += [T(f"bD{m}", "DUMB", m, "buy", now - 80000, 500, 0.004), T(f"sD{m}", "DUMB", m, "sell", now - 70000, 200, 0.0016)]
    # now: smart wallets buy NEW
    for k, w in enumerate(["SMART1", "SMART2", "SMART3"]):
        trades.append(T(f"n{w}", w, "NEW", "buy", now - 300 + k, 1500, 0.0002))
    trades.append(T("nD", "DUMB", "NEW", "buy", now - 200, 5000, 0.0002))
    db.insert_trades(conn, trades)
    db.upsert_tokens(conn, [Token(mint=m, symbol=m, price_usd=p, liquidity_usd=50000, pool_address="x")
                            for m, p in [("OLD1", 0.003), ("OLD2", 0.003), ("OLD3", 0.003), ("NEW", 0.0002)]])
    scoring.rescore_all(conn)
    w = {r["address"]: r for r in conn.execute("SELECT * FROM wallets")}
    print({k: (round(v["score"], 1), v["is_smart"]) for k, v in w.items()})
    assert w["SMART1"]["is_smart"] and not w["DUMB"]["is_smart"]
    sigs = engine.evaluate(conn, ["NEW"], now=now)
    m = conn.execute("SELECT * FROM signal_metrics").fetchone()
    print(dict(m))
    assert m["smart_buyers"] == 3 and abs(m["net_usd"] - 4500) < 1
    assert len(sigs) == 1


def test_rpc_parse():
    from core.sources import wallet_history as wh
    wh.sol_price_usd = lambda: 150.0
    tx = {"blockTime": 1700000000, "meta": {"err": None, "fee": 5000,
          "preBalances": [2_000_000_000, 0], "postBalances": [999_995_000, 0],
          "preTokenBalances": [], "postTokenBalances": [
              {"accountIndex": 1, "mint": "MEME", "owner": "W", "uiTokenAmount": {"uiAmountString": "1000000"}}]},
          "transaction": {"signatures": ["SIG"], "message": {"accountKeys": [{"pubkey": "W"}, {"pubkey": "ATA"}]}}}
    t = wh.parse_rpc_tx(tx, "W")
    assert len(t) == 1 and t[0].side == "buy" and abs(t[0].sol_amount - 1.0) < 1e-6 and abs(t[0].usd_amount - 150) < 1e-6


if __name__ == "__main__":
    test_smart_wallet_and_signal()
    test_rpc_parse()
    print("ok")
