"""Expected return after costs per signal candidate, learned from what similar setups really did.

A candidate falls into a bucket by (distinct smart entities, net smart inflow, smart sellers). Each bucket's EV is
the mean take-profit return after costs of the replayed setups in it, shrunk toward the overall mean
(weight n / (n + SHRINK)), so a bucket with three lucky coins does not dominate.

calibrate() replays the last CALIB_HOURS with signals.backtest (point-in-time tiers), fits the buckets on the first
two thirds and checks the rule "EV > SIGNAL_MIN_EV" on the last third it never saw. The table only goes active when
that out-of-sample check made money after costs; otherwise the fixed thresholds stay in charge. The final table is
then refit on all data. Runs every CALIB_EVERY_HOURS inside the signals service; one row per run in signal_calibration.
"""
import json
import logging
import os
import time

from signals import backtest

log = logging.getLogger("mayhambot.signals.ev")
SHRINK = float(os.getenv("SIGNAL_EV_SHRINK", 10))
MIN_EV = float(os.getenv("SIGNAL_MIN_EV", 0.0))
CALIB_HOURS = float(os.getenv("SIGNAL_CALIB_HOURS", 72))
CALIB_EVERY_HOURS = float(os.getenv("SIGNAL_CALIB_EVERY_HOURS", 6))
MIN_OOS_N = int(os.getenv("SIGNAL_CALIB_MIN_OOS_N", 10))

SCHEMA = """CREATE TABLE IF NOT EXISTS signal_calibration (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, hours REAL, n_train INTEGER, n_all INTEGER,
    active INTEGER, table_json TEXT, oos_json TEXT, notes TEXT)"""


def bucket(entities, net_usd, sellers):
    e = "1" if entities <= 1 else "2" if entities == 2 else "3+"
    n = "<200" if net_usd < 200 else "<500" if net_usd < 500 else ">=500"
    s = "0" if not sellers else "1+"
    return f"e{e}|n{n}|s{s}"


def fit(results, cost=backtest.COST, shrink=SHRINK):
    """results: replayed setups with entities, net, sellers and tp_ret. -> {global, n, buckets {key: {n, mean, ev}}}"""
    if not results:
        return dict(global_ev=None, n=0, buckets={})
    rets = [r["tp_ret"] - cost for r in results]
    g = sum(rets) / len(rets)
    by = {}
    for r, x in zip(results, rets):
        by.setdefault(bucket(r["entities"], r["net"], r["sellers"]), []).append(x)
    buckets = {k: dict(n=len(v), mean=round(sum(v) / len(v), 4),
                       ev=round((sum(v) + shrink * g) / (len(v) + shrink), 4)) for k, v in by.items()}
    return dict(global_ev=round(g, 4), n=len(results), buckets=buckets)


def predict(table, entities, net_usd, sellers):
    """(ev, n, bucket key). Unknown bucket: the global mean, n 0."""
    if not table or table.get("global_ev") is None:
        return None, 0, None
    k = bucket(entities, net_usd, sellers)
    b = table["buckets"].get(k)
    return (b["ev"], b["n"], k) if b else (table["global_ev"], 0, k)


BASE_RULE = dict(wallets=1, net=0, score=0, p=None, sellers=None)  # every setup the EV table may later judge


def calibrate(conn, hours=CALIB_HOURS, now=None, cost=backtest.COST, min_ev=MIN_EV, min_oos_n=MIN_OOS_N):
    now = now or int(time.time())
    conn.execute(SCHEMA)
    window = backtest.settings.signal_window_min * 60
    since, until = now - int(hours * 3600), now - 3600
    data = backtest.load(conn, since, until, window)
    snaps = backtest.snapshots(data, since, until, window)
    res = backtest.replay(snaps, data["prices"], BASE_RULE)
    res.sort(key=lambda r: r["t"])
    cut = len(res) * 2 // 3
    train, test = res[:cut], res[cut:]
    t_train = fit(train, cost)
    picked = [r for r in test if (predict(t_train, r["entities"], r["net"], r["sellers"])[0] or -1) > min_ev]
    oos = backtest.summary(picked, cost)
    oos["baseline_all"] = backtest.summary(test, cost)
    active = int(oos["n"] >= min_oos_n and oos["mean_tp"] > 0)
    table = fit(res, cost)
    notes = (f"{len(res)} replayed setups, pit tiers {data['pit']}; out of sample {oos['n']} picked, mean "
             f"{oos['mean_tp']:+.1%} vs all {oos['baseline_all'].get('mean_tp', 0):+.1%}" if oos["n"]
             else f"{len(res)} replayed setups, nothing picked out of sample")
    conn.execute("INSERT INTO signal_calibration(ts, hours, n_train, n_all, active, table_json, oos_json, notes) "
                 "VALUES (?,?,?,?,?,?,?,?)", (now, hours, len(train), len(res), active, json.dumps(table),
                                               json.dumps(oos), notes))
    conn.commit()
    log.info("signal EV calibration: %s -> %s", notes, "ACTIVE" if active else "not active (fixed thresholds stay)")
    return dict(active=bool(active), table=table, oos=oos, notes=notes)


_cache = {"id": None, "table": None}


def active_table(conn):
    """The newest calibration if it passed its out-of-sample check and is younger than 2 calibration periods."""
    try:
        r = conn.execute("SELECT id, ts, active, table_json FROM signal_calibration ORDER BY id DESC LIMIT 1").fetchone()
    except Exception:  # noqa: BLE001 - table not created yet
        return None
    if not r or not r[2] or time.time() - r[1] > 2 * CALIB_EVERY_HOURS * 3600 + 3600:
        return None
    if _cache["id"] != r[0]:
        _cache.update(id=r[0], table=json.loads(r[3]))
    return _cache["table"]


def due(conn, now=None):
    now = now or int(time.time())
    try:
        r = conn.execute("SELECT MAX(ts) FROM signal_calibration").fetchone()
    except Exception:  # noqa: BLE001
        return True
    return not r or not r[0] or now - r[0] >= CALIB_EVERY_HOURS * 3600
