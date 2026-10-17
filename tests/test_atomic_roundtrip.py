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


def test_frozen_price_of_a_dead_coin_is_not_held_at_entry(tmp_path):
    """A coin nobody refreshed or traded for a day is valued at 0, not at the last (often the wallet's own) print."""
    from core import db, scoring
    from core.models import Trade
    conn = db.connect(str(tmp_path / "d.db"))
    day = scoring.PRICE_DEAD_S
    db.insert_trades(conn, [
        Trade("a", "W", "DEAD", "buy", 1000, 100.0, 1.0, 100.0, 1.0, "t"),
        Trade("b", "W", "LIVE", "buy", 1000, 100.0, 1.0, 100.0, 1.0, "t"),
        Trade("c", "X", "LIVE", "buy", 1000 + 2 * day, 100.0, 1.5, 150.0, 1.5, "t"),
    ])
    conn.execute("INSERT INTO tokens(mint, price_usd, liquidity_usd, last_updated) VALUES ('DEAD', 1.0, 50000, 1000)")
    conn.commit()
    pos = {(p["wallet"], p["mint"]): p for p in scoring.compute_positions(conn)}
    assert pos[("W", "DEAD")]["unrealized_pnl_usd"] == -100.0  # frozen at entry for two days: worth 0
    assert pos[("W", "LIVE")]["unrealized_pnl_usd"] == 50.0  # still traded: last print 1.5


def test_history_trades_valued_at_one_sol_rate(tmp_path):
    """A wallet-history trade stored with USD at another day's SOL price is revalued at the common rate, so its
    entry price, peak multiple and PnL are in the same money as the pool trades."""
    from core import db, scoring
    from core.models import Trade
    conn = db.connect(str(tmp_path / "d.db"))
    db.insert_trades(conn, [
        Trade("h", "W", "M", "buy", 1000, 100.0, 1.0, 200.0, 2.0, "helius"),   # fetched when SOL was quoted at $200
        Trade("p1", "X", "M", "buy", 1100, 100.0, 1.0, 150.0, 1.5, "geckoterminal"),
        Trade("p2", "Y", "M", "buy", 1200, 100.0, 2.0, 300.0, 3.0, "geckoterminal"),
        Trade("p3", "Z", "N", "buy", 1200, 100.0, 1.0, 150.0, 1.5, "geckoterminal"),
    ])
    assert scoring.sol_rate(conn) == 150.0
    pos = {(p["wallet"], p["mint"]): p for p in scoring.compute_positions(conn)}
    assert pos[("W", "M")]["first_buy_price"] == 1.5 and pos[("W", "M")]["buy_usd"] == 150.0
    assert pos[("W", "M")]["peak_multiple"] == 2.0   # 3.0 / 1.5, not 3.0 / 2.0
