"""Keep web/cli tests off the real checkout's state: the auto-update gate runs this suite inside the live folder
on the Mac, where data/ (autoupdate/watchdog state, pids, rpc pool, credit ledger), logs/ and the backups hold
the real, possibly red, state. Every test sees an empty temp root instead."""
import pytest


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    from web import queries as Q
    from web.db import cache

    root = tmp_path / "root"
    (root / "data").mkdir(parents=True)
    (root / "logs").mkdir()
    monkeypatch.setattr(Q, "ROOT_DIR", str(root))
    monkeypatch.setenv("MAYHAM_LOG_DIR", str(root / "logs"))
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "backups"))
    try:
        from core import http as chttp

        (root / "data" / "ratelimit").mkdir()
        monkeypatch.setattr(chttp, "_LOCK_DIR", str(root / "data" / "ratelimit"))
    except Exception:
        pass
    cache.clear()
    yield
    cache.clear()
