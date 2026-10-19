"""Shadow book "schneller Einstieg": fill 2 to 5 s after the create with its 20 s twin, trail exit, rug check first."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from sniper import research, shadow  # noqa: E402
from sniper.engine import Engine  # noqa: E402
from sniper.tests.test_sniper import T0, Curve, cfg, conn  # noqa: E402,F401


def _run(eng, cv, t, n=1, sol=0.4, wallet="B"):
    for i in range(n):
        eng.handle(cv.buy(f"{wallet}{t:.0f}{i}", sol), t)


def test_fast_entry_and_its_20s_twin_trail_out_and_hold_an_hour(conn):  # noqa: F811
    eng = Engine(cfg(), conn)
    cv = Curve("FAST1")
    eng.handle(cv.create(0.5), T0)
    eng.handle(cv.buy("W1", 0.3), T0 + 0.5)
    eng.tick(T0 + 2.1)                          # rug check at 2 s
    assert eng.shadow.counts["checked"] == 1 and not eng.shadow.open
    eng.tick(T0 + 3.2)                          # filled one latency later
    fast = eng.shadow.open[("FAST1", "fast")]
    assert 3.0 <= fast["fill_age_sec"] <= 5 and fast["decide_ts"] == T0 + 2.1
    assert fast["fill_price_sol"] > fast["spot_price_sol"] and 0 < fast["buy_cost_pct"] < 0.1   # fee and slippage
    for k in range(4, 21):                      # the curve runs up
        _run(eng, cv, T0 + k, n=2, sol=1.0)
        eng.tick(T0 + k + 0.5)
    eng.tick(T0 + 21.5)
    assert ("FAST1", "decide") in eng.shadow.open
    twin = eng.shadow.open[("FAST1", "decide")]
    assert twin["fill_price_sol"] > fast["fill_price_sol"] and twin["decide_ts"] == T0 + 20.5
    for k in range(22, 40):
        _run(eng, cv, T0 + k, n=2, sol=1.5)
    top = cv.v_sol
    while cv.v_sol > 0.6 * top:                 # dump from the high: the trail fires for both
        eng.handle(cv.sell("DUMP", 30_000_000), T0 + 45)
    assert fast.get("exit_reason") == "trail" and twin.get("exit_reason") == "trail"
    eng.tick(T0 + 3.2 + 3600)                   # the fast leg ends after its hour, its row is written
    eng.tick(T0 + 21.5 + 3600)
    rows = {r["leg"]: dict(r) for r in conn.execute("SELECT * FROM sniper_shadow")}
    assert set(rows) == {"fast", "decide"} and not eng.shadow.open
    f, d = rows["fast"], rows["decide"]
    assert f["pnl_pct"] > d["pnl_pct"] and f["x2"] == 1 and f["ret_60m"] is not None and f["end_src"] == "bonding_curve"
    assert f["fill_ts"] - f["decide_ts"] >= 1 and d["fill_ts"] - d["created_ts"] >= 20
    rep = shadow.report(conn)
    assert rep["pairs"]["fast"]["n"] == 1 and rep["pairs"]["decide"]["n"] == 1 and rep["exits"] == {"trail": 1}
    lines = research.summary(dict(research.run(conn, cfg()), shadow=rep)).splitlines()
    assert any(line.startswith("Schatten-Einstieg") and "20-s-Entscheid" in line for line in lines)


def test_rug_check_runs_before_the_fast_entry(conn):  # noqa: F811
    eng = Engine(cfg(), conn)
    cv = Curve("MASCHE")
    eng.handle(cv.create(80.0), T0)             # the dev buys ~77 % of the curve himself
    eng.tick(T0 + 2.0)
    eng.tick(T0 + 3.5)
    assert sum(eng.shadow.blocked.values()) == 1 and not eng.shadow.open
    assert eng.shadow.counts["filled_fast"] == 0


def test_no_fill_after_five_seconds_and_off_switch(conn):  # noqa: F811
    eng = Engine(cfg(), conn)
    cv = Curve("LATE1")
    eng.handle(cv.create(0.5), T0)
    eng.tick(T0 + 6)                            # the loop first sees it after 5 s: no fast entry
    eng.tick(T0 + 7)
    assert eng.shadow.counts["late"] == 1 and not eng.shadow.open
    assert Engine(cfg(shadow=False), conn).shadow is None
