"""mayhambot web dashboard: JSON API + the built frontend (web/static), Python only at runtime.

    python -m web                      # http://127.0.0.1:8600
    WEB_HOST=0.0.0.0 WEB_PASSWORD=...  # on a server: listen on all interfaces, HTTP Basic login (user: mayham)

Writes (operating mode, presets, pause, buy/sell orders, follow wallet) go through copytrade.control into the
shared DB; the running copytrade loop picks them up. The .env master switch for real money (LIVE_TRADING + key)
is never touched here. Without a password, writes are only accepted from localhost.
"""
from __future__ import annotations

try:  # redact api-key=/token= in every log record of this process (core.http, see core/README.md)
    import core.http  # type: ignore  # noqa: F401
except Exception:
    pass

import base64
import hmac
import json
import os
import sqlite3
import time
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from . import queries as q
from .db import cache, read_conn, write_conn

STATIC = Path(__file__).resolve().parent / "static"
PASSWORD = os.getenv("WEB_PASSWORD", "")
USER = os.getenv("WEB_USER", "mayham")


app = FastAPI(title="mayhambot", docs_url="/api/docs", openapi_url="/api/openapi.json")


@app.exception_handler(sqlite3.OperationalError)  # handled inside the app: no traceback, page gets the message
@app.exception_handler(Exception)                   # anything else: still a German message (Starlette logs it too)
async def _unhandled(request: Request, exc: Exception):
    """A view that hits a table another service has not created yet (or any other bug) answers with a German
    message instead of a bare 500, so the page can say what is missing. Keys are stripped from the text."""
    msg = str(exc)
    try:
        from core.http import redact  # type: ignore
        msg = redact(msg)
    except Exception:
        pass
    if isinstance(exc, sqlite3.OperationalError) and ("no such table" in msg or "no such column" in msg):
        what = msg.split(":", 1)[-1].strip()
        return JSONResponse({"detail": f"Daten fehlen noch ({what}). Der zuständige Dienst hat seine Tabelle noch nicht angelegt "
                                       "oder läuft mit einer älteren Version.", "missing": what}, status_code=503)
    if isinstance(exc, sqlite3.OperationalError) and "locked" in msg:
        return JSONResponse({"detail": "Datenbank gerade beschäftigt, nächster Versuch gleich automatisch."}, status_code=503)
    import logging
    logging.getLogger("web").exception("Fehler in %s", request.url.path)
    return JSONResponse({"detail": f"Fehler in dieser Ansicht: {type(exc).__name__}: {msg[:200]}"}, status_code=500)


class Json(JSONResponse):
    def render(self, content) -> bytes:
        return json.dumps(content, default=str, separators=(",", ":"), allow_nan=False).encode()


def _clean(o):
    """NaN/inf -> None so the JSON stays valid."""
    if isinstance(o, float):
        return o if o == o and o not in (float("inf"), float("-inf")) else None
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    return o


def ok(data):
    return Json(_clean(data))


LOCAL_HOSTS = ("127.0.0.1", "::1", "localhost")


def _cross_site(request: Request) -> bool:
    """A write whose Origin/Referer is another site than the one it is sent to (CSRF from a web page
    the user has open), or a localhost-only server reached under a foreign Host name (DNS rebinding)."""
    host = request.headers.get("host", "")
    src = request.headers.get("origin") or request.headers.get("referer")
    if src and src != "null" and urlparse(src).netloc != host:
        return True
    if src == "null":
        return True
    name = urlparse(f"//{host}").hostname or ""
    return not PASSWORD and bool(host) and name not in LOCAL_HOSTS


@app.middleware("http")
async def auth(request: Request, call_next):
    if request.method not in ("GET", "HEAD", "OPTIONS") and _cross_site(request):
        return Json({"detail": "Schreiben nur von der Dashboard-Seite selbst"}, status_code=403)
    if PASSWORD:
        h = request.headers.get("authorization", "")
        good = False
        if h.startswith("Basic "):
            try:
                u, _, p = base64.b64decode(h[6:]).decode().partition(":")
                good = hmac.compare_digest(u, USER) and hmac.compare_digest(p, PASSWORD)
            except Exception:
                good = False
        if not good:
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="mayhambot"'})
    elif request.method not in ("GET", "HEAD") and request.client and request.client.host not in LOCAL_HOSTS:
        return Json({"detail": "Schreiben nur von localhost oder mit WEB_PASSWORD"}, status_code=403)
    return await call_next(request)


