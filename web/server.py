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
import time
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from . import queries as q
from .db import cache, read_conn, write_conn

STATIC = Path(__file__).resolve().parent / "static"
PASSWORD = os.getenv("WEB_PASSWORD", "")
USER = os.getenv("WEB_USER", "mayham")

app = FastAPI(title="mayhambot", docs_url="/api/docs", openapi_url="/api/openapi.json")


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


@app.middleware("http")
async def auth(request: Request, call_next):
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
    elif request.method not in ("GET", "HEAD") and request.client and request.client.host not in ("127.0.0.1", "::1", "localhost"):
        return Json({"detail": "Schreiben nur von localhost oder mit WEB_PASSWORD"}, status_code=403)
    return await call_next(request)


# ---------------------------------------------------------------------- read
@app.get("/api/overview")
def overview():
    return ok(cache.get(("overview",), 5, lambda: q.overview(read_conn())))


@app.get("/api/signals")
def signals(since_h: float = 72, min_score: float = 0, tier: str | None = None, min_wallets: int = 0,
            mint: str | None = None, limit: int = 300):
    since = int(time.time() - since_h * 3600) if since_h > 0 else 0
    return ok(q.signals(read_conn(), since=since, mint=mint, min_score=min_score, tier=tier,
                        min_wallets=min_wallets, limit=min(limit, 2000)))


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
def wallets(tier: str | None = None, sort: str = "score", search: str | None = None, limit: int = 200, offset: int = 0):
    return ok(q.wallets(read_conn(), tier=tier, sort=sort, q=search, limit=min(limit, 2000), offset=offset))


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


@app.get("/api/system")
def system():
    return ok(q.system(read_conn()))


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
    cid = control.request_buy(conn, mint, float(size) if size else None, strategy=strategy)
    return _done("Kauf beauftragt (du verkaufst selbst oder per Verkaufen-Knopf)", command_id=cid)


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
