import sqlite3

from core import db


def test_retry_locked_retries_then_succeeds(monkeypatch):
    monkeypatch.setattr(db.time, "sleep", lambda s: None)
    conn = sqlite3.connect(":memory:")
    calls = []

    def step():
        calls.append(1)
        if len(calls) < 3:
            raise sqlite3.OperationalError("database is locked")
        return 7
    assert db.retry_locked(conn, step) == 7 and len(calls) == 3


def test_retry_locked_reraises_other_errors():
    conn = sqlite3.connect(":memory:")
    try:
        db.retry_locked(conn, lambda: conn.execute("SELECT * FROM nope"))
    except sqlite3.OperationalError as e:
        assert "no such table" in str(e)
    else:
        raise AssertionError("expected error")
