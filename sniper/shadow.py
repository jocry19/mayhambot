"""Shadow book "schneller Einstieg" (plan 19.10. S1): what an entry 2 to 5 s after the launch would have brought,
next to the same launch entered at today's 20 s decision. Measured only: no paper balance, no learning buy, no
main-account buy, nothing reported to the brain.

Per pump.fun launch:
  fast    at SNIPER_SHADOW_ENTRY_SEC (2 s) the stream-only rug check runs (bundle estimate, hard flags, dev sold,
          "Masche" dev buy >= 75 %, dev history, known bundlers/ruggers, the rug model); a launch that passes is
          filled SNIPER_SHADOW_LATENCY_SEC later on the curve state of that moment, never later than
          SNIPER_SHADOW_MAX_SEC (5 s) after the create
  decide  the same launch at SNIPER_DECIDE_SEC (20 s) plus the same latency, with the same exits and costs
Exit: a trail of SNIPER_SHADOW_TRAIL_PCT (20 %) from the high once the price was SNIPER_SHADOW_TRAIL_FROM_PCT
(20 %) above the entry, an emergency stop at -SNIPER_SHADOW_STOP_PCT (50 %), else held to the end. Every leg is
followed SNIPER_SHADOW_HOLD_MIN (60 min) from its fill, so the row also carries the result of simply holding.
Fills and sells use the paper book's own formulas (curve fee from the trades, adverse slippage, priority and
network fee per transaction), so a round trip costs about 10 % like every other sniper measurement.

Rows go to sniper_shadow (one per mint and leg) when a leg is finished. Open legs are copied to sniper_shadow_open
every minute and taken up again after a restart (the app restarted every 17 to 28 min from 19.10. 21:58, so no leg
reached its hour and nothing was written for three days); gap_sec counts the seconds a leg was not watched, in
which its high, trail or stop may have been missed. The lab reads decide_ts, fill_ts, fill_price_sol and the cost columns from there."""
import logging
import statistics

from core import db
from sniper import store

log = logging.getLogger("sniper.shadow")

SCHEMA = """CREATE TABLE IF NOT EXISTS sniper_shadow (
    mint TEXT, leg TEXT, pool TEXT, created_ts REAL, decide_ts REAL, fill_ts REAL, fill_age_sec REAL,
    fill_mcap_sol REAL, fill_price_sol REAL, spot_price_sol REAL, size_sol REAL, tokens REAL,
    curve_fee_pct REAL, adverse_slippage REAL, fees_sol REAL, buy_cost_pct REAL, fill_src TEXT,
    peak_price_sol REAL, max_ret REAL, x2 INTEGER, exit_ts REAL, exit_reason TEXT, exit_sol REAL, pnl_pct REAL,
    end_ts REAL, end_sol REAL, ret_60m REAL, end_src TEXT, precheck_json TEXT, params_json TEXT, gap_sec REAL,
    PRIMARY KEY (mint, leg)
)"""
OPEN_SCHEMA = """CREATE TABLE IF NOT EXISTS sniper_shadow_open (
    mint TEXT, leg TEXT, saved_ts REAL, leg_json TEXT, PRIMARY KEY (mint, leg)
)"""
SAVE_SEC = 60
COLS = ("mint", "leg", "pool", "created_ts", "decide_ts", "fill_ts", "fill_age_sec", "fill_mcap_sol",
        "fill_price_sol", "spot_price_sol", "size_sol", "tokens", "curve_fee_pct", "adverse_slippage", "fees_sol",
        "buy_cost_pct", "fill_src", "peak_price_sol", "max_ret", "x2", "exit_ts", "exit_reason", "exit_sol",
        "pnl_pct", "end_ts", "end_sol", "ret_60m", "end_src", "precheck_json", "params_json", "gap_sec")
LEGS = ("fast", "decide")
# the sniper's later verdict that the 2 s check could not see yet (deep check, rug protection)
LATER_RUG = ("bundle", "insider", "hard", "safety", "rug_p", "dev_sold")


