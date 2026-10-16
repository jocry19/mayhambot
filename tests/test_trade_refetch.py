from signals.run import REFETCH_MAX_SECONDS, due_for_trades, refetch_interval


def row(mint, last, m5=0, h1=0):
    return {"mint": mint, "last_trades_fetch": last, "txns_m5": m5, "txns_h1": h1}


def test_interval_follows_activity():
    assert refetch_interval(row("q", 1), 90) == REFETCH_MAX_SECONDS          # no trades -> rarely
    assert refetch_interval(row("b", 1, m5=500), 90) == 90                    # busy -> every cycle
    assert 90 < refetch_interval(row("m", 1, h1=1200), 90) < REFETCH_MAX_SECONDS


def test_due_order_and_skip():
    now = 10_000
    watch = [row("quiet", now - 100), row("new", 0), row("busy", now - 200, m5=600), row("old", now - 2000)]
    assert [r["mint"] for r in due_for_trades(watch, now, 90)] == ["new", "old", "busy"]
