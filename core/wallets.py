"""Wallet registry: manual (user-added) and auto-discovered wallets."""
import time

from core.config import settings


def add_wallet(conn, address, label="", source="manual"):
    now = int(time.time())
    manual = 1 if source == "manual" else 0
    conn.execute(
        """INSERT INTO wallets(address, label, source, manual, is_smart, first_seen, updated_at)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(address) DO UPDATE SET label=COALESCE(NULLIF(excluded.label,''), label),
               manual=MAX(manual, excluded.manual), source=CASE WHEN excluded.manual=1 THEN 'manual' ELSE source END,
               is_smart=MAX(is_smart, excluded.is_smart)""",
        (address, label, source, manual, manual, now, now))
    conn.commit()


def get_wallet(conn, address):
    return conn.execute("SELECT * FROM wallets WHERE address=?", (address,)).fetchone()


def smart_wallets(conn, min_score=None):
    q = "SELECT * FROM wallets WHERE is_smart=1 AND is_bot=0"
    args = ()
    if min_score is not None:
        q += " AND (score>=? OR manual=1)"
        args = (min_score,)
    return conn.execute(q + " ORDER BY score DESC", args).fetchall()


def backfill_candidates(conn, limit):
    """Wallets worth fetching full history for: early, profitable entries, not bots, not recently backfilled."""
    return [r[0] for r in conn.execute(
        """SELECT w.address FROM wallets w
           JOIN wallet_positions p ON p.wallet = w.address
           WHERE w.is_bot=0 AND w.backfilled_at < ? AND p.buy_usd >= ?
           GROUP BY w.address
           ORDER BY w.manual DESC, MAX(COALESCE(p.peak_multiple,0)) * (SUM(p.roi > 0) + 1) DESC
           LIMIT ?""", (int(time.time()) - 6 * 3600, settings.min_position_usd, limit))]


def mark_backfilled(conn, address):
    conn.execute("UPDATE wallets SET backfilled_at=? WHERE address=?", (int(time.time()), address))
    conn.commit()
