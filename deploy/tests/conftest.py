import pytest


@pytest.fixture(autouse=True)
def _no_real_telegram(monkeypatch):
    """core.config / live.config load the Mac's .env into os.environ (python-dotenv) when another test imports
    them, and ops.load_env lets TELEGRAM_* from the environment win. Without this, deploy tests would read the
    real bot and chat on the Mac (and could message the user from the update gate)."""
    import os
    for k in list(os.environ):
        if k.startswith(("TELEGRAM_", "LIVE_", "AUTO_UPDATE", "WATCHDOG_", "ALERT_", "BACKUP_")):
            monkeypatch.delenv(k, raising=False)
