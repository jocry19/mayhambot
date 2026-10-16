"""insert_trades keeps out atomic bot round trips and mis-parsed prices."""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import db  # noqa: E402
from core.models import Token, Trade  # noqa: E402


def test_round_trip_and_price_outlier_are_not_stored():
    conn = db.connect(os.path.join(tempfile.mkdtemp(), "t.db"))
    now = int(time.time())
    db.upsert_tokens(conn, [Token(mint="M", symbol="M", price_usd=0.001, liquidity_usd=50000, pool_address="p")])
    added = db.insert_trades(conn, [
        Trade("rt", "BOT", "M", "buy", now, 1000.0, 1.0, 1.0, 0.001, "geckoterminal"),
        Trade("rt", "BOT", "M", "sell", now, 1000.0, 1.1, 1.1, 0.0011, "geckoterminal"),
        Trade("bad", "W", "M", "buy", now, 1000.0, 9990.0, 1_050_000.0, 1050.0, "helius"),
        Trade("ok", "W", "M", "buy", now, 1000.0, 0.01, 1.2, 0.0012, "helius"),
        Trade("old", "W", "M", "buy", now - 30 * 86400, 10.0, 0.01, 1.0, 0.1, "helius"),  # long ago: price unknown
    ])
    assert added == 2
    assert {r[0] for r in conn.execute("SELECT sig FROM trades")} == {"ok", "old"}
