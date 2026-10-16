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
       "/api/copytrade/backtest?strategy=whale", "/api/copytrade/learn?strategy=whale", "/api/copytrade/learn?strategy=signal"]


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
