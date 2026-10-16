"""A buy and a sell of the same token by the same wallet in one transaction (MEV / volume bot) is no position."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import db, scoring  # noqa: E402
from core.models import Trade  # noqa: E402


def test_same_sig_buy_and_sell_is_not_a_position():
    conn = db.connect(os.path.join(tempfile.mkdtemp(), "t.db"))
    db.insert_trades(conn, [
        Trade("atomic", "BOT", "M", "buy", 1000, 1000.0, 1.0, 100.0, 0.1, "geckoterminal"),
        Trade("atomic", "BOT", "M", "sell", 1000, 1000.0, 1.3, 130.0, 0.13, "geckoterminal"),
        Trade("b1", "HOLDER", "M", "buy", 1000, 1000.0, 1.0, 100.0, 0.1, "geckoterminal"),
        Trade("s1", "HOLDER", "M", "sell", 1600, 1000.0, 2.0, 200.0, 0.2, "geckoterminal"),
    ])
    pos = {p["wallet"]: p for p in scoring.compute_positions(conn)}
    assert "BOT" not in pos
    assert round(pos["HOLDER"]["realized_pnl_usd"], 6) == 100.0
