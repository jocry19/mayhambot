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
       "/api/copytrade/backtest?strategy=whale", "/api/copytrade/learn?strategy=whale", "/api/copytrade/learn?strategy=signal", "/api/safety"]


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
    r = c.post("/api/buy", json={"mint": free["mint"], "strategy": "signal"})
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

    write("safety", now - 300, 100, 20)
    write("research", now - 4000, 10, 1)
    c = _client(demo)
    u = c.get("/api/system").json()["api_usage"]
    assert u["available"]
    write("safety", now, 400, 54, {"credits": 900, "refused": 7})
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
