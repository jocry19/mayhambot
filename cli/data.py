"""Data for the terminal: one snapshot built from web.queries, plus the few write actions.

Writes only go through copytrade.control (modes, pause, buy/sell requests). Nothing here reads
or prints .env values; the live master switch stays in .env and is only shown as on/off.
"""
from __future__ import annotations

import time

from web import queries as Q
from web.db import cache, has, read_conn, rows, scalar, write_conn


def snapshot(n_signals: int = 60, n_wallets: int = 150) -> dict:
    conn = read_conn()
    now = int(time.time())
    tm = Q.tier_map(conn)
    tiers = {k: sum(1 for v in tm.values() if v.get("tier") == k) for k in Q.TIERS}
    sigs = Q.signals(conn, limit=n_signals)
    open_pos, closed, summary = [], [], {}
    if has(conn, "ct_positions"):
        open_pos = [Q._pos(p) for p in rows(conn, "SELECT * FROM ct_positions WHERE status='open' ORDER BY opened_at DESC")]
        closed = [Q._pos(p) for p in rows(conn, "SELECT * FROM ct_positions WHERE status='closed' ORDER BY closed_at DESC LIMIT 80")]
        for mode in ("paper", "live"):
            ps = [Q._pos(p) for p in rows(conn, "SELECT * FROM ct_positions WHERE mode=?", (mode,))]
            if ps or mode == "paper":
                summary[mode] = Q._summary(ps)
    alerts = rows(conn, "SELECT * FROM ct_alerts ORDER BY ts DESC LIMIT 30") if has(conn, "ct_alerts") else []
    wl = Q.wallets(conn, limit=n_wallets)
    return {
        "now": now,
        "tiers": tiers,
        "tier_source": Q.tier_source(conn),
        "wallets_total": scalar(conn, "SELECT COUNT(*) FROM wallets", default=0) if has(conn, "wallets") else 0,
        "trades_24h": scalar(conn, "SELECT COUNT(*) FROM trades WHERE ts>=?", (now - 86400,), 0) if has(conn, "trades") else 0,
        "last_trade_ts": scalar(conn, "SELECT MAX(ts) FROM trades") if has(conn, "trades") else None,
        "signals": sigs,
        "signals_24h": sum(1 for s in sigs if s["ts"] >= now - 86400),
        "top_coins": Q.coins(conn, sort="strength", limit=15),
        "open": open_pos, "closed": closed, "summary": summary, "alerts": alerts,
        "wallets": wl.get("items", []) if isinstance(wl, dict) else wl,
        "control": Q.control_state(conn),
        "model": Q.model_active(conn),
    }


# ---------------------------------------------------------------- actions
def _control():
    from copytrade import control  # type: ignore

    conn = write_conn()
    control.init(conn)
    cache.clear()
    return control, conn


def set_mode(strategy: str, mode: str) -> str:
    control, conn = _control()
    control.set_mode(conn, strategy, mode)
    return f"{control.STRATEGY_LABELS.get(strategy, strategy)}: {control.MODE_LABELS.get(mode, mode)}"


def set_paused(on: bool) -> str:
    control, conn = _control()
    control.set_paused(conn, on)
    return "Neue Käufe pausiert" if on else "Bot darf wieder kaufen"


def use_preset(strategy: str, name: str) -> str:
    control, conn = _control()
    if name not in control.presets(conn):
        raise ValueError(f"Preset '{name}' gibt es nicht")
    control.use_preset(conn, strategy, name)
    return f"{control.STRATEGY_LABELS.get(strategy, strategy)}: Preset {name}"


def sell(position_id: int) -> str:
    control, conn = _control()
    row = conn.execute("SELECT symbol, status FROM ct_positions WHERE id=?", (position_id,)).fetchone()
    if not row or row[1] != "open":
        raise ValueError(f"Keine offene Position #{position_id}")
    control.request_sell(conn, position_id)
    return f"Verkauf von {row[0] or '#' + str(position_id)} beauftragt"


def buy(mint: str, size_sol: float | None = None) -> str:
    mint = mint.strip()
    if not (32 <= len(mint) <= 48) or not mint.isalnum():
        raise ValueError("Das sieht nicht wie eine Mint-Adresse aus")
    control, conn = _control()
    control.request_buy(conn, mint, size_sol)
    return f"Kauf von {mint[:6]}… beauftragt" + (f" ({size_sol} SOL)" if size_sol else " (Preset-Größe)")
