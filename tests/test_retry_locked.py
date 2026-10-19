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


def test_watch_writes_names_a_long_transaction_and_a_long_statement(monkeypatch, caplog):
    clock = {"t": 100.0}
    monkeypatch.setattr(db.time, "monotonic", lambda: clock["t"])
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t(x)")
    db.watch_writes(conn, warn_s=10)
    caplog.set_level("WARNING", logger="core.db")
    conn.execute("INSERT INTO t VALUES (1)")   # implicit BEGIN
    clock["t"] += 3
    conn.commit()
    assert not caplog.records                  # short: nothing
    conn.execute("INSERT INTO t VALUES (2)")
    clock["t"] += 12                           # e.g. a network call before the commit
    conn.commit()
    assert len(caplog.records) == 1 and "Transaktion" in caplog.text and "INSERT INTO t VALUES (2)" in caplog.text
    caplog.clear()
    conn.isolation_level = None                # autocommit: one long statement

    def slow(*a):
        clock["t"] += 1
        return 1
    conn.create_function("slow", 1, slow)
    conn.execute("INSERT INTO t SELECT slow(value) FROM (WITH RECURSIVE c(value) AS (SELECT 1 UNION ALL "
                 "SELECT value + 1 FROM c WHERE value < 200000) SELECT value FROM c)")
    assert len(caplog.records) == 1 and "Anweisung" in caplog.text
