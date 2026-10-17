"""API and terminal smoke tests on a synthetic DB (web.devdata) and on an empty DB.

    python -m pytest web/tests -q
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

GET = ["/api/overview", "/api/signals", "/api/signals/performance", "/api/coins", "/api/wallets", "/api/wallets/stats",
       "/api/copytrade", "/api/copytrade?mode=live", "/api/model", "/api/system", "/api/control", "/api/search?search=a",
       "/api/copytrade/backtest?strategy=whale", "/api/copytrade/learn?strategy=whale", "/api/copytrade/learn?strategy=signal", "/api/safety",
       "/api/sniper", "/api/sniper?hours=6", "/api/market", "/api/market?hours=168", "/api/market/coin/So11111111111111111111111111111111111111112"]


def _client(db: str):
    os.environ["MAYHAM_DB_PATH"] = db
    os.environ.pop("WEB_PASSWORD", None)
    from web import db as wdb
    from web.server import app
    from web.tests.asgi import Client

    wdb.cache.clear()
    return Client(app, client=("127.0.0.1", 5000))


@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    from web import devdata

    p = str(tmp_path_factory.mktemp("db") / "demo.db")
    devdata.build(p, days=1.0, n_tokens=10, n_wallets=200)
    return p


@pytest.fixture(scope="module")
def empty(tmp_path_factory):
    p = str(tmp_path_factory.mktemp("db") / "empty.db")
    sqlite3.connect(p).close()
    return p


@pytest.mark.parametrize("url", GET)
def test_get_empty_db(empty, url):
    r = _client(empty).get(url)
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("url", GET)
def test_get_demo_db(demo, url):
    r = _client(demo).get(url)
    assert r.status_code == 200, r.text


def test_detail_pages(demo):
    c = _client(demo)
    coin = c.get("/api/coins").json()[0]["mint"]
    d = c.get(f"/api/coins/{coin}").json()
    assert d["token"]["mint"] == coin and "signals" in d and "flows" in d
    w = c.get("/api/wallets").json()["items"][0]["address"]
    assert c.get(f"/api/wallets/{w}").json()["address"] == w


def test_signal_has_breakdown_and_tiers(demo):
    sigs = _client(demo).get("/api/signals").json()
    assert sigs, "devdata should produce signals"
    s = sigs[0]
    assert set(s["tiers"]) == {"ultra", "smart", "good"}
    assert s["grade"] in "ABCDE" and s["breakdown"]


def test_no_secrets_in_system(demo, monkeypatch):
    monkeypatch.setenv("HELIUS_API_KEY", "SECRET-KEY-123")
    monkeypatch.setenv("SOLANA_RPC_URL", "https://rpc.example/?api-key=SECRET-KEY-123")
    c = _client(demo)
    for url in ("/api/system", "/api/control", "/api/overview"):
        assert "SECRET-KEY-123" not in c.get(url).text


def test_controls_write_config_only(demo):
    c = _client(demo)
    r = c.post("/api/control/mode", json={"strategy": "signal", "mode": "manual"})
    assert r.status_code == 200, r.text
    assert c.get("/api/control").json()["modes"]["signal"] == "manual"
    assert c.post("/api/control/mode", json={"strategy": "signal", "mode": "yolo"}).status_code == 400
    # real money needs the .env master switch; without it the API refuses
    r = c.post("/api/control/live", json={"strategy": "signal", "on": True, "confirm": "ECHTGELD"})
    assert r.status_code in (400, 403, 409)


def test_writes_refused_from_lan(demo):
    from web.server import app
    from web.tests.asgi import Client

    os.environ["MAYHAM_DB_PATH"] = demo
    c = Client(app, client=("192.168.1.20", 5000))
    assert c.post("/api/control/pause", json={"on": True}).status_code in (401, 403)


def test_cli_reports(demo, capsys):
    os.environ["MAYHAM_DB_PATH"] = demo
    from cli.__main__ import main

    for cmd in (["status"], ["signals", "-n", "3"], ["wallets", "-n", "3"], ["positions"]):
        assert main(cmd) == 0
    out = capsys.readouterr().out
    assert "mayhambot" in out and "Signale" in out


def test_buy_button_flow(demo):
    c = _client(demo)
    sigs = c.get("/api/signals").json()
    free = next(s for s in sigs if not s["open_position_id"])
    pv = c.get(f"/api/buy/preview?mint={free['mint']}").json()
    assert pv["account"] == "paper" and pv["preset"] and pv["size_sol"] and not pv["blocked"]
    if (pv.get("safety") or {}).get("level") != "low":  # rug warning must be acknowledged explicitly
        r = c.post("/api/buy", json={"mint": free["mint"], "strategy": "signal"})
        assert r.status_code == 409 and "Rug-Warnung" in r.json()["detail"]
    r = c.post("/api/buy", json={"mint": free["mint"], "strategy": "signal", "risk_ack": True})
    assert r.status_code == 200, r.text
    cid = r.json()["command_id"]
    assert c.get(f"/api/commands/{cid}").json()["status"] == "pending"
    # a second order for the same coin is refused while the first waits
    assert c.post("/api/buy", json={"mint": free["mint"]}).status_code == 409
    held = next((s for s in sigs if s["open_position_id"]), None)
    if held:
        assert c.post("/api/buy", json={"mint": held["mint"]}).status_code == 409
    c.post("/api/control/pause", json={"on": True})
    other = next(x for x in c.get("/api/coins").json() if x["mint"] != free["mint"])
    assert c.post("/api/buy", json={"mint": other["mint"]}).status_code == 409
    c.post("/api/control/pause", json={"on": False})


def test_learn_view(demo, empty):
    d = _client(empty).get("/api/copytrade/learn?strategy=whale").json()
    assert d["available"] is False and d["note"]
    c = _client(demo)
    for st in ("whale", "signal"):
        d = c.get(f"/api/copytrade/learn?strategy={st}").json()
        assert d["available"] and d["strategy"] == st, d
        assert d["versions"] and all(isinstance(v["params"], dict) for v in d["versions"])
        assert d["surface"] and d["exit_rules"] and d["attribution"]
    pos = c.get("/api/copytrade").json()
    assert any(p.get("param_version") for p in pos["open"] + pos["closed"])


def test_safety_views_and_overrides(demo, empty):
    assert _client(empty).get("/api/safety").json()["available"] is False
    c = _client(demo)
    sigs = c.get("/api/signals").json()
    assert any(s["safety"] and s["safety"]["level"] for s in sigs)
    coins = c.get("/api/coins").json()
    held = {p["mint"] for p in c.get("/api/copytrade").json()["open"]}
    mint = next(x["mint"] for x in coins if x.get("safety") and not x["safety"]["blocked"] and x["mint"] not in held)
    d = c.get(f"/api/coins/{mint}").json()["safety"]
    assert d["checked"] and 0 <= d["score"] <= 100 and d["checks"] and d["history"]
    assert all("data" not in ch for ch in d["checks"])
    ct = c.get("/api/copytrade").json()
    assert any(p.get("safety_level") for p in ct["open"] + ct["closed"])
    assert isinstance(ct["closed"][0]["safety_reasons"], list)
    assert any(a.get("safety") for a in ct["alerts"])
    # block -> buy refused, listed; allow; clear
    assert c.post(f"/api/safety/{mint}", json={"action": "nope"}).status_code == 400
    assert c.post(f"/api/safety/{mint}", json={"action": "block", "reason": "test"}).status_code == 200
    assert c.get(f"/api/coins/{mint}").json()["safety"]["blocked"] is True
    pv = c.get(f"/api/buy/preview?mint={mint}").json()
    assert pv["blocked"] and "Rug-Schutz" in pv["blocked"]
    assert c.post("/api/buy", json={"mint": mint}).status_code == 409
    assert any(i["mint"] == mint and i["override"] == "block" for i in c.get("/api/safety").json()["items"])
    assert c.post(f"/api/safety/{mint}", json={"action": "clear"}).status_code == 200
    assert c.get(f"/api/coins/{mint}").json()["safety"]["override"] is None
    from web.tests.asgi import Client
    from web.server import app
    assert Client(app, client=("192.168.1.20", 5000)).post(f"/api/safety/{mint}", json={"action": "block"}).status_code in (401, 403)


def test_cli_safety(demo, capsys):
    os.environ["MAYHAM_DB_PATH"] = demo
    from web import db as wdb
    wdb.cache.clear()
    from cli.__main__ import main
    assert main(["safety"]) == 0
    out = capsys.readouterr().out
    assert "Gesperrt" in out or "Kein Coin" in out
    mint = sqlite3.connect(demo).execute("SELECT mint FROM safety_tokens LIMIT 1").fetchone()[0]
    assert main(["safety", mint]) == 0
    assert "Rug-Risiko" in capsys.readouterr().out
    assert main(["safety", "block", mint, "test"]) == 0
    assert sqlite3.connect(demo).execute("SELECT action FROM safety_overrides WHERE mint=?", (mint,)).fetchone()[0] == "block"
    assert main(["safety", "clear", mint]) == 0
    assert main(["signals", "-n", "3"]) == 0 and main(["positions"]) == 0


def test_api_usage(demo, tmp_path, monkeypatch, capsys):
    import json
    import time

    from core import http as chttp
    from web import queries as Q

    monkeypatch.setattr(chttp, "_LOCK_DIR", str(tmp_path))
    monkeypatch.setenv("HELIUS_SHARE_SAFETY", "15")  # keep safety's cap above the 120000 used below
    Q._USAGE_HIST.clear()
    now = int(time.time())

    def write(service, ts, calls, last, extra=None):
        g = {"helius": {"calls": calls, "429": 2, "waited_s": 12.5, "budget_per_min": 60, "last_min": last, **(extra or {})},
             "api.dexscreener.com": {"calls": 40, "429": 0, "waited_s": 0, "budget_per_min": None, "last_min": 3}}
        (tmp_path / f"usage-{service}.json").write_text(json.dumps({"service": service, "pid": 1, "ts": ts, "groups": g}))

    write("safety", now - 300, 100, 20, {"live_calls": 10, "bulk_calls": 50, "live_waited_s": 1.0, "bulk_waited_s": 4.0})
    write("research", now - 4000, 10, 1)
    c = _client(demo)
    u = c.get("/api/system").json()["api_usage"]
    assert u["available"]
    write("safety", now, 400, 54, {"credits": 900, "refused": 7, "live_calls": 30, "bulk_calls": 250,
                                    "live_waited_s": 4.5, "bulk_waited_s": 30.0})
    # shared Helius ledger: this month, safety used 120000, 3000 of them today
    month, day = chttp._now_parts()[:2]
    (tmp_path / "credits-helius.json").write_text(json.dumps({"month": month, "services": {
        "safety": {"month": 120000, "days": {day: 3000}}, "research": {"month": 700000, "days": {day: 1000}}}}))
    from web.db import cache
    cache.clear()
    rows = {(r["service"], r["group"]): r for r in c.get("/api/system").json()["api_usage"]["rows"]}
    s = rows[("safety", "helius")]
    assert s["calls_window"] == 300 and s["window_s"] == 300 and s["load"] == 0.9 and s["429"] == 2
    assert s["credits_month"] == 120000 and s["credits_today"] == 3000 and s["refused"] == 7
    assert s["credits_cap"] and s["load_month"] == round(120000 / s["credits_cap"], 3) and s["load_day"] is not None
    assert s["live_calls_window"] == 20 and s["live_waited_s_window"] == 3.5 and s["bulk_calls_window"] == 200
    assert rows[("research", "helius")]["day_full"] is True and rows[("research", "helius")]["credits_day_budget"] == 0
    assert rows[("research", "helius")]["load_day"] == 1.0
    assert ("sniper", "helius") in rows and rows[("sniper", "helius")]["stale"]  # share but no process yet
    tot = c.get("/api/system").json()["api_usage"]["credits"]
    assert tot["used"] == 820000 and tot["limit"] == 1000000 and tot["load"] == 0.82
    assert rows[("research", "helius")]["stale"] and rows[("research", "helius")]["load"] is None
    assert rows[("safety", "api.dexscreener.com")]["load"] is None
    from cli.report import api_line
    line = api_line()
    assert "Helius 54/60" in line and "knapp: safety" in line and "research" not in line
    from cli.report import credits_line
    cl = credits_line()
    assert "820.000 von 1.000.000" in cl and "yellow" in cl and "7 Aufrufe abgewiesen" in cl


def test_wallet_tiers_from_wallets_table(demo, tmp_path, monkeypatch):
    c = _client(demo)
    w = c.get("/api/wallets?limit=2000").json()
    assert w["tier_info"]["source"] == "model" and w["tier_info"]["synced"] and w["tier_info"]["version"]
    con = sqlite3.connect(demo)
    want = dict(con.execute("SELECT tier, COUNT(*) FROM wallets WHERE tier IS NOT NULL GROUP BY tier").fetchall())
    assert w["counts"] == {k: want.get(k, 0) for k in ("ultra", "smart", "good")}
    assert c.get("/api/overview").json()["tiers"] == w["counts"]
    it = w["items"][0]
    assert it["tier_source"] == "model" and it["tier_version"] and "score_heuristic" in it and "model_score" in it
    # one entity per cluster
    cl = next(x for x in w["items"] if (x.get("cluster_n") or 0) > 1)
    members = c.get(f"/api/wallets?limit=2000&cluster={cl['cluster_id']}").json()["items"]
    assert len(members) == cl["cluster_n"] and all(m["cluster_id"] == cl["cluster_id"] for m in members)
    g = c.get("/api/wallets?limit=2000&group=1").json()
    assert g["total"] == w["total"] - sum(n - 1 for n in {x["cluster_id"]: x["cluster_n"] for x in w["items"] if x.get("cluster_id")}.values())
    assert c.get("/api/wallets?sort=exit_capture").status_code == 200
    # next retrain: from the research log line, else computed
    m = c.get("/api/model").json()
    assert m["retrain"]["source"] in ("berechnet", "log") and m["tier_info"]["counts"] == w["counts"]
    (tmp_path / "research.log").write_text("x model: next retrain at 2099-01-02 03:04 UTC\n")
    monkeypatch.setenv("MAYHAM_LOG_DIR", str(tmp_path))
    from web.db import cache
    cache.clear()
    r = c.get("/api/model").json()["retrain"]
    assert r["source"] == "log" and r["next_ts"] == __import__("calendar").timegm((2099, 1, 2, 3, 4, 0)) and not r["due"] and not r["failed"]


def test_sniper_page(demo, empty, capsys):
    c = _client(empty)
    assert c.get("/api/sniper").json()["available"] is False
    assert c.get("/api/overview").json()["sniper"] is None
    c = _client(demo)
    d = c.get("/api/sniper?hours=24").json()
    assert d["available"] and d["launches"] and d["positions"] and d["balance_curve"]
    assert {x["source_label"] for x in d["launches"]} <= {"pump.fun", "Migration", "neuer Pool"}
    ft = d["stats"]["filter_total"]
    assert ft["measured"] > 0 and abs(ft["without_sol"] - ((ft["with_sol"] or 0) + ft["skipped_sol"])) < 1e-3
    assert any(f.get("filter_value_sol") is not None for f in d["stats"]["filters"])
    assert 0.05 < ft["cost_pct"] < 0.3  # skipped launches pay the same round-trip costs as paper trades
    mint = d["launches"][0]["mint"]
    assert c.get(f"/api/sniper/{mint}").status_code == 200
    o = c.get("/api/overview").json()["sniper"]
    assert o["launches_today"] >= 0 and o["last_buy"]["mint"] and "pnl_sol" in o
    from cli import data as cdata
    from cli.report import sniper_line

    line = sniper_line(cdata.snapshot(n_signals=5, n_wallets=0)["sniper"])
    assert line.startswith("Launch-Sniper (Papier):") and "Launches heute" in line
    assert sniper_line(None) == ""


def test_sniper_heartbeat(tmp_path):
    import json

    from web import queries as Q

    p = str(tmp_path / "hb.db")
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE sniper_state(key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("CREATE TABLE sniper_launches(mint TEXT, created_ts INT, decided_ts INT, stage INT, decision TEXT)")

    def beat(ts, creates, connected=True):
        conn.execute("INSERT OR REPLACE INTO sniper_state VALUES ('service_status', ?)", (json.dumps({
            "ts": ts, "connected": connected, "subscribed": 5, "queue": 0, "open_positions": 1, "pump_api_ok": True,
            "budget": {"rpc": {"used_last_min": 30, "per_min": 120, "total": 900}, "deep": {"used_last_min": 4, "per_min": 4, "total": 40}},
            "counters": {"creates": creates, "stage2": creates // 10, "entries": creates // 100}}),))

    Q._SNIPER_HIST.clear()
    assert Q.sniper_stream(conn, now=1000)["source"] == "none"
    beat(1000, 1000)
    s = Q.sniper_stream(conn, now=1005)
    assert s["source"] == "heartbeat" and s["connected"] and s["service_up"] and s["rate_source"] == "db"
    assert s["budget"]["rpc"]["load"] == 0.25 and s["load_deep"] == 1.0
    beat(1600, 1500)     # 500 launches in 10 min -> 3000 per hour
    s = Q.sniper_stream(conn, now=1610)
    assert s["rate_source"] == "counters" and s["launches_1h"] == 3000 and s["deep_1h"] == 300
    beat(1700, 10)       # counters dropped: service restarted, no rate until a minute of samples
    assert Q.sniper_stream(conn, now=1700)["rate_source"] == "db"
    s = Q.sniper_stream(conn, now=1700 + 31)
    assert s["source"] == "stale" and not s["connected"] and not s["service_up"] and "budget" not in s
    beat(1800, 20, connected=False)
    s = Q.sniper_stream(conn, now=1800)
    assert s["service_up"] and not s["connected"]
    Q._SNIPER_HIST.clear()


def test_rpc_pool(demo, tmp_path, monkeypatch, capsys):
    import json
    import time as _t

    from core import http as chttp
    from web import queries as Q
    from cli.report import rpc_pool_line

    monkeypatch.setattr(chttp, "_LOCK_DIR", str(tmp_path))
    monkeypatch.setenv("PUBLIC_RPC_URLS", "https://a.example,https://b.example/?api-key=SECRET,https://c.example")
    monkeypatch.setattr(chttp, "_latency", {})
    now = _t.time()
    (tmp_path / "rpc-pool.json").write_text(json.dumps({
        "a.example": {"ok": True, "checked_at": now - 60, "latency_ms": 180, "disabled_until": 0},
        "b.example": {"ok": True, "checked_at": now - 60, "latency_ms": 90, "disabled_until": now + 300, "reason": "429 x3"}}))
    usage = {"rows": [
        {"service": "copytrade", "group": "a.example", "calls": 500, "calls_window": 200, "429_window": 3, "live_calls_window": 50,
         "bulk_calls_window": 150, "latency_ms_window": 1200.0, "window_s": 600},
        {"service": "sniper", "group": "a.example", "calls": 100, "calls_window": 100, "429_window": 0, "live_calls_window": 50,
         "bulk_calls_window": 50, "latency_ms_window": 900.0, "window_s": 600}]}
    p = Q.rpc_pool(now=now, usage=usage)
    assert p["total"] == 3 and p["active"] == 2
    r = {x["host"]: x for x in p["rows"]}
    assert r["b.example"]["state"] == "pausiert" and r["b.example"]["reason"] == "429 x3"
    assert r["c.example"]["state"] == "ungeprüft" and r["c.example"]["calls_window"] is None
    a = r["a.example"]
    assert a["calls_window"] == 300 and a["429_window"] == 3 and a["live_share"] == 0.333
    assert a["latency_ms"] == 1100 and a["latency_src"] == "Aufrufe" and a["services"] == ["copytrade", "sniper"]
    assert p["fastest_host"] == "a.example" and "SECRET" not in json.dumps(p)
    monkeypatch.setattr(Q, "rpc_pool", lambda **k: p)
    line = rpc_pool_line()
    assert line.startswith("RPC-Pool: 2 von 3 Endpunkten aktiv") and "1.100 ms" in line and "pausiert: b.example" in line
    s = _client(demo).get("/api/system").json()
    assert "rpc_pool" in s


def test_sniper_check_errors_and_trade_source(demo):
    from web import queries as Q

    d = _client(demo).get("/api/sniper?limit=2000").json()
    errs = [x for x in d["launches"] if x["check_errors"]]
    assert errs and all(isinstance(x["check_errors"], list) for x in d["launches"])
    flat = " ".join(e for x in errs for e in x["check_errors"])
    assert "api-key=demo" not in flat and "/coins/x" not in flat
    det = _client(demo).get(f"/api/sniper/{errs[0]['mint']}").json()
    assert det["check_errors"] == errs[0]["check_errors"]
    assert Q._redact("x https://h.example/?api-key=SECRET&b=1 y") == "x https://h.example y"
    import json

    conn = sqlite3.connect(demo)
    conn.row_factory = sqlite3.Row
    ts = json.loads(conn.execute("SELECT value FROM sniper_state WHERE key='service_status'").fetchone()[0])["ts"]
    Q._SNIPER_HIST.clear()
    st = Q.sniper_stream(conn, now=ts + 5)
    Q._SNIPER_HIST.clear()
    assert st["source"] == "heartbeat"
    assert st["trade_source"] == "rpc" and st["rpc_ws_connected"] is True and st["rpc_ws_subs"] == 112 and st["rpc_ws_max_subs"] == 150


def test_sniper_launch_sources(tmp_path):
    import json

    from web import queries as Q

    conn = sqlite3.connect(str(tmp_path / "ls.db"))
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE sniper_state(key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("CREATE TABLE sniper_launches(mint TEXT, created_ts INT, decided_ts INT, stage INT, decision TEXT)")
    conn.execute("INSERT INTO sniper_state VALUES ('service_status', ?)", (json.dumps({
        "ts": 1000, "connected": False, "active_launch_source": "rpc", "launches_per_min": 9.0,
        "launch_sources": {"pumpportal": {"enabled": True, "connected": False, "per_min": 0, "first_per_min": 0, "last_ts": None},
                           "rpc": {"enabled": True, "connected": True, "per_min": 9.0, "first_per_min": 9.0, "last_ts": 998}}}),))
    Q._SNIPER_HIST.clear()
    s = Q.sniper_stream(conn, now=1005)
    Q._SNIPER_HIST.clear()
    assert s["connected"] is True and s["active_launch_source"] == "rpc" and s["launches_per_min"] == 9.0
    assert [x["source"] for x in s["launch_sources"]] == ["pumpportal", "rpc"]


def test_system_health_backup_and_errors(demo, empty, tmp_path, monkeypatch):
    import time as _t

    from web import queries as Q
    from web.server import app

    s = _client(demo).get("/api/system").json()
    names = {r["name"]: r for r in s["health"]}
    assert {"signals", "copytrade", "sniper", "web"} <= set(names)
    assert all(r["state"] in ("ok", "langsam", "steht", "neu") for r in s["health"])
    assert names["web"]["state"] == "ok" and "backup" in s
    # copy-trading is alive when its price loop writes equity, even with no wallet polled (only Modus 2 on)
    import sqlite3

    eq = sqlite3.connect(demo).execute("SELECT MAX(ts) FROM ct_equity").fetchone()[0]
    assert eq and names["copytrade"]["last_ts"] >= eq
    e = _client(empty).get("/api/system").json()
    assert all(r["state"] in ("neu", "ok") for r in e["health"])
    # backups: newest archive by name, never opened; watchdog problems passed through
    bd = tmp_path / "bk"
    bd.mkdir()
    (bd / "mayhambot-20261016-0100.tar.gz").write_bytes(b"x" * 10)
    (bd / "mayhambot-20261016-1300.tar.gz").write_bytes(b"x" * 20)
    monkeypatch.setenv("BACKUP_DIR", str(bd))
    b = Q.backup_status(now=_t.time() + 60)
    assert b["last_name"] == "mayhambot-20261016-1300.tar.gz" and b["count"] == 2 and b["ok"] and b["last_size"] == 20
    assert not Q.backup_status(now=_t.time() + 4 * 3600)["ok"]
    # a view that hits a missing table answers 503 with a German message instead of a bare 500
    import sqlite3 as _s

    @app.get("/api/_test_missing")
    def _missing():
        raise _s.OperationalError("no such table: intel_quotes")

    app.router.routes.insert(0, app.router.routes.pop())  # ahead of the SPA catch-all
    try:
        r = _client(empty).get("/api/_test_missing")
        assert r.status_code == 503 and "Daten fehlen noch" in r.json()["detail"] and r.json()["missing"] == "intel_quotes"
    finally:
        app.router.routes[:] = [x for x in app.router.routes if getattr(x, "path", "") != "/api/_test_missing"]


def test_pid_states(tmp_path, monkeypatch):
    from web import queries as Q

    assert Q._pid_running("gibtsnicht-xyz") is None


def test_cache_swr_bg_and_last_ts(tmp_path):
    import threading
    import time as _t

    from web.db import TTLCache
    from web import queries as Q

    c = TTLCache()
    calls = []
    assert c.get_swr(("k",), 0.05, lambda: calls.append(1) or 1) == 1        # first call computes inline
    _t.sleep(0.06)
    ev = threading.Event()
    assert c.get_swr(("k",), 0.05, lambda: (ev.wait(1), 2)[1]) == 1           # stale: old value at once
    ev.set()
    for _ in range(50):
        if c._d[("k",)][1] == 2:
            break
        _t.sleep(0.01)
    assert c._d[("k",)][1] == 2
    assert c.get_bg(("b",), 10, lambda: 5) is None                            # never blocks
    for _ in range(50):
        if c.get_bg(("b",), 10, lambda: 6) == 5:
            break
        _t.sleep(0.01)
    assert c.get_bg(("b",), 10, lambda: 6) == 5
    # newest trade without an index on ts: from the newest inserts (own DB file: has() must see its schema)
    conn = sqlite3.connect(str(tmp_path / "t.db"))
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE trades(sig TEXT, wallet TEXT, mint TEXT, side TEXT, ts INTEGER, PRIMARY KEY(sig, wallet, mint, side))")
    conn.executemany("INSERT INTO trades VALUES (?,?,?,?,?)", [(str(i), "w", "m", "buy", 1000 + i) for i in range(500)])
    Q.cache.clear()
    assert not Q._has_index(conn, "trades", "ts") and Q.last_ts(conn, "trades") == 1499
    conn.execute("CREATE INDEX ix ON trades(ts)")
    Q.cache.clear()
    assert Q._has_index(conn, "trades", "ts") and Q.last_trade_ts(conn) == 1499


def test_safety_quality(demo, empty):
    s = _client(demo).get("/api/system").json()["safety_quality"]
    assert s and s["backtest"]["best"] == "learned" and [x["key"] for x in s["backtest"]["scores"]] == ["learned", "rules", "sniper"]
    lr = s["backtest"]["scores"][0]
    assert lr["recall"] == round(208 / 351, 3) and lr["auc"] == 0.81 and lr["doublers_flagged"] == 4
    assert {x["level"] for x in s["levels"]} == {"low", "caution", "high", "critical"}
    assert _client(empty).get("/api/system").json()["safety_quality"] is None


def test_cross_site_writes_refused(demo):
    c = _client(demo)
    body = b'{"on": true}'
    evil = c._call("POST", "/api/control/pause", body, [(b"origin", b"https://evil.example"),
                                                        (b"content-type", b"text/plain")])
    assert evil.status_code == 403
    form = c._call("POST", "/api/control/pause", body, [(b"referer", b"https://evil.example/x")])
    assert form.status_code == 403
    rebound = c._call("POST", "/api/control/pause", body, [(b"host", b"evil.example:8600")])
    assert rebound.status_code == 403
    same = c._call("POST", "/api/control/pause", b'{"on": false}', [(b"origin", b"http://127.0.0.1:8600"),
                                                                     (b"content-type", b"application/json")])
    assert same.status_code == 200


def test_overview_board(demo, empty):
    b = _client(demo).get("/api/overview").json()["board"]
    names = [r["name"] for r in b["rows"]]
    assert names[:4] == ["signals", "copytrade", "safety", "sniper"] and "research" in names
    assert all(r["page"].startswith("/") and r["headline"] for r in b["rows"])
    assert b["worst"] in ("ok", "langsam", "steht", "neu")
    e = _client(empty).get("/api/overview").json()["board"]
    assert all(r["state"] in ("neu", "ok") for r in e["rows"]) and not e["problems"]


def test_schema_cache_per_database(tmp_path):
    from web.db import has, tables

    a, b = sqlite3.connect(str(tmp_path / "a.db")), sqlite3.connect(str(tmp_path / "b.db"))
    a.execute("CREATE TABLE x(ts INTEGER)")
    assert has(a, "x", "ts") and not has(b, "x")          # two files never share an entry
    b.execute("CREATE TABLE x(ts INTEGER, y TEXT)")
    assert has(b, "x", "y") and not has(a, "x", "y")
    a.execute("ALTER TABLE x ADD COLUMN y TEXT")             # schema_version bump: seen at once, no 20 s wait
    assert has(a, "x", "y") and "x" in tables(a)


def test_autoupdate_red_streak(tmp_path, monkeypatch):
    import json

    from web import queries as Q

    (tmp_path / "data").mkdir()
    monkeypatch.setattr(Q, "ROOT_DIR", str(tmp_path))
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "none"))
    st = {"deployed": "abcdef1234", "last_deploy": 1, "red_streak": 1}
    (tmp_path / "data" / "autoupdate_state.json").write_text(json.dumps(st))
    b = Q.backup_status()
    assert b["autoupdate"]["red_streak"] == 1 and "autoupdate_red" not in b["problems"]
    st.update(red_streak=3, red_tests=["FAILED web/tests/test_api.py::test_x - assert 1 https://h.example/?api-key=K"])
    (tmp_path / "data" / "autoupdate_state.json").write_text(json.dumps(st))
    b = Q.backup_status()
    assert b["problems"]["autoupdate_red"].startswith("Auto-Update hängt: Tests seit 3 Runden rot")
    assert b["autoupdate"]["red_tests"][0].startswith("FAILED web/tests") and "api-key=K" not in b["autoupdate"]["red_tests"][0]
    assert b["autoupdate"]["blocked"] is None and "autoupdate_blocked" not in b["problems"]
    st.update(blocked="Lokaler Commit blockiert das Update: 1 Commit(s) nur auf dem Mac, 2 Commit(s) von main fehlen")
    (tmp_path / "data" / "autoupdate_state.json").write_text(json.dumps(st))
    b = Q.backup_status()
    assert b["autoupdate"]["blocked"].startswith("Lokaler Commit blockiert")
    assert b["problems"]["autoupdate_blocked"].startswith("Auto-Update blockiert: Lokaler Commit")


def test_wallet_copy_edge(demo):
    import time as _t

    c = _client(demo)
    d = c.get("/api/wallets?limit=50").json()
    assert "edges_pending" in d and all("copy_edge" in w for w in d["items"])
    assert any(w.get("net_pnl_usd") is not None for w in d["items"])
    for _ in range(100):                      # computed in the background, then served from the cache
        d = c.get("/api/wallets?limit=50").json()
        if not d["edges_pending"]:
            break
        _t.sleep(0.05)
    with_edge = [w for w in d["items"] if w["copy_edge"]]
    assert with_edge and all({"mean", "lower", "proven", "n"} <= set(w["copy_edge"]) for w in with_edge)
    a = with_edge[0]["address"]
    e = c.get(f"/api/wallets/{a}").json()["edge"]
    assert e["copy"]["n"] == with_edge[0]["copy_edge"]["n"] and e["verdict"]
    assert e["copy"]["proven"] == (e["copy"]["lower"] > 0)


def test_live_buy_capped(demo, monkeypatch):
    from web import server
    c = _client(demo)
    free = next(s for s in c.get("/api/signals").json() if not s["open_position_id"])
    monkeypatch.setattr(server, "_buy_preview", lambda conn, mint: {"account": "live", "size_sol": 0.5, "preset": "x"})
    monkeypatch.setenv("LIVE_MAX_TRADE_SOL", "1")
    r = c.post("/api/buy", json={"mint": free["mint"], "size_sol": 5, "risk_ack": True})
    assert r.status_code == 409 and "LIVE_MAX_TRADE_SOL" in r.json()["detail"]


def test_inflight_position_and_command_states(demo):
    c = _client(demo)
    d = c.get("/api/copytrade?mode=paper").json()
    assert any(p["status"] == "opening" for p in d["open"]) and all(p["status"] != "failed" for p in d["open"])
    assert all(p["status"] == "closed" for p in d["closed"])
    live = c.get("/api/copytrade?mode=live").json()
    assert [p["status"] for p in live["failed"]] == ["failed"] and live["failed"][0]["exit_reason"]
    assert any(x["status"] == "expired" for x in d["commands"])


def test_live_card_and_kill_switch(demo, monkeypatch):
    monkeypatch.setenv("LIVE_TRADING", "true")
    monkeypatch.delenv("LIVE_DRY_RUN", raising=False)
    from web.db import cache

    cache.clear()
    c = _client(demo)
    d = c.get("/api/live").json()
    assert d["available"] and d["state"] == "dry" and not d["killed"]
    assert {"wallet", "dry_run", "clean"} <= {x["key"] for x in d["checks"]} and d["limits"]["max_position_sol"] > 0
    st = {o["status"]: o["status_de"] for o in d["orders"]}
    assert st["dry_ok"] == "Dry-Run simuliert ok" and st["rejected"] == "vor dem Senden abgelehnt" and d["attention"] == {}
    body = str(d)
    assert "DemoTradingWallet1111" not in body  # only the first 6 characters of the trading wallet
    r = c.post("/api/live/kill", json={"reason": "Test"})
    assert r.status_code == 200
    assert c.get("/api/live").json()["killed"].startswith("Dashboard: Test")
    assert c.post("/api/live/unkill", json={"confirm": "FREIGEBEN"}).status_code in (404, 405)  # release stays off the dashboard
    assert c.get("/api/control").json()["live_dry_run"] is True
    assert c.get("/api/copytrade?mode=dry").status_code == 200


def test_explore_account_separate(demo):
    c = _client(demo)
    d = c.get("/api/copytrade?mode=explore").json()
    assert d["closed"] and all(p["mode"] == "explore" for p in d["closed"]) and d["balance_sol"] is not None
    strict = c.get("/api/copytrade?mode=paper").json()
    assert all(p["mode"] == "paper" for p in strict["closed"]) and strict["balance_sol"] != d["balance_sol"]
    ov = c.get("/api/overview").json()["copytrade"]
    assert ov["explore"]["trades"] == len(d["closed"]) and ov["paper"]["pnl_sol"] != ov["explore"]["pnl_sol"]


def test_buy_needs_rug_ack(demo, monkeypatch):
    from web import server
    c = _client(demo)
    free = next(s for s in c.get("/api/signals").json() if not s["open_position_id"])
    flags = {"level": "high", "score": 71, "reasons": ["Versteckte Verkaufssteuer 11 %", "Wash-Volumen 37 %"]}
    monkeypatch.setattr(server, "_buy_preview", lambda conn, mint: {"account": "paper", "size_sol": 0.5, "preset": "x", "safety": flags})
    r = c.post("/api/buy", json={"mint": free["mint"]})
    assert r.status_code == 409 and "hoch" in r.json()["detail"] and "Verkaufssteuer" in r.json()["detail"]
    monkeypatch.setattr(server, "_buy_preview", lambda conn, mint: {"account": "paper", "size_sol": 0.5, "preset": "x", "safety": None})
    assert "ungeprüft" in c.post("/api/buy", json={"mint": free["mint"]}).json()["detail"]
    monkeypatch.setattr(server, "_buy_preview", lambda conn, mint: {"account": "paper", "size_sol": 0.5, "preset": "x", "safety": {"level": "low"}})
    assert c.post("/api/buy", json={"mint": free["mint"]}).status_code == 200


def test_credit_borrow_state():
    import types

    from web import queries as Q

    fake = types.SimpleNamespace(CREDIT_BURST={}, _ledger_read=lambda: {}, _borrowable=lambda led, svc: 1200.0)
    cred = {"signals": {"credits_day_budget": 1000, "credits_today": 1500},
            "research": {"credits_day_budget": 4000, "credits_today": 500}, "copytrade": {"credits_day_budget": 900, "credits_today": 100}}
    b = Q._credit_borrow(fake, cred, "signals", cred["signals"])
    assert b == {"borrowed": 500, "borrow_left": 1200, "lenders": ["research", "copytrade"]}


def test_signal_plan_and_reach(demo, tmp_path):
    import shutil

    db = str(tmp_path / "plan.db")
    shutil.copy(demo, db)
    con = sqlite3.connect(db)  # a re-call on the first coin at twice the price
    sid, mint, ts, price = con.execute("SELECT id, mint, ts, price_usd FROM signals ORDER BY ts LIMIT 1").fetchone()
    con.execute("INSERT INTO signals(ts, mint, symbol, score, price_usd, call_no) VALUES (?,?,?,?,?,2)", (ts + 1200, mint, "X", 70, price * 2))
    con.commit()
    con.close()
    c = _client(db)
    rows = c.get("/api/signals?since_h=0&limit=1000").json()
    done = [s for s in rows if s["outcome"].get("max_ret_4h") is not None]
    assert done and all("peak_min_1h" in s["outcome"] and s["plan"]["target_ret"] == 0.5 for s in done)
    re = [s for s in rows if s["plan"]["call_no"] > 1]
    assert [s["plan"]["move_since_last"] for s in re] == [1.0]
    m = c.get("/api/signals/performance").json()["manual"]
    assert [x["level"] for x in m["reach"]] == [0.5, 1.0, 1.5]
    r50 = m["reach"][0]["max_ret_4h"]
    assert r50["n"] == len([s for s in rows if s["outcome"].get("max_ret_4h") is not None])
    assert r50["hits"] >= m["reach"][1]["max_ret_4h"]["hits"] >= m["reach"][2]["max_ret_4h"]["hits"]
    planned = [s for s in rows if s["plan"]["target_ret"] is not None]
    assert m["planned"] == len(planned) and m["target_hits"] == sum(1 for s in planned if s["plan"]["target_hit_ts"])


def test_summary_realized_plus_open():
    from web import queries as Q

    ps = [Q._pos({"status": "closed", "pnl_sol": -1.0, "pnl_pct": -0.5, "closed_at": 1, "opened_at": 0}),
          Q._pos({"status": "closed", "pnl_sol": 0.5, "pnl_pct": 0.25, "closed_at": 2, "opened_at": 1}),
          Q._pos({"status": "stuck", "entry_price_sol": 1.0, "last_price_sol": 2.0, "tokens": 1.0, "entry_cost_sol": 1.0, "opened_at": 3})]
    s = Q._summary(ps)
    assert s["open"] == 1 and s["realized_sol"] == -0.5
    assert s["unrealized_sol"] == round(2 * (1 - Q.EXIT_COST) - 1, 4) and s["total_sol"] == round(-0.5 + s["unrealized_sol"], 4)
    assert s["max_drawdown_sol"] == -1.0  # the curve starts at 0, so the first loss counts


def test_signal_hit_rate_counts_unmeasured(demo, tmp_path):
    import shutil
    import time as _t

    db = str(tmp_path / "hr.db")
    shutil.copy(demo, db)
    con = sqlite3.connect(db)
    con.execute("INSERT INTO signals(ts, mint, symbol, score) VALUES (?, 'Unmeasured1111111111111111111111111pump', 'U', 50)", (int(_t.time()) - 7200,))
    con.commit()
    con.close()
    p = _client(db).get("/api/signals/performance").json()
    assert p["unmeasured"] >= 1 and p["hit_rate"] == round(p["hits"] / p["n"], 3)


def test_learning_progress(demo):
    d = _client(demo).get("/api/learning").json()
    assert d["available"] and d["target"] == 100 and d["rows"]
    accts = {(r["strategy"], r["account"]) for r in d["rows"]}
    assert ("whale", "paper") in accts and ("whale", "explore") in accts
    for r in d["rows"]:
        assert 0 <= r["oos_trades"] <= r["trades"] and 0 <= r["progress"] <= 1
    assert any(r["gate"] for r in d["rows"] if r["account"] == "paper")


def test_local_day0():
    import time as _t
    from web import queries as Q

    now = 1_760_000_000
    d0 = Q.local_day0(now)
    assert d0 <= now < d0 + 25 * 3600 and _t.localtime(d0)[3:6] == (0, 0, 0)
    assert _t.localtime(d0)[:3] == _t.localtime(now)[:3]


def test_brain_view(demo, empty, tmp_path):
    """Entscheider page: account, honest scorecard, decisions with their reasons; empty brain tables are fine."""
    import sqlite3

    from web import queries as Q

    assert _client(empty).get("/api/brain").json()["available"] is False
    c = _client(demo)
    d = c.get("/api/brain").json()
    assert d["available"] and d["decisions"] and d["equity"] and len(d["plans"]) == 6
    a = d["account"]
    assert abs(a["equity_sol"] - (a["balance_sol"] + a["open_value_sol"])) < 1e-3 and a["start_sol"] == 5.0
    dec = d["decisions"][0]
    assert 2 <= len(dec["note"]) <= 6 and dec["verdict"] and dec["kind_label"] in ("Wal-Kauf", "Signal", "Launch")
    h = d["honest"]
    assert h["target"] == 100 and h["n"] >= h["n_taken"] and 0 <= h["progress"] <= 1
    assert all(x["decision"] == "enter" for x in c.get("/api/brain?show=enter").json()["decisions"])
    assert all(x["decision"] != "enter" for x in c.get("/api/brain?show=skip").json()["decisions"])
    assert all(x["kind"] == "whale" for x in c.get("/api/brain?kind=whale").json()["decisions"])
    assert c.get("/api/brain?show=x").status_code == 400
    # goal, day factors, selection lead, capture, exit comparison, segments, regime markers, partial sells
    assert d["goal"]["mult"] == [2, 4] and len(d["days"]) == 14 and any(x.get("mult") for x in d["days"])
    assert any(x.get("alpha") is not None for x in d["days"]) and any(x.get("capture_organic") is not None for x in d["days"])
    assert d["exits"]["replays"]["n"] > 0 and {e["mode"] for e in d["exits"]["by_exit"]} <= {"organic", "plan"}
    assert d["segments"]["by_liq"] and d["regimes"] and any(x.get("regimes") for x in d["days"])
    held = d["open"] + d["closed"]
    assert {p["exit_mode"] for p in held} <= {"organic", "plan"}
    for p in d["open"]:
        if p.get("value_sol") is not None and p.get("tokens"):
            assert abs(p["value_sol"] - p["last_price_sol"] * p["tokens"]) < 1e-3  # remaining tokens, as brain counts it
    assert any(b["name"] == "brain" for b in c.get("/api/overview").json()["board"]["rows"])
    # brain created its tables but has not decided anything yet: every number may be empty
    p = str(tmp_path / "fresh.db")
    conn = sqlite3.connect(p)
    from brain import store

    store.init(conn)
    conn.commit()
    conn.row_factory = sqlite3.Row
    v = Q.brain_view(conn)
    assert v["available"] and v["decisions"] == [] and v["account"]["equity_sol"] == 5.0 and v["honest"]["n"] == 0
    assert v["goal"]["today"] is None and v["exits"]["by_exit"] == [] and v["segments"]["by_delay"] == [] and v["regimes"] == []
    from cli.report import brain_line

    assert "noch nichts nachgeprüft" in brain_line(v) and brain_line(None) == ""