# ---------------------------------------------------------------------- read
@app.get("/api/overview")
def overview():
    return ok(cache.get_swr(("overview",), 5, lambda: q.overview(read_conn())))


@app.get("/api/signals")
def signals(since_h: float = 72, min_score: float = 0, tier: str | None = None, min_wallets: int = 0,
            mint: str | None = None, limit: int = 300):
    since = int(time.time() - since_h * 3600) if since_h > 0 else 0
    conn = read_conn()
    out = q.signals(conn, since=since, mint=mint, min_score=min_score, tier=tier,
                    min_wallets=min_wallets, limit=min(limit, 2000))
    held, pending = _buy_state(conn)
    for sg in out:  # for the buy button: already in the coin / buy order waiting
        sg["open_position_id"] = held.get(sg["mint"])
        sg["pending_buy_id"] = pending.get(sg["mint"])
    return ok(out)


def _buy_state(conn):
    held = {r["mint"]: r["id"] for r in q.rows(conn, "SELECT id, mint FROM ct_positions WHERE status='open'")} \
        if q.has(conn, "ct_positions") else {}
    pending = {r["mint"]: r["id"] for r in q.rows(conn, "SELECT id, mint FROM ct_commands WHERE status='pending' AND action='buy'")} \
        if q.has(conn, "ct_commands") else {}
    return held, pending


def _buy_preview(conn, mint: str) -> dict:
    """What a manual buy of `mint` would do right now: preset, size, paper/live, and why it is blocked."""
    try:
        from copytrade import control
        from copytrade.config import load
    except Exception as e:
        return {"available": False, "blocked": f"copytrade fehlt: {e}"}
    held, pending = _buy_state(conn)
    ctl = q.control_state(conn)
    strategy = "signal"
    name = (ctl.get("active_presets") or {}).get(strategy) or "standard"
    p = (ctl.get("presets") or {}).get(name) or {}
    live = bool(ctl.get("live", {}).get(strategy)) and bool(load().live_ready)
    tok = q.one(conn, "SELECT symbol, mcap_usd, liquidity_usd, price_usd FROM tokens WHERE mint=?", (mint,)) \
        if q.has(conn, "tokens") else None
    blocked = None
    if ctl.get("paused"):
        blocked = "Käufe sind pausiert (Notbremse unter Steuerung)"
    elif mint in held:
        blocked = f"Schon im Coin (Position #{held[mint]})"
    elif mint in pending:
        blocked = "Ein Kaufauftrag für diesen Coin wartet schon"
    safety = q.safety_map(conn, [mint]).get(mint)
    if not blocked and safety and safety.get("blocked"):
        why = "; ".join((safety.get("reasons") or [])[:2]) or ("manuell gesperrt" if safety.get("override") == "block" else "")
        blocked = f"Rug-Schutz blockiert diesen Coin ({why}). Freigeben geht auf der Coin-Seite." if why else \
            "Rug-Schutz blockiert diesen Coin. Freigeben geht auf der Coin-Seite."
    return {"available": True, "mint": mint, "symbol": (tok or {}).get("symbol"), "mcap_usd": (tok or {}).get("mcap_usd"),
            "liquidity_usd": (tok or {}).get("liquidity_usd"), "strategy": strategy, "mode": (ctl.get("modes") or {}).get(strategy),
            "preset": name, "size_sol": p.get("size_sol"), "take_profit": p.get("take_profit"), "stop_loss": p.get("stop_loss"),
            "max_hold_min": p.get("max_hold_min"), "account": "live" if live else "paper",
            "open_position_id": held.get(mint), "pending_buy_id": pending.get(mint), "blocked": blocked, "safety": safety}


@app.get("/api/buy/preview")
def buy_preview(mint: str):
    return ok(_buy_preview(read_conn(), mint.strip()))


@app.get("/api/commands/{cid}")
def command(cid: int):
    conn = read_conn()
    if not q.has(conn, "ct_commands"):
        raise HTTPException(404, "Auftrag unbekannt")
    c = q.one(conn, "SELECT * FROM ct_commands WHERE id=?", (cid,))
    if not c:
        raise HTTPException(404, "Auftrag unbekannt")
    pos = None
    if c["action"] == "buy" and c["status"] == "done" and str(c.get("result") or "").isdigit():
        r = q.one(conn, "SELECT * FROM ct_positions WHERE id=?", (int(c["result"]),))
        pos = q._pos(r) if r else None
    return ok({**c, "position": pos})


