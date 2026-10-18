import json

from core import db
from market import store
from signals import exits, narrative

NOW = 5_000_000


def _conn(tmp_path):
    c = db.connect(str(tmp_path / "n.db"))
    store.ensure_schema(c)
    return c


def _term(c, term, stage, heat, mints, ts=NOW - 300):
    c.execute("INSERT INTO market_terms(ts, term, n_recent, heat, mints_json, stage) VALUES (?,?,?,?,?,?)",
              (ts, term, 12, heat, json.dumps(mints), stage))


def _tok(c, mint, name, vol, liq=20000, upd=NOW - 60):
    c.execute("INSERT INTO tokens(mint, symbol, name, price_usd, liquidity_usd, volume_h1, last_updated, tracked) "
              "VALUES (?,?,?,?,?,?,?,0)", (mint, "X", name, 0.001, liq, vol, upd))


def test_leader_of_a_new_narrative_is_booked_once_and_not_reported(tmp_path, monkeypatch):
    from signals import shadow
    seen = []
    monkeypatch.setattr(shadow, "report_opportunity", lambda *a, **k: seen.append(a))
    c = _conn(tmp_path)
    _term(c, "roadster", "new", 70, ["A", "B", "C", "D"])
    _term(c, "doge", "mature", 90, ["E"])                       # old narrative: not a signal
    _term(c, "pizza", "growing", 20, ["F"])                     # too cold
    _tok(c, "A", "Tesla Roadster", 5000)
    _tok(c, "B", "Roadster Inu", 90000)                         # most volume: the leader
    _tok(c, "C", "Roadster Cat", 1e6, liq=500)                  # not tradable
    _tok(c, "D", "Something Else", 1e6)                         # does not carry the term
    _tok(c, "E", "Doge", 1e6)
    _tok(c, "F", "Pizza", 1e6)
    c.commit()
    assert set(narrative.watch_mints(c, NOW)) == {"A", "B", "C", "D"}
    assert narrative.scan(c, 8000, NOW) == 2
    rows = dict(c.execute("SELECT mint, reason FROM signal_shadow WHERE kind='narrative'").fetchall())
    assert rows == {"B": "narrative new leader", "A": "narrative new"}
    assert narrative.scan(c, 8000, NOW + 3600) == 0             # once per coin and day
    assert seen == []                                           # unproven: not for the brain


def test_stale_snapshot_books_nothing_and_ready_needs_cases(tmp_path):
    c = _conn(tmp_path)
    _term(c, "roadster", "new", 70, ["A"], ts=NOW - 7200)
    _tok(c, "A", "Roadster", 5000)
    c.commit()
    assert narrative.hot_terms(c, NOW) == [] and narrative.scan(c, 8000, NOW) == 0
    r = narrative.ready(c, NOW)
    assert r["ready"] is False and r["n"] == 0


def test_shadow_leaders_replay_like_calls(tmp_path):
    c = _conn(tmp_path)
    t = NOW - 6 * 3600
    c.execute("INSERT INTO signal_shadow(ts, mint, kind, reason, price_usd, outcome_flag, cost) "
              "VALUES (?,?,?,?,?,?,?)", (t, "A", "narrative", "narrative new leader", 1.0, "ok", 0.05))
    c.execute("INSERT INTO signal_shadow(ts, mint, kind, reason, price_usd, outcome_flag, cost) "
              "VALUES (?,?,?,?,?,?,?)", (t, "B", "narrative", "narrative new", 1.0, "ok", 0.05))
    for dt, p in ((60, 0.8), (600, 1.7), (1200, 2.5)):
        c.execute("INSERT INTO token_prices(mint, ts, price_usd) VALUES ('A',?,?)", (t + 60 + dt, p))
    c.commit()
    res = exits.compare(c, 1, NOW, kind="narrative", table="signal_shadow", reason_like="% leader")
    assert res["tp+50 sl-35, no trail"]["n"] == 1 and res["tp+50 sl-35, no trail"]["mean_after_cost"] == 0.45
