"""/api/intel on the dashboard's synthetic DB (web.devdata): empty before the first round, full after one."""
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def _client(db):
    os.environ["MAYHAM_DB_PATH"] = db
    os.environ.pop("WEB_PASSWORD", None)
    from web import db as wdb
    from web.server import app
    from web.tests.asgi import Client
    wdb.cache.clear()
    return Client(app, client=("127.0.0.1", 5000))


def test_intel_api_empty_and_after_a_round(tmp_path):
    empty = str(tmp_path / "empty.db")
    sqlite3.connect(empty).close()
    for url in ("/api/intel", "/api/intel/coin/So11111111111111111111111111111111111111112"):
        r = _client(empty).get(url)
        assert r.status_code == 200 and r.json()["available"] is False

    from web import devdata
    from intel import base, service
    demo = str(tmp_path / "demo.db")
    devdata.build(demo, days=1.0, n_tokens=10, n_wallets=200)
    base.clear_cache()
    s = service.Service(db_path=demo)
    t0 = time.time()
    out = s.round()
    assert time.time() - t0 < 30
    assert out["coins"] > 0
    d = _client(demo).get("/api/intel").json()
    assert d["available"] and d["coins"] and d["sources"]
    mint = d["coins"][0]["mint"]
    c = _client(demo).get(f"/api/intel/coin/{mint}").json()
    assert c["available"] and c["groups"]