@app.get("/api/signals/performance")
def signal_perf(since_h: float = 0):
    since = int(time.time() - since_h * 3600) if since_h > 0 else None
    return ok(cache.get(("perf", since_h), 30, lambda: q.signal_performance(read_conn(), since)))


@app.get("/api/coins")
def coins(sort: str = "strength", q_: str | None = None, limit: int = 200, window_min: int = 30, search: str | None = None):
    return ok(q.coins(read_conn(), sort=sort, limit=min(limit, 1000), q=search or q_, window_min=window_min))


@app.get("/api/coins/{mint}")
def coin(mint: str):
    return ok(cache.get(("coin", mint), 5, lambda: q.coin_detail(read_conn(), mint)))


@app.get("/api/wallets")
def wallets(tier: str | None = None, sort: str = "score", search: str | None = None, limit: int = 200, offset: int = 0,
            cluster: str | None = None, group: int = 0):
    return ok(q.wallets(read_conn(), tier=tier, sort=sort, q=search, limit=min(limit, 2000), offset=offset,
                        cluster=cluster or None, group_clusters=bool(group)))


@app.get("/api/wallets/stats")
def wallet_stats():
    return ok(cache.get(("wstats",), 30, lambda: q.wallet_stats_overview(read_conn())))


@app.get("/api/wallets/{address}")
def wallet(address: str):
    return ok(q.wallet_detail(read_conn(), address))


@app.get("/api/copytrade")
def copytrade(mode: str = "paper", strategy: str | None = None):
    if mode not in ("paper", "live"):
        raise HTTPException(400, "mode paper|live")
    return ok(cache.get(("ct", mode, strategy), 4, lambda: q.copytrade(read_conn(), mode, strategy or None)))


def _records(df, limit=2000):
    if df is None or len(df) == 0:
        return []
    df = df[[c for c in df.columns if not str(df[c].dtype).startswith("datetime")]]
    return json.loads(df.to_json(orient="records"))[-limit:]


@app.get("/api/copytrade/learn")
def copytrade_learn(strategy: str = "whale"):
    """Self-learning of copytrade (copytrade/learn.py): status, parameter versions with their P&L,
    variant heatmap, exit-rule comparison and attribution per feature bucket."""
    try:
        from copytrade import control, learn
    except Exception as e:
        return ok({"available": False, "note": f"copytrade-Lernen nicht installiert: {e}"})
    if strategy not in control.STRATEGIES:
        raise HTTPException(400, "Modus unbekannt")
    conn = read_conn()
    if not q.has(conn, "ct_evals") or not q.has(conn, "ct_param_versions"):
        return ok({"available": False, "note": "Das Lernen hat noch nichts ausgewertet. Es läuft alle 10 min im Copy-Trading-Dienst."})

    def run():
        out = {"available": True, "strategy": strategy}
        for key, fn in (("status", lambda: learn.status(conn)),
                        ("versions", lambda: _records(learn.versions(conn, strategy))),
                        ("surface", lambda: _records(learn.variant_surface(conn, strategy, "take_profit", "stop_loss"))),
                        ("exit_rules", lambda: _records(learn.exit_rule_comparison(conn, strategy))),
                        ("attribution", lambda: _records(learn.attribution(conn, strategy))),
                        ("params", lambda: learn.params_for(conn, strategy, apply_caps=True))):
            try:  # one failing view must not hide the others
                out[key] = fn()
            except Exception as e:
                out[key] = None
                out.setdefault("errors", {})[key] = str(e)[:200]
        for v in out.get("versions") or []:
            v["params"] = q._j(v.get("params"), v.get("params"))
            v["notes"] = q._j(v.get("notes"), v.get("notes"))
        return out
    return ok(cache.get(("learn", strategy), 60, run))


