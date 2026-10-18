import time

from copytrade import prices

from test_copytrade import MINT, W, env  # noqa: F401  (fixture)


def test_last_price_from_the_swaps_already_read(env):
    s, conn, ex, state = env
    now = time.time()
    assert prices.last_price(conn, MINT, 60, now) is None
    conn.execute("INSERT INTO ct_swaps(sig, wallet, mint, side, ts, token_amount, sol_amount, price_sol) "
                 "VALUES ('a', ?, ?, 'buy', ?, 1000, 1.0, 0.001)", (W, MINT, int(now - 30)))
    conn.execute("INSERT INTO ct_swaps(sig, wallet, mint, side, ts, token_amount, sol_amount, price_sol) "
                 "VALUES ('d', ?, ?, 'buy', ?, 1, 0.001, 9.0)", (W, MINT, int(now - 5)))  # dust: ignored
    conn.execute("INSERT INTO ct_swaps(sig, wallet, mint, side, ts, token_amount, sol_amount, price_sol) "
                 "VALUES ('old', ?, ?, 'buy', ?, 1000, 1.0, 0.5)", (W, MINT, int(now - 600)))  # too old
    try:
        conn.execute("INSERT INTO trades(sig, wallet, mint, side, ts, token_amount, sol_amount, source) "
                     "VALUES ('t', 'X', ?, 'sell', ?, 1000, 1.2, 'signals')", (MINT, int(now - 10)))
        has_trades = True
    except Exception:
        has_trades = False
    lp = prices.last_price(conn, MINT, 60, now)
    if has_trades:
        assert lp["source"] == "trades" and abs(lp["price_sol"] - 0.0012) < 1e-12 and lp["n"] == 2
        assert abs(lp["median_sol"] - 0.0011) < 1e-12
    else:
        assert lp["source"] == "ct_swaps" and lp["price_sol"] == 0.001 and lp["n"] == 1
    assert prices.last_price(conn, MINT, 5, now) is None