def ensure_schema(conn):
    db.retry_locked(conn, lambda: conn.execute(SCHEMA))
    db.retry_locked(conn, lambda: conn.execute(OPEN_SCHEMA))
    if "gap_sec" not in {r[1] for r in conn.execute("PRAGMA table_info(sniper_shadow)")}:
        db.retry_locked(conn, lambda: conn.execute("ALTER TABLE sniper_shadow ADD COLUMN gap_sec REAL"))
    db.retry_locked(conn, lambda: conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sniper_shadow_fill ON sniper_shadow(fill_ts)"))
    return conn


class ShadowBook:
    def __init__(self, cfg, book, conn=None):
        self.cfg, self.book = cfg, book
        self.open = {}        # (mint, leg) -> leg dict, in memory until finished
        self.done = []        # finished legs, written by flush() in the loop thread
        self.counts = {"checked": 0, "filled_fast": 0, "filled_decide": 0, "late": 0, "no_fill": 0, "written": 0, "resumed": 0}
        self._saved = 0.0
        self.blocked = {}     # precheck reason -> count
        if conn is not None:
            ensure_schema(conn)

    def holds(self, mint):
        return any((mint, leg) in self.open for leg in LEGS)

    def params(self):
        c = self.cfg
        return {"entry_sec": c.shadow_entry_sec, "max_sec": c.shadow_max_sec, "latency_sec": c.shadow_latency_sec,
                "decide_sec": c.decide_sec, "trail_from_pct": c.shadow_trail_from_pct, "trail_pct": c.shadow_trail_pct,
                "stop_pct": c.shadow_stop_pct, "hold_min": c.shadow_hold_min, "size_sol": c.size_sol}

    # ------------------------------------------------------------------ rug check before the fast entry
    def precheck(self, conn, st, engine, now):
        """Reason the 2 s entry is not taken, or None. Only what the stream and our own tables know this early."""
        from sniper.analyze import analyze
        if st.migrated:
            return "migrated", {}
        if (st.mcap_sol or 0) < st.base_floor():
            return "no_price", {}
        a = analyze(st, self.cfg, None, engine.tiers, now)
        F = a.features
        feats = {k: F.get(k) for k in ("n_trades", "n_buyers", "dev_buy_pct", "dev_sold_pct", "unseen_pct",
                                       "bundle_pct", "top10_pct", "fast_buyers")}
        feats["mcap_sol"] = round(st.mcap_sol, 3)
        if a.bundle:
            return "bundle", feats
        if a.hard:
            return "hard", feats
        if (F.get("dev_sold_pct") or 0) >= 50:
            return "dev_sold", feats
        if (F.get("dev_buy_pct") or 0) >= 75:
            return "masche", feats      # the rug protection's veto: the dev buys the curve himself
        if st.creator:
            h = store.creator_history(conn, st.creator, now, exclude_mint=st.mint)
            feats["dev_bad"] = h["bad"]
            if h["bad"] >= 2:
                return "dev_history", feats
        bad = store.known_bad(conn, [st.creator, *list(st.bought)[:50]])
        feats["known_bad"] = sum(1 for n in bad.values() if n)
        if feats["known_bad"] >= 2 or bad.get(st.creator):
            return "known_bad", feats
        p = engine.rug_p(dict(F, mcap_decide_sol=st.mcap_sol), now)
        feats["rug_p"] = p
        if p is not None and p >= self.cfg.rug_p_block:
            return "rug_p", feats
        return None, feats

    # ------------------------------------------------------------------ per launch, every tick (loop thread)
    def step(self, conn, st, engine, now):
        if st.source != "pumpportal" or st.pool != "pump":
            return
        c = self.cfg
        age = now - st.created_ts
        sh = getattr(st, "_shadow", None)
        if sh is None:
            if st.decision != "pending" or age >= c.shadow_max_sec or age < c.shadow_entry_sec:
                if st.decision == "pending" and age >= c.shadow_max_sec:
                    st._shadow = {"state": "late"}
                    self.counts["late"] += 1
                return
            self.counts["checked"] += 1
            why, feats = self.precheck(conn, st, engine, now)
            st._shadow = sh = {"state": "blocked" if why else "wait", "decide_ts": now, "pre": dict(feats, reason=why)}
            if why:
                self.blocked[why] = self.blocked.get(why, 0) + 1
                return
        if sh["state"] == "wait" and now >= sh["decide_ts"] + c.shadow_latency_sec:
            if age > c.shadow_max_sec + 1:     # the loop fell behind: no fill that the plan would not allow
                sh["state"] = "late"
                self.counts["late"] += 1
            else:
                sh["state"] = "filled" if self._fill(st, "fast", sh["decide_ts"], now, sh["pre"]) else "no_fill"
        if sh["state"] == "filled" and "decide" not in sh and age >= c.decide_sec:
            sh["decide"] = now      # the 20 s twin of the same launch, same latency
        if sh.get("decide") and not sh.get("decide_done") and now >= sh["decide"] + c.shadow_latency_sec:
            sh["decide_done"] = True
            self._fill(st, "decide", sh["decide"], now, sh["pre"])
        self.check(st, now)

    def _fill(self, st, leg, decide_ts, now, pre):
        size = self.cfg.size_sol
        spot = st.price_sol
        tokens, _impact, src = self.book.fill_buy(st, size, None)
        if tokens <= 0 or spot <= 0:
            self.counts["no_fill"] += 1
            return False
        price = size / tokens
        self.open[(st.mint, leg)] = {
            "mint": st.mint, "leg": leg, "pool": st.pool, "created_ts": st.created_ts, "decide_ts": decide_ts,
            "fill_ts": now, "fill_age_sec": round(now - st.created_ts, 2), "fill_mcap_sol": round(st.mcap_sol, 4),
            "fill_price_sol": price, "spot_price_sol": spot, "size_sol": size, "tokens": tokens,
            "curve_fee_pct": st.fee_pct or self.cfg.curve_fee_pct, "adverse_slippage": self.cfg.adverse_slippage,
            "fees_sol": self.book._fees(), "buy_cost_pct": round(1 - tokens * spot / size, 4), "fill_src": src,
            "peak_price_sol": price, "gap_sec": 0.0, "precheck_json": store.j(pre), "params_json": store.j(self.params())}
        self.counts["filled_" + leg] += 1
        return True

    # ------------------------------------------------------------------ exits (stream or loop thread, memory only)
    def check(self, st, now):
        c = self.cfg
        for leg in LEGS:
            p = self.open.get((st.mint, leg))
            if not p:
                continue
            price = self.book.mark(st, now)
            if price > 0:
                p["peak_price_sol"] = max(p["peak_price_sol"], price)
                entry, peak = p["fill_price_sol"], p["peak_price_sol"]
                if not p.get("exit_reason"):
                    why = None
                    if price <= entry * (1 - c.shadow_stop_pct / 100):
                        why = "stop"
                    elif peak >= entry * (1 + c.shadow_trail_from_pct / 100) and \
                            price <= peak * (1 - c.shadow_trail_pct / 100):
                        why = "trail"
                    if why:
                        sol, src = self.book.fill_sell(st, p["tokens"], None, now)
                        p.update(exit_reason=why, exit_ts=now, exit_sol=sol, exit_src=src)
            if now >= p["fill_ts"] + c.shadow_hold_min * 60:
                self._finish(st, p, now, price)

    def _finish(self, st, p, now, price):
        if price > 0:
            end_sol, src = self.book.fill_sell(st, p["tokens"], None, now)
            p.update(end_ts=now, end_sol=end_sol, end_src=src, ret_60m=round(end_sol / p["size_sol"] - 1, 4))
        else:
            p.update(end_ts=now, end_sol=None, end_src="no_price", ret_60m=None)   # no market we trust: no label
        if not p.get("exit_reason") and price > 0:
            p.update(exit_reason="end", exit_ts=now, exit_sol=p["end_sol"])
        if p.get("exit_sol") is not None:
            p["pnl_pct"] = round(p["exit_sol"] / p["size_sol"] - 1, 4)
        p["max_ret"] = round(p["peak_price_sol"] / p["fill_price_sol"] - 1, 4)
        p["x2"] = int(p["peak_price_sol"] >= 2 * p["fill_price_sol"])
        self.open.pop((p["mint"], p["leg"]), None)
        self.done.append(p)

    def drop(self, mint, now, st=None):
        """The launch leaves memory before the hour is over (should not happen, the engine keeps it): finish
        what is known instead of losing the row."""
        for leg in LEGS:
            p = self.open.get((mint, leg))
            if p:
                self._finish(st, p, now, self.book.mark(st, now) if st else 0.0)

    def save_open(self, conn, now, force=False):
        """Copy the open legs so a restart takes them up again instead of losing them."""
        if not force and now - self._saved < SAVE_SEC:
            return
        self._saved = now
        rows = [(m, leg, now, store.j(p)) for (m, leg), p in self.open.items()]

        def write():
            conn.execute("DELETE FROM sniper_shadow_open")
            conn.executemany("INSERT INTO sniper_shadow_open VALUES (?,?,?,?)", rows)
            conn.commit()
        try:
            db.retry_locked(conn, write)
        except Exception as e:  # noqa: BLE001 - the next minute tries again
            log.warning("sniper_shadow_open: not saved: %s", e)

    def resume(self, conn, now):
        """Open legs saved before a restart, back in memory with the unwatched time in gap_sec. Returns their mints."""
        import json
        mints = set()
        for r in conn.execute("SELECT mint, leg, saved_ts, leg_json FROM sniper_shadow_open").fetchall():
            try:
                p = json.loads(r["leg_json"])
            except (TypeError, ValueError):
                continue
            p["gap_sec"] = round((p.get("gap_sec") or 0) + max(0.0, now - (r["saved_ts"] or now)), 1)
            self.open[(r["mint"], r["leg"])] = p
            mints.add(r["mint"])
        self.counts["resumed"] += len(self.open)
        return mints

    def flush(self, conn, now=None):
        if now is not None:
            self.save_open(conn, now)
        if not self.done:
            return 0
        rows, self.done = self.done, []
        q = f"INSERT OR REPLACE INTO sniper_shadow({','.join(COLS)}) VALUES ({','.join('?' * len(COLS))})"

        def write():
            conn.executemany(q, [tuple(r.get(k) for k in COLS) for r in rows])
            conn.commit()
        try:
            db.retry_locked(conn, write)
            self.counts["written"] += len(rows)
        except Exception as e:  # noqa: BLE001 - a locked DB only delays the rows
            log.warning("sniper_shadow: %d rows not written yet: %s", len(rows), e)
            self.done = rows + self.done
        return len(rows)


# ---------------------------------------------------------------------- report
def _stats(rows):
    pnl = [r["pnl_pct"] for r in rows if r["pnl_pct"] is not None]
    hold = [r["ret_60m"] for r in rows if r["ret_60m"] is not None]
    if not pnl:
        return {"n": 0}
    return {"n": len(pnl), "net": round(statistics.mean(pnl), 4), "median": round(statistics.median(pnl), 4),
            "x2": round(sum(r["x2"] or 0 for r in rows) / len(rows), 3),
            "win": round(sum(1 for v in pnl if v > 0) / len(pnl), 3),
            "hold60": round(statistics.mean(hold), 4) if hold else None, "n_hold60": len(hold),
            "age": round(statistics.median(r["fill_age_sec"] for r in rows), 1)}


def _t(xs):
    if len(xs) < 3:
        return None
    sd = statistics.stdev(xs)
    return round(statistics.mean(xs) / (sd / len(xs) ** 0.5), 2) if sd > 0 else None


def robust(rows):
    """How much the mean rests on a few big winners (plan 19.10.: Ø +5 % at a median of -11 %): the mean of the top
    fifth, the share above +100 %, the mean without the three best, the t value and the three time thirds."""
    rs = sorted((r for r in rows if r["pnl_pct"] is not None), key=lambda r: r["fill_ts"])
    xs = [r["pnl_pct"] for r in rs]
    if len(xs) < 10:
        return {"n": len(xs)}
    top = sorted(xs, reverse=True)
    k = max(1, len(xs) // 5)
    third = len(xs) // 3
    return {"n": len(xs), "top5th": round(statistics.mean(top[:k]), 4), "over100": round(sum(x > 1 for x in xs) / len(xs), 3),
            "without3": round(statistics.mean(top[3:]), 4), "best": round(top[0], 4), "t": _t(xs),
            "thirds": [round(statistics.mean(xs[i * third:(i + 1) * third if i < 2 else len(xs)]), 4) for i in range(3)]}


def report(conn, since=0):
    """Fast shadow entry against the 20 s decision on the same launches (pairs: both legs finished)."""
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT s.*, l.skip_reason AS final_reason FROM sniper_shadow s LEFT JOIN sniper_launches l "
            "ON l.mint = s.mint WHERE s.fill_ts >= ?", (since,))]
    except Exception:  # noqa: BLE001 - no table yet
        return {"n": 0}
    by = {leg: {r["mint"]: r for r in rows if r["leg"] == leg} for leg in LEGS}
    priced = {leg: {m for m, r in by[leg].items() if r["pnl_pct"] is not None} for leg in LEGS}
    pair = sorted(priced["fast"] & priced["decide"])   # the same launches on both sides, each with a result
    clean = [m for m in pair if by["fast"][m]["final_reason"] not in LATER_RUG]
    out = {"n": len(rows), "fast_all": _stats(list(by["fast"].values())),
           "pairs": {leg: _stats([by[leg][m] for m in pair]) for leg in LEGS},
           "clean": {leg: _stats([by[leg][m] for m in clean]) for leg in LEGS},
           "exits": {}, "no_price": {leg: len(by[leg]) - len(priced[leg]) for leg in LEGS},
           "robust": {leg: robust([by[leg][m] for m in pair]) for leg in LEGS}}
    diff = [by["fast"][m]["pnl_pct"] - by["decide"][m]["pnl_pct"] for m in pair]
    out["diff"] = {"n": len(diff), "mean": round(statistics.mean(diff), 4) if diff else None, "t": _t(diff)}
    for r in by["fast"].values():
        k = r["exit_reason"] or "kein Kurs"
        out["exits"][k] = out["exits"].get(k, 0) + 1
    return out


