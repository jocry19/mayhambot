"""Shared test setup: the copy trader runs safety.assess before every buy; tests have no network, so a quick
check answers 'low' unless the coin was blocked by hand (safety.block). Tests that need another answer patch
safety.assess themselves."""
import time

import pytest


@pytest.fixture(autouse=True)
def _offline_safety_check(request, monkeypatch):
    if not request.module.__name__.split(".")[-1].startswith("test_copytrade"):
        yield
        return
    from copytrade import strategy

    strategy._lb_cache.clear()  # per-wallet cache must not leak between test databases
    try:
        import safety
    except Exception:
        yield
        return

    def assess(mint, conn=None, deep=True, **k):
        blocked = bool(conn is not None and safety.is_blocked(conn, mint))
        reasons = [{"check": "override", "risk": 100, "text": "manuell gesperrt"}] if blocked else []
        return safety.Assessment(mint, int(time.time()), 90.0 if blocked else 10.0, "critical" if blocked else "low",
                                 blocked, 1.0, reasons=reasons)

    monkeypatch.setattr(safety, "assess", assess)
    yield