@app.get("/api/copytrade/backtest")
def backtest(strategy: str = "whale", preset: str | None = None):
    """Walk-forward backtests of copytrade (mode 1: whale, mode 2: signal with a preset)."""
    try:
        from copytrade import control, stats
        from copytrade.config import load
    except Exception as e:
        raise HTTPException(503, f"copytrade fehlt: {e}")
    conn = read_conn()

    def run():
        if strategy == "signal":
            fn = getattr(stats, "signal_backtest", None)
            if not fn:
                return {"available": False}
            p = control.presets(conn).get(preset) if preset else None
            df = fn(conn, p or control.preset(conn, "signal"))
        else:
            df = stats.backtest(conn, load())
        if df is None or len(df) == 0:
            return {"available": True, "n": 0, "rows": []}
        df = df.copy()
        df = df[[c for c in df.columns if not str(df[c].dtype).startswith("datetime")]]
        recs = json.loads(df.to_json(orient="records"))
        pnl = [r.get("pnl_sol") or 0 for r in recs]
        wins = sum(1 for x in pnl if x > 0)
        return {"available": True, "n": len(recs), "win_rate": wins / len(recs), "pnl_sol": sum(pnl), "rows": recs[-2000:]}
    try:
        return ok(cache.get(("bt", strategy, preset), 60, run))
    except Exception as e:
        if "no such table" in str(e):  # copytrade has not collected anything yet
            return ok({"available": True, "n": 0, "rows": [], "note": "Noch keine Daten für einen Backtest"})
        raise HTTPException(500, f"Backtest fehlgeschlagen: {e}")


@app.get("/api/model")
def model():
    return ok(cache.get(("model",), 20, lambda: q.model_view(read_conn())))


@app.get("/api/sniper")
def sniper_page(hours: float = 24, limit: int = 300):
    hours = max(0.0, min(hours, 24 * 30))
    return ok(cache.get(("sniper", hours, limit), 5, lambda: q.sniper_view(read_conn(), hours=hours, limit=min(limit, 1000))))


@app.get("/api/sniper/{mint}")
def sniper_launch(mint: str):
    conn = read_conn()
    if not q.has(conn, "sniper_launches"):
        raise HTTPException(404, "Launch unbekannt")
    import sniper  # type: ignore
    d = sniper.launch(conn, mint.strip())
    if not d:
        raise HTTPException(404, "Launch unbekannt")
    d.pop("features", None)
    d["check_errors"] = q.sniper_check_errors(conn, [d.get("mint") or mint.strip()]).get(d.get("mint") or mint.strip(), [])
    return ok(d)


@app.get("/api/market")
def market_page(hours: float = 24):
    """Market phase, sector and narrative heat, rotation, evaluation (market/README.md). Read-only."""
    hours = max(1.0, min(hours, 24 * 14))

    def build():
        try:
            import market  # type: ignore
        except Exception as e:
            return {"available": False, "note": f"market/ fehlt: {e}"[:200]}
        return market.view(read_conn(), hours=hours)
    return ok(cache.get(("market", hours), 15, build))


@app.get("/api/market/coin/{mint}")
def market_coin(mint: str):
    """Market context of one coin (sector, heat, narratives, phase) for the coin page."""
    def build():
        try:
            import market  # type: ignore
        except Exception as e:
            return {"available": False, "note": f"market/ fehlt: {e}"[:200]}
        conn = read_conn()
        if not q.has(conn, "market_snapshots"):
            return {"available": False, "note": "Noch keine Marktanalyse."}
        return dict(market.coin_context(conn, mint.strip()), available=True)
    return ok(cache.get(("market_coin", mint), 30, build))


@app.get("/api/intel")
def intel_page():
    """Zentrale: live coin board, smart money flow and rotation, ticker, wallet profiles, source monitor and
    the measurement whether each feature helps (intel/README.md). Read-only."""
    def build():
        try:
            import intel  # type: ignore
        except Exception as e:
            return {"available": False, "note": f"intel/ fehlt: {e}"[:200]}
        conn = read_conn()
        if not q.has(conn, "intel_state"):
            return {"available": False, "note": "Die Zentrale sammelt noch keine Daten: der Dienst intel startet mit dem nächsten Update."}
        return intel.view(conn)
    return ok(cache.get(("intel",), 10, build))


@app.get("/api/intel/coin/{mint}")
def intel_coin(mint: str):
    """Dossier of one coin: all point-in-time features with coverage, flags, ticker events, feature history."""
    def build():
        try:
            import intel  # type: ignore
        except Exception as e:
            return {"available": False, "note": f"intel/ fehlt: {e}"[:200]}
        conn = read_conn()
        if not q.has(conn, "intel_latest"):
            return {"available": False, "note": "Noch keine Zentrale-Daten."}
        return intel.coin_view(conn, mint.strip())
    return ok(cache.get(("intel_coin", mint), 20, build))


