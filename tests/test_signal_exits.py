from core import db
from signals import exits

NOW = 4_000_000
# dips 25 % first (the median call dips ~21 % before its high), then 2.2x, then crashes
PATH = [(60, 0.9), (300, 0.75), (900, 1.3), (1500, 1.6), (2100, 2.2), (2700, 1.5), (3500, 0.6)]


def test_tight_stop_and_early_trail_miss_the_move():
    r = exits.RULES
    assert exits.simulate(PATH, 1.0, r["paper now (tp+50 sl-20 trail 20/12)"]) == -0.2   # stopped out in the dip
    assert exits.simulate(PATH, 1.0, r["tp+50 sl-35, no trail"]) == 0.5
    staged = exits.simulate(PATH, 1.0, r["half at +50, rest stop at entry, trail 25 % from high"])
    assert round(staged, 3) == round(0.5 * 0.5 + 0.5 * 0.5, 3)    # rest trailed out at 1.5 (2.2 high, -25 % -> 1.65)
    assert exits.simulate(PATH, 1.0, r["hold 1 h (no exits)"]) == 0.6 - 1


def test_dead_coin_books_dead_ret_and_compare_runs(tmp_path):
    assert exits.simulate([], 1.0, exits.RULES["tp+50 sl-35, no trail"], dead=True) == exits.DEAD_RET
    c = db.connect(str(tmp_path / "e.db"))
    t = NOW - 6 * 3600
    c.execute("INSERT INTO signals(ts, mint, symbol, score, price_usd, outcome_flag) VALUES (?,?,?,?,?,?)",
              (t, "A", "AAA", 50, 1.0, "ok"))
    for dt, p in PATH:
        c.execute("INSERT INTO token_prices(mint, ts, price_usd) VALUES ('A',?,?)", (t + 60 + dt, p))
    c.commit()
    res = exits.compare(c, 1, NOW)
    assert res["tp+50 sl-35, no trail"]["n"] == 1 and res["tp+50 sl-35, no trail"]["mean_after_cost"] == 0.44