def _fmt(s):
    if not s.get("n"):
        return "keine"
    hold = "?" if s.get("hold60") is None else f"{100 * s['hold60']:+.1f} %"
    return (f"n {s['n']}, Ø netto {100 * s['net']:+.1f} %, Median {100 * s['median']:+.1f} %, ×2 {100 * s['x2']:.0f} %, "
            f"gehalten bis 60 min {hold}, Fill {s['age']:.1f} s nach dem Create")


def summary_lines(rep):
    if not rep or not rep.get("n"):
        return []
    p, c = rep["pairs"], rep["clean"]
    out = [f"Schatten-Einstieg (Trail 20 % ab +20 %, Notstopp −50 %, gleiche Kosten, nur gemessen), dieselben Launches: "
            f"2–5 s {_fmt(p['fast'])} | 20-s-Entscheid {_fmt(p['decide'])}",
            f"  ohne späteren Rug-, Bundle- oder Insider-Befund: 2–5 s {_fmt(c['fast'])} | 20 s {_fmt(c['decide'])}"
            + " | Ausstiege 2–5 s: " + ", ".join(f"{k} {v}" for k, v in sorted(rep["exits"].items()))
            + " | ohne Kurs am Ende (nicht gewertet): 2–5 s {fast}, 20 s {decide}".format(**rep.get("no_price", {"fast": 0, "decide": 0}))]
    rb, d = rep.get("robust") or {}, rep.get("diff") or {}
    if all((rb.get(leg) or {}).get("n", 0) >= 10 for leg in LEGS):
        def one(r):
            th = "/".join(f"{100 * v:+.1f}" for v in r["thirds"])
            t = "?" if r["t"] is None else f"{r['t']:.2f}"
            return (f"oberes Fünftel Ø {100 * r['top5th']:+.0f} %, über +100 % {100 * r['over100']:.1f} %, "
                    f"ohne die 3 besten Ø {100 * r['without3']:+.1f} % (bester {100 * r['best']:+.0f} %), t {t}, "
                    f"Drittel {th} %")
        dt = "?" if d.get("t") is None else f"{d['t']:.2f}"
        out.append(f"  Belastbarkeit: 2–5 s {one(rb['fast'])} | 20 s {one(rb['decide'])} | "
                   f"2–5 s minus 20 s je Launch Ø {100 * (d.get('mean') or 0):+.1f} Punkte, t {dt}")
    return out


def status(book):
    if book is None:
        return None
    return {"open": len(book.open), "pending_write": len(book.done), **book.counts, "blocked": dict(book.blocked)}