@app.get("/api/system")
def system():
    return ok(cache.get_swr(("system",), 5, lambda: q.system(read_conn())))


@app.get("/api/search")
def search(q_: str = "", search: str = ""):
    return ok(q.search(read_conn(), search or q_))


@app.get("/api/control")
def control_state():
    return ok(q.control_state(read_conn()))


# ---------------------------------------------------------------------- write
def _control():
    try:
        from copytrade import control
    except Exception as e:
        raise HTTPException(503, f"copytrade fehlt: {e}")
    conn = write_conn()
    control.init(conn)
    return control, conn


def _done(msg, **extra):
    cache.clear()
    return ok({"ok": True, "message": msg, **extra})


@app.post("/api/control/mode")
async def set_mode(request: Request):
    b = await request.json()
    control, conn = _control()
    try:
        control.set_mode(conn, b.get("strategy"), b.get("mode"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _done(f"{control.STRATEGY_LABELS.get(b['strategy'], b['strategy'])}: {control.MODE_LABELS.get(b['mode'], b['mode'])}")


@app.post("/api/control/preset")
async def use_preset(request: Request):
    b = await request.json()
    control, conn = _control()
    try:
        control.use_preset(conn, b.get("strategy"), b.get("name"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _done(f"Preset {b['name']} für {b['strategy']} aktiv")


@app.post("/api/control/preset/save")
async def save_preset(request: Request):
    b = await request.json()
    name = (b.get("name") or "").strip()
    if not name or len(name) > 40:
        raise HTTPException(400, "Name fehlt")
    control, conn = _control()
    if name in control.BUILTIN:
        raise HTTPException(400, "Eingebaute Presets nicht überschreiben, bitte neuen Namen wählen")
    p = control.Preset.from_dict(b.get("preset") or {})
    # sanity limits so a typo cannot create an absurd preset
    if not (0 < p.size_sol <= 100) or not (0 < p.stop_loss < 1) or p.max_open < 1 or p.max_daily_loss_sol <= 0:
        raise HTTPException(400, "Werte außerhalb des erlaubten Bereichs")
    control.save_preset(conn, name, p)
    if b.get("use_for"):
        control.use_preset(conn, b["use_for"], name)
    return _done(f"Preset {name} gespeichert", preset=asdict(p))


@app.post("/api/control/pause")
async def pause(request: Request):
    b = await request.json()
    control, conn = _control()
    control.set_paused(conn, bool(b.get("on")))
    return _done("Neue Käufe pausiert" if b.get("on") else "Käufe wieder erlaubt")


@app.post("/api/control/live")
async def live(request: Request):
    """Real money for one strategy. Works only when the .env master switch is on; needs typed confirmation."""
    b = await request.json()
    control, conn = _control()
    from copytrade.config import load
    on = bool(b.get("on"))
    if on:
        if not load().live_ready:
            raise HTTPException(409, "Echtgeld-Hauptschalter ist aus (LIVE_TRADING=true und Schlüssel in .env nötig)")
        if b.get("confirm") != "ECHTGELD":
            raise HTTPException(400, "Bestätigung fehlt")
    if b.get("strategy") not in control.STRATEGIES:
        raise HTTPException(400, "unbekannter Modus")
    control.set_live(conn, b["strategy"], on)
    return _done(("Echtgeld AN" if on else "Echtgeld aus") + f" für {b['strategy']}")


@app.post("/api/positions/{pid}/sell")
def sell(pid: int):
    control, conn = _control()
    p = conn.execute("SELECT id, status FROM ct_positions WHERE id=?", (pid,)).fetchone()
    if not p or p["status"] != "open":
        raise HTTPException(404, "Position nicht offen")
    cid = control.request_sell(conn, pid)
    return _done("Verkauf beauftragt, der Bot führt ihn in wenigen Sekunden aus", command_id=cid)


def _live_max_trade_sol() -> float:
    """Hard cap for one manual real-money buy from the dashboard (env LIVE_MAX_TRADE_SOL, default 1 SOL)."""
    try:
        return float(os.getenv("LIVE_MAX_TRADE_SOL") or 1.0)
    except ValueError:
        return 1.0


@app.post("/api/buy")
async def buy(request: Request):
    b = await request.json()
    mint = (b.get("mint") or "").strip()
    if not (32 <= len(mint) <= 50):
        raise HTTPException(400, "Mint-Adresse ungültig")
    size = b.get("size_sol")
    if size is not None and not (0 < float(size) <= 100):
        raise HTTPException(400, "Größe ungültig")
    control, conn = _control()
    strategy = b.get("strategy") if b.get("strategy") in control.STRATEGIES else "signal"
    pv = _buy_preview(conn, mint)
    if pv.get("blocked"):
        raise HTTPException(409, pv["blocked"])
    cap = _live_max_trade_sol()
    eff = float(size) if size else float(pv.get("size_sol") or 0)
    if pv.get("account") == "live" and eff > cap:
        raise HTTPException(409, f"Echtgeld-Kauf über {cap:g} SOL nicht erlaubt (LIVE_MAX_TRADE_SOL)")
    cid = control.request_buy(conn, mint, float(size) if size else None, strategy=strategy)
    acct = "Echtgeld" if pv.get("account") == "live" else "Papier"
    return _done(f"Kauf beauftragt ({acct}, Preset {pv.get('preset')}); der Bot führt ihn in Sekunden aus", command_id=cid,
                 account=pv.get("account"))


@app.get("/api/safety")
def safety_list():
    return ok(cache.get(("safety_list",), 10, lambda: q.safety_list(read_conn())))


@app.get("/api/safety/{mint}")
def safety_detail(mint: str):
    return ok(q.safety_detail(read_conn(), mint.strip()))


SAFETY_ACTIONS = {"block": "gesperrt", "allow": "freigegeben", "clear": "wieder automatisch"}


@app.post("/api/safety/{mint}")
async def safety_override(mint: str, request: Request):
    """Manual block / allow / back to automatic, only through safety.block / allow / clear_override."""
    mint = mint.strip()
    if not (32 <= len(mint) <= 50):
        raise HTTPException(400, "Mint-Adresse ungültig")
    b = await request.json()
    action = b.get("action")
    if action not in SAFETY_ACTIONS:
        raise HTTPException(400, "Aktion muss block, allow oder clear sein")
    reason = str(b.get("reason") or "").strip()[:200]
    try:
        import safety
    except Exception as e:
        raise HTTPException(503, f"Rug-Schutz (safety/) fehlt: {e}")
    conn = write_conn()
    if action == "block":
        safety.block(conn, mint, reason or "manuell im Dashboard gesperrt")
    elif action == "allow":
        safety.allow(conn, mint, reason or "manuell im Dashboard freigegeben")
    else:
        safety.clear_override(conn, mint)
    return _done(f"Coin {SAFETY_ACTIONS[action]}")


@app.post("/api/commands/{cid}/cancel")
def cancel(cid: int):
    control, conn = _control()
    conn.execute("UPDATE ct_commands SET status='cancelled', done_at=? WHERE id=? AND status='pending'", (int(time.time()), cid))
    conn.commit()
    return _done("Auftrag storniert")


@app.post("/api/wallets/{address}/follow")
def follow(address: str):
    if not (32 <= len(address) <= 50):
        raise HTTPException(400, "Adresse ungültig")
    conn = write_conn()
    msgs = []
    try:
        from core import wallets as cw
        cw.add_wallet(conn, address, source="manual")
        msgs.append("Signal-Tracking")
    except Exception:
        pass
    try:
        from copytrade import store
        from copytrade.config import load
        cconn = store.connect(load())
        store.add_wallets(cconn, [address], source="manual")
        msgs.append("Copy-Trading")
    except Exception:
        pass
    if not msgs:
        raise HTTPException(503, "weder core noch copytrade verfügbar")
    return _done("Wallet wird jetzt beobachtet: " + ", ".join(msgs))


# ---------------------------------------------------------------------- frontend
@app.get("/{path:path}")
def spa(path: str):
    if path.startswith("api/"):
        raise HTTPException(404)
    f = (STATIC / path).resolve()
    if path and f.is_file() and STATIC in f.parents:
        headers = {"Cache-Control": "public, max-age=31536000, immutable"} if "/assets/" in f.as_posix() else {}
        return FileResponse(f, headers=headers)
    index = STATIC / "index.html"
    if not index.exists():
        return Response("Frontend nicht gebaut: cd web/frontend && npm install && npm run build", media_type="text/plain")
    return FileResponse(index, headers={"Cache-Control": "no-cache"})
