"""Hard risk limits for every live (and dry-run) buy, independent of strategy and preset.

Checked right before the swap, in this order: kill switch, unresolved orders, failure streak, data health,
live service heartbeat (real mode), go-live gate (real mode), open positions, exposure, rolling 24 h loss
(realized plus open losses), wallet separation, reserve. The size is clamped to the stage ceiling.
Sells are never blocked: getting out always beats staying in.
"""
from __future__ import annotations

import time

from . import store
from .config import LiveConfig

HEARTBEAT_MAX_AGE_S = 300
DATA_DOWN_MAX_AGE_S = 600
_gate_cache: dict[str, tuple[float, bool, str]] = {}
GATE_TTL_S = 600


def open_positions(conn, mode: str) -> list:
    return conn.execute("SELECT * FROM ct_positions WHERE mode=? AND status IN ('open','closing')", (mode,)).fetchall()


def exposure(conn, mode: str) -> float:
    return sum(float(p["entry_cost_sol"] or 0) for p in open_positions(conn, mode))


def loss_24h(conn, mode: str) -> float:
    """Realized PnL of the last 24 h plus open positions marked to their last price, losses only for open ones.
    Negative = loss."""
    r = conn.execute("SELECT COALESCE(SUM(pnl_sol),0) FROM ct_positions WHERE mode=? AND status='closed' "
                     "AND closed_at>=?", (mode, int(time.time()) - 86400)).fetchone()
    realized = float(r[0] or 0)
    unreal = 0.0
    for p in open_positions(conn, mode):
        val = float(p["last_price_sol"] or 0) * float(p["tokens"] or 0)
        unreal += min(0.0, val - float(p["entry_cost_sol"] or 0))
    return realized + unreal


def live_strategies(conn) -> list[str]:
    try:
        from copytrade import control  # type: ignore

        return [s for s in control.STRATEGIES if control.live_wanted(conn, s)]
    except Exception:
        return []


def gate(conn, cfg: LiveConfig, force: bool = False) -> tuple[bool, str]:
    key = str(id(conn))
    hit = _gate_cache.get(key)
    if hit and not force and time.time() - hit[0] < GATE_TTL_S:
        return hit[1], hit[2]
    from . import readiness

    rep = readiness.report(conn, cfg)
    ok = rep["ready"]
    why = "" if ok else "; ".join(c["label"] for c in rep["checks"] if c["required"] and not c["ok"])[:200]
    _gate_cache[key] = (time.time(), ok, why)
    return ok, why


def check_buy(conn, cfg: LiveConfig, mode: str, size_sol: float, balance_sol: float) -> tuple[float, str | None]:
    """Returns (allowed size, None) or (0, reason)."""
    why = store.killed(conn)
    if why:
        return 0, f"Not-Aus aktiv ({why})"
    if store.pending(conn):
        return 0, "unbestätigte Order offen (erst klären, siehe python -m live status)"
    fails = store.consecutive_fails(conn)
    if fails >= cfg.max_consecutive_fails:
        store.kill(conn, f"{fails} Fehlschläge in Folge")
        return 0, f"{fails} fehlgeschlagene Orders in Folge, Not-Aus gesetzt"
    if store.get(conn, "data_down") == "1" and time.time() - float(store.get(conn, "data_down_at") or 0) < DATA_DOWN_MAX_AGE_S:
        return 0, f"Datenausfall ({store.get(conn, 'data_down_reason') or 'Marktdaten'})"
    if mode == "live":
        hb = float(store.get(conn, "heartbeat") or 0)
        if time.time() - hb > HEARTBEAT_MAX_AGE_S:
            return 0, "Live-Wächter läuft nicht (kein Not-Aus über Telegram möglich)"
        if cfg.require_gate:
            ok, why = gate(conn, cfg)
            if not ok:
                return 0, f"Go-Live-Prüfung nicht bestanden: {why}"
    if len(open_positions(conn, mode)) >= cfg.max_open:
        return 0, f"max. {cfg.max_open} offene Echtgeld-Positionen ({cfg.stage.name})"
    size = min(size_sol, cfg.max_position_sol)
    room = cfg.max_exposure_sol - exposure(conn, mode)
    if room < size * 0.5:
        return 0, f"Einsatz-Obergrenze {cfg.max_exposure_sol:g} SOL erreicht"
    size = min(size, room)
    loss = loss_24h(conn, mode)
    if loss <= -cfg.max_daily_loss_sol:
        return 0, f"Tagesverlust-Grenze erreicht ({loss:+.3f} SOL in 24 h, Grenze {cfg.max_daily_loss_sol:g})"
    if mode == "live" and balance_sol > cfg.max_wallet_sol:
        return 0, (f"Trading-Wallet hält {balance_sol:.2f} SOL > {cfg.max_wallet_sol:g} (LIVE_MAX_WALLET_SOL): "
                   "eigene kleine Trading-Wallet nutzen, nicht das Hauptvermögen")
    if balance_sol - size < cfg.reserve_sol:
        size = balance_sol - cfg.reserve_sol
        if size < 0.01:
            return 0, f"Guthaben {balance_sol:.3f} SOL, Reserve {cfg.reserve_sol:g} SOL für Gebühren"
    return round(size, 9), None
