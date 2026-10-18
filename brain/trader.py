"""The brain's paper account: sees every opportunity, decides, opens and manages its own positions.

Opportunities are read from the shared DB as the other services write them: whale buys (ct_swaps, from the copy
tracker), smart-money signals (signals) and sniper entries (sniper_launches). Every opportunity is recorded with
its context and the decision, also the ones the brain lets pass: those are what it learns most from.
Paper only: fills come from copytrade's PaperExecutor (real Jupiter quotes, measured slippage, fees).
"""
from __future__ import annotations

import collections
import copy
import dataclasses
import json
import logging
import random
import time

from . import decide, exits, features, opportunities, pockets, store
from .pool import Pool
from .plans import BY_NAME, PLANS

log = logging.getLogger("brain.trader")

DEDUP_S = 6 * 3600  # one opportunity per (whale entity or kind, coin) in this window: repeat buys are no new idea
POOL_DRAWS = 3000  # the learning account's draws its quantile is taken over
POOL_DRAWS_MIN = 200
NAME_COPY_S = 6 * 3600  # a scam's name copies come within hours
RECORD_N = 30  # the brain's own trades of a kind its exploring is judged on
RECORD_MIN_N = 20
RECORD_EVERY_S = 60
CURVE_FEE = 0.0125  # pump.fun protocol + creator fee on a curve sale
CURVE_V_SOL_DEFAULT = 30.0
RUG_BAN_S = 24 * 3600
REENTRY_S = 6 * 3600  # no second brain position in a coin within this window: a coin we just lost on is no new idea
TRAIL_MIN_HOLD_S = 60  # the trail arms after a minute: the first quotes after a buy are noise, not a run
PRICE_RETRY_S = 30  # a coin without a DexScreener pair yet gets this long to show one before it is decided
FLIP_MAX = 0.6
RAN_5M_MAX = 0.05  # 10/2026: whale copies after >5 % in the 5 min before lost 9-20 % on average, flat ones about 0  # wallets that sell this share of their buys within FLIP_S are gone before a copy can follow
NO_MARKET_S = 15 * 60  # an open position without any price this long is closed as "kein Markt"
RUG_LIQ_DROP = 0.6  # pool (curve: curve depth) 60 % under the buy: LP pulled, before the price stop sees it
STAGED_TRAIL = 0.10  # staged pump (safety check 'staged', data.pattern, 15784c6): 10 % under the high ...
STAGED_AFTER_HIGH_S = 15 * 60  # ... or 15 min after the high (74 % drained within 60 min, mostly 15-60 min after it)
STAGED_REFRESH_S = 300
STAGED_YOUNG_H = 1.0
STAGED_OLD_REFRESH_S = 900  # older coins: the exit risk is read less often
EXIT_RISK_SELL = 60  # safety check 'exit' (status ok): ~-60 % liquidity in 60 min, insider sales ~25 % of it, or two
# medium reasons together (safety thread 18.10., derived from its ramp, to be calibrated on the exits it causes)
INFLOW_DROP = 0.6  # 5-min buys under this share of their peak while in profit: the rush is over
EXIT_NAMES = {"target": "Ziel erreicht", "stop": "Stop-Loss", "trail": "Trailing-Stop", "time": "Zeit abgelaufen",
              "whale": "Wal verkauft", "inflow_peak": "Ansturm vorbei", "rug": "Liquidität abgezogen",
              "no_market": "kein Markt", "organic": "Zustand des Coins gekippt",
              "staged": "inszenierter Pump", "exit_risk": "Insider ziehen ab"}
VERDICT_EVERY_S = 600
MARKET_MIN_N = 30
MARKET_WINDOW_S = 3 * 86400
MARKET_CHALLENGE_PER_H = 12  # challenge: exploration buys from the unproven side sources per hour
CHALLENGE_PAUSE_S = 6 * 3600
MOMENTUM_MIN_M5 = 5.0  # DexScreener 5-min change in percent: a move is on (wide on purpose, the brain only watches
MOMENTUM_MIN_TX = 30
MOMENTUM_EVERY_S = 60
MOMENTUM_FRESH_S = 180  # and learns this source until its replays earn it)


def _depth(snap) -> float:
    """Liquidity a trade meets: the pool's, else the bonding curve's (DexScreener gives curves no liquidity)."""
    return snap.liquidity_usd or getattr(snap, "curve_liq_usd", 0.0) or 0.0


def _default_staged(conn, mint) -> tuple[bool, float | None]:
    """(confirmed staged pump?, exit risk 0-85 or None) from one quick safety assessment."""
    import safety  # type: ignore

    a = safety.assess(mint, conn, deep=False)
    checks = a.checks or {}
    staged = bool(((checks.get("staged") or {}).get("data") or {}).get("pattern"))
    ex = checks.get("exit") or {}
    risk = ex.get("risk") if ex.get("status") == "ok" else None
    return staged, risk


def _default_curve(conn, mint, now):
    from sniper import store as sn_store  # type: ignore

    return sn_store.curve_now(conn, mint, CURVE_MARK_AGE_S, now=now)


def _default_safety(conn, mint):
    """(level, blocked, checked, reasons): the stored rug verdict while fresh, else the quick check (~4 RPC calls)."""
    import safety  # type: ignore

    a = safety.assess(mint, conn, deep=False)
    return a.level, bool(a.blocked), a.level != "unchecked", [r.get("text") for r in (a.reasons or [])[:2]]


HELD_S = 30
CURVE_MARK_MAX_RATIO = 5.0  # a curve price this far from DexScreener's is a units error (quote coins), not lag
CURVE_MARK_AGE_S = 120  # a curve price stays the price until the next trade; older than this the sniper stopped watching


def _with_price(snap, px: float):
    """The snapshot with another price (the live curve's), price_usd and the curve depth scaled along."""
    k = px / snap.price_sol if snap.price_sol else 1.0
    out = copy.copy(snap)
    out.price_sol = px
    if getattr(out, "price_usd", None):
        out.price_usd *= k
    if getattr(out, "curve_liq_usd", None):
        out.curve_liq_usd *= k ** 0.5  # constant product: the curve's depth goes with the square root of its price
    return out


def _always_learn(row) -> bool:
    """A signals call, band candidate or near call (>= 2 smart groups bought, only a later gate said no; ~100 a day):
    always a learning buy (rare, and the model must measure them)."""
    if row["source_action"] == "alerted":
        return True
    try:
        f = json.loads(row["features"] or "{}")
        return any(f.get(k) in (True, 1, "yes") for k in ("signals.band", "signals.near_call"))
    except (TypeError, ValueError):
        return False


OWN_SOURCES = ("whale", "launch", "market")  # the brain reads these itself (ct_swaps, sniper, signal_shadow)


def opportunities_model(pm) -> str:
    from .pool import VERSION

    return f"{VERSION}/{pm.n:.0f}"


class Trader:
    def __init__(self, conn, s, model, *, snapshots=None, executor=None, safety_check=None, notify=None,
                 on_close=None, rng: random.Random | None = None, clock=time.time, exit_model=None, pool_model=None,
                 staged_check=None, curve_now=None):
        self.conn, self.s, self.model = conn, s, model
        self.pm = pool_model if pool_model is not None else Pool(len(PLANS))
        self.xm = exit_model  # organic exits (exits.py); None = the plans' fixed exits only
        self.live = exits.Live()
        self.pockets = pockets.Book(conn, lambda: self.clock())
        self._organic_at = (0.0, False)
        self._market_at = (-1e18, False)
        self._challenge_at = (-1e18, False)
        self._momentum_at = -1e18
        self._snapshots = snapshots
        self._executor = executor
        self.safety_check = safety_check or _default_safety
        self.staged_check = staged_check or _default_staged
        self._staged: dict[str, tuple[float, bool, float | None]] = {}
        self.curve_now = curve_now or _default_curve
        self._held: dict[int, float] = {}  # position -> when the quote last contradicted its stop or target
        self._snaps: dict = {}  # the last market snapshot per open mint, for the curve exits between two price rounds
        try:  # once, before the first trade on the split accounts
            moved = store.split_learning(conn, s.start_balance_sol, clock())
            if moved:
                log.info("learning account split off: %+.4f SOL of past pool trades moved out of the brain's balance",
                         moved)
        except Exception as e:
            log.warning("learning account split failed: %s", e)
        self.notify = notify or (lambda text: None)
        self.on_close = on_close or (lambda pos: None)
        self.rng = rng or random.Random()
        self.clock = clock
        self._safe = None  # the rug verdict of the opportunity being decided
        self.too_late = 0  # whale buys the tracker delivered after max_whale_delay_s
        self.supp = {"read": set(), "old": set(), "own": set()}  # supplemented row ids this hour (hour line)
        self.late_lag: list[tuple[float, float]] = []  # (tracker lag, brain lag) of those, seconds
        self.pending: dict[tuple[str, str], tuple] = {}  # (kind, ref) -> (row, entity, first seen): waiting for a pair
        self._records: dict = {}  # kind -> (checked at, (mean net, n) or None)
        self.pool_draws: collections.deque = collections.deque(maxlen=POOL_DRAWS)  # recent learning-account draws
        try:
            self.pool_draws.extend(r[0] for r in reversed(conn.execute(
                "SELECT draw FROM opportunities WHERE draw IS NOT NULL ORDER BY id DESC LIMIT ?", (POOL_DRAWS,)).fetchall()))
        except Exception:  # no opportunities table yet
            pass

    # ------------------------------------------------------------------ wiring
    def snapshots(self, mints) -> dict:
        if self._snapshots is None:
            from copytrade import market

            return market.snapshots(mints)
        return self._snapshots(mints)

    def mark(self, snaps: dict) -> dict:
        """Coins still on a bonding curve: the sniper's live curve price instead of DexScreener's, which runs 30-90 s
        behind on curves. Exits and the reference price at the buy then use the price the paper fill also meets."""
        now = self.clock()
        out = dict(snaps)
        for mint, snap in snaps.items():
            if snap is None or not snap.price_sol:
                continue
            try:
                cv = self.curve_now(self.conn, mint, now)
            except Exception as e:
                log.debug("curve %s: %s", mint[:6], e)
                cv = None
            if cv and not cv.get("complete") and cv.get("price_sol"):
                px = float(cv["price_sol"])
                if 1 / CURVE_MARK_MAX_RATIO < px / snap.price_sol < CURVE_MARK_MAX_RATIO:
                    out[mint] = _with_price(snap, px)
        return out

    @property
    def executor(self):
        if self._executor is None:
            from copytrade import chain, config as ct_config
            from copytrade.execution import PaperExecutor

            cs = ct_config.load()
            self._executor = PaperExecutor(cs, chain.Rpc(cs))
        return self._executor

    # ------------------------------------------------------------------ cursors
    def _cursor(self, key: str, sql: str) -> int:
        v = store.get(self.conn, key)
        if v is None:  # first start: begin with what comes next, never trade the backlog
            try:
                v = self.conn.execute(sql).fetchone()[0] or 0
            except Exception:
                v = 0
            store.put(self.conn, key, v)
        return int(v)

    def candidates(self) -> list[tuple[str, str, object]]:
        """New opportunities since the last scan: [(kind, ref, row)]."""
        now = self.clock()
        out = []
        c = self.conn
        if store.has(c, "ct_swaps"):
            cur = self._cursor("cursor_whale", "SELECT MAX(rowid) FROM ct_swaps")
            rows = c.execute("SELECT rowid AS rid, * FROM ct_swaps WHERE rowid>? ORDER BY rowid LIMIT 500",
                             (cur,)).fetchall()
            live = store.has(c, "ct_detect")
            for r in rows:
                if r["side"] == "buy" and (r["sol_amount"] or 0) >= self.s.min_whale_sol:
                    if now - (r["ts"] or 0) <= self.s.max_whale_delay_s:
                        out.append(("whale", r["sig"], r))
                    elif not live or c.execute("SELECT 1 FROM ct_detect WHERE sig=? LIMIT 1",
                                               (r["sig"],)).fetchone():
                        # too late: counted in the hourly line, split into tracker lag (chain -> ct_swaps) and ours;
                        # only swaps the tracker saw live (ct_detect): backfill and filled gaps are hours old by design
                        self.too_late += 1
                        seen = r["seen_at"] if "seen_at" in r.keys() and r["seen_at"] else None
                        if seen:
                            self.late_lag.append((seen - (r["ts"] or seen), now - seen))
            if rows:
                store.put(c, "cursor_whale", rows[-1]["rid"])
        if store.has(c, "signals"):
            cur = self._cursor("cursor_signal", "SELECT MAX(id) FROM signals")
            rows = c.execute("SELECT * FROM signals WHERE id>? ORDER BY id LIMIT 200", (cur,)).fetchall()
            for r in rows:
                if now - (r["ts"] or 0) <= self.s.max_signal_age_s:
                    out.append(("signal", str(r["id"]), r))
            if rows:
                store.put(c, "cursor_signal", rows[-1]["id"])
        if store.has(c, "signal_shadow"):
            # side source: coins the signal funnel looked at but did not call (also without smart buyers)
            cur = self._cursor("cursor_market", "SELECT MAX(id) FROM signal_shadow")
            rows = c.execute("SELECT * FROM signal_shadow WHERE id>? ORDER BY id LIMIT 300", (cur,)).fetchall()
            for r in rows:
                if now - (r["ts"] or 0) <= self.s.max_signal_age_s:
                    out.append(("market", f"shadow{r['id']}", r))
            if rows:
                store.put(c, "cursor_market", rows[-1]["id"])
        if store.has(c, "tokens"):
            # side source: coins the signals service keeps fresh from DexScreener with a 5-min move and trading going
            # on (momentum), whether smart money is in or not; the brain learns which of them run
            # no cursor on last_updated: a row stamped ahead of the clock (seen 17.10.: 52 min in the future) would
            # have skipped everything after it. Once a minute the rows refreshed in the last 3 min; the 6 h dedup per
            # coin keeps repeats out
            if now - self._momentum_at >= MOMENTUM_EVERY_S:
                self._momentum_at = now
                try:
                    rows = c.execute(
                        """SELECT mint, MIN(last_updated, ?) AS ts, dex, mcap_usd, price_change_m5 AS pc_m5,
                                  price_change_h1 AS pc_h1, txns_m5, volume_h1, 'momentum' AS reason,
                                  CASE WHEN pair_created_at > 0 THEN (? - pair_created_at /
                                      (CASE WHEN pair_created_at > 1e11 THEN 1000 ELSE 1 END)) / 60.0 END AS age_min
                           FROM tokens WHERE last_updated>=? AND price_change_m5>=? AND COALESCE(txns_m5,0)>=?
                           ORDER BY last_updated DESC LIMIT 300""",
                        (int(now), now, int(now) - MOMENTUM_FRESH_S, MOMENTUM_MIN_M5, MOMENTUM_MIN_TX)).fetchall()
                except Exception as e:  # an older tokens table without txns: no momentum source
                    log.debug("momentum source: %s", e)
                    rows = []
                for r in rows:
                    out.append(("market", f"mom{r['mint']}{int(now) // 3600}", r))
        if self.s.pool_trade != "off" and store.has(c, "opportunities"):
            # every other source's opportunities (sniper, copytrade, signals, wallet model, strategy): one pooled model
            # learns them all; the brain's own sources come in their own way above
            cur = self._cursor("cursor_pool", "SELECT MAX(id) FROM opportunities")
            rows = c.execute("SELECT * FROM opportunities WHERE id>? ORDER BY id LIMIT 500", (cur,)).fetchall()
            for r in rows:
                if r["source"] not in OWN_SOURCES and now - (r["ts"] or 0) <= self.s.max_pool_age_s:
                    out.append(("pool", f"opp{r['id']}", r))
            if rows:
                store.put(c, "cursor_pool", rows[-1]["id"])
            # rows a source completed later (opportunities.supplement): decided once more with the fuller picture
            try:
                scur = self._cursor("cursor_pool_supp", "SELECT MAX(supplemented_at) FROM opportunities")
                rows = c.execute("SELECT * FROM opportunities WHERE supplemented_at>=? ORDER BY supplemented_at "
                                 "LIMIT 200", (scur,)).fetchall()
            except Exception:  # the column comes with the first supplement or the next start
                rows = []
            for r in rows:
                self.supp["read"].add(r["id"])
                if r["source"] in OWN_SOURCES or r["position_id"]:
                    self.supp["own"].add(r["id"])
                elif now - (r["supplemented_at"] or 0) > self.s.max_pool_age_s:
                    self.supp["old"].add(r["id"])
                else:
                    out.append(("pool", f"opp{r['id']}+", r))
            if rows:
                store.put(c, "cursor_pool_supp", rows[-1]["supplemented_at"])
        if store.has(c, "sniper_launches") and store.has(c, "sniper_positions"):
            # 'enter' is written at stage 2 together with the sniper's paper position, after decided_ts (stage 1):
            # the cursor runs on the position's opened_ts, as copytrade.engine.follow_launches does
            cur = self._cursor("cursor_launch", "SELECT MAX(opened_ts) FROM sniper_positions")
            rows = c.execute("""SELECT l.*, p.opened_ts AS entered_ts FROM sniper_launches l
                                JOIN sniper_positions p ON p.mint = l.mint
                                WHERE l.decision='enter' AND p.opened_ts>? ORDER BY p.opened_ts LIMIT 200""",
                             (cur,)).fetchall()
            for r in rows:
                if now - (r["entered_ts"] or 0) <= self.s.max_launch_age_s:
                    out.append(("launch", r["mint"], r))
            if rows:
                store.put(c, "cursor_launch", rows[-1]["entered_ts"])
        return out

    def _seen(self, kind: str, ref: str, mint: str, entity: str, now: float) -> bool:
        if self.conn.execute("SELECT 1 FROM brain_opps WHERE kind=? AND ref=?", (kind, ref)).fetchone():
            return True
        return self.conn.execute("SELECT 1 FROM brain_opps WHERE kind=? AND mint=? AND entity=? AND ts>?",
                                 (kind, mint, entity, int(now - DEDUP_S))).fetchone() is not None

    # ------------------------------------------------------------------ deciding
    def scan(self) -> int:
        """Decide every new opportunity. Returns the number of positions opened."""
        cands = self.candidates()
        if not cands and not self.pending:
            return 0
        now = self.clock()
        fresh = [(kind, ref, row, ent) for (kind, ref), (row, ent, _) in self.pending.items()]
        # the 6 h dedup (_seen) only knows recorded decisions: two buys of one whale in one coin in the same scan, or
        # while the first waits for a price, were both decided and learned as two samples
        batch = {(kind, row["mint"], ent) for kind, _, row, ent in fresh}
        for kind, ref, row in cands:
            ent = kind
            if kind == "whale":
                from copytrade import store as ct_store

                ent = ct_store.entity(self.conn, row["wallet"])
            elif kind == "pool":
                ent = row["source"] + ("+" if ref.endswith("+") else "")  # one decision per source and coin in 6 h,
                # one more once the source completed the row
            if (kind, ref) in self.pending or (kind, row["mint"], ent) in batch:
                continue
            if not self._seen(kind, ref, row["mint"], ent, now):
                batch.add((kind, row["mint"], ent))
                fresh.append((kind, ref, row, ent))
                self.pending[(kind, ref)] = (row, ent, now)
        if not fresh:
            return 0
        try:
            snaps = self.mark(self.snapshots([r["mint"] for _, _, r, _ in fresh]))
        except Exception as e:
            log.warning("snapshots failed: %s", e)
            snaps = {}
        opened = 0
        for kind, ref, row, ent in fresh:
            snap = snaps.get(row["mint"])
            if (snap is None or not snap.price_sol) and now - self.pending[(kind, ref)][2] < PRICE_RETRY_S:
                continue  # brand-new coins often show up at DexScreener a few seconds after the first buys
            self.pending.pop((kind, ref), None)
            try:
                opened += bool(self.decide(kind, ref, row, ent, snaps.get(row["mint"])))
            except Exception as e:  # one bad row must not stop the others
                log.exception("decision %s %s failed: %s", kind, ref, e)
        return opened

    def decide(self, kind: str, ref: str, row, entity: str, snap) -> int | None:
        now = self.clock()
        builder = {"whale": features.whale, "signal": features.signal, "launch": features.launch,
                   "market": features.market, "pool": features.pool}[kind]
        feats, raw = builder(self.conn, row, snap, now)
        symbol = (snap.symbol if snap else "") or (row["symbol"] if "symbol" in row.keys() else "") or ""
        self._safe = None
        if snap is not None and snap.price_sol and kind != "pool" and (kind != "market" or self.market_live()):
            # the rug verdict is part of what the brain sees (new coins have no stored verdict yet) and the gate
            # below reuses it: one quick check per priced opportunity
            try:
                self._safe = self.safety_check(self.conn, row["mint"])
                if self._safe[2]:
                    feats["safety"] = self._safe[0]
            except Exception as e:
                log.debug("safety %s: %s", row["mint"][:6], e)
        pocket, trust = self.pockets.match(kind, feats)
        st = self.settings(now, trust, self.pockets.sharpness(kind))
        model = self.model
        if kind == "pool":  # the learning account: whatever the draw lets through, at a small stake
            model = self.pm
            st = dataclasses.replace(st, min_size_sol=self.s.learn_size_sol, explore_share=0.0, no_explore="")
        d = decide.choose(model, kind, feats, st, self.rng, equity=self.equity())
        if kind == "pool":
            d.size_sol = self.s.learn_size_sol  # the learning account always buys at the learning stake (18.10.:
            # Kelly on the brain's equity made one 0.12 SOL)
            if _always_learn(row):
                # a real call (1-3 a day) or a band candidate: the learning account always takes it at the learning
                # stake, so the class gets cases of its own; the hard gates (rug, liquidity) still decide (signals
                # thread 18.10.)
                if not d.enter:
                    d.enter, d.explore, d.size_sol, d.why_not = True, True, self.s.learn_size_sol, ""
            elif d.enter and not self._top_draw(d.sample):
                # the learning account buys the best draws, not a random sample (09:00: the labels rank the pool
                # model's mean, sniper +0.115): its trades measure execution, the labels teach the rest
                d.enter, d.why_not = False, f"Lernkonto-Stichprobe: Ziehung nicht unter den besten {self.s.pool_sample:.0%}"
                raw["gate"] = "Lernkonto-Stichprobe"
            self.pool_draws.append(d.sample)
            if d.enter and self.s.pool_trade == "shadow":
                d.enter, d.why_not = False, "Lernkonto pausiert (nur Vorhersage, Labels lernen weiter)"
                raw["gate"] = "Lernkonto pausiert"
        if pocket:
            raw["pocket"], raw["trust"] = pocket, trust
        block = None
        wanted = d.enter and not d.explore  # convinced before the hard gates: what the honest score judges
        if d.enter:
            block = self._gate(kind, row["mint"], snap, d, row, raw)
        if block:
            d.enter = False
            raw["gate"] = block  # the hour line counts the gates that held wanted buys back
            fl = raw.get("flip") or {}
            if not block.startswith("Nebenquelle"):
                    log.info("PASS %s %s plan=%s%s: %s", kind, symbol or row["mint"][:6], d.plan,
                         f" flip={fl['share']:.0%}/{fl['n']}" if fl.get("share") is not None else "", block)
        note = decide.note(kind, raw, symbol, d, block or "")
        src_ts = row["entered_ts"] if kind == "launch" else row["ts"]
        cur = self.conn.execute(
            """INSERT OR IGNORE INTO brain_opps(ts, decided_at, kind, ref, mint, symbol, wallet, entity, src_ts,
                   price_sol, features, raw, decision, wanted, plan, pred_mean, pred_std, pred_p, sample, explore, size_sol,
                   note)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (int(now), now, kind, ref, row["mint"], symbol, row["wallet"] if kind == "whale" else "", entity,
             src_ts, snap.price_sol if snap else None, json.dumps(feats), json.dumps(raw, default=str),
             "enter" if d.enter else "skip", int(wanted), d.plan, d.mean, d.std, d.p_pos, d.sample, int(d.explore),
             d.size_sol if d.enter else None, note))
        opp_id = cur.lastrowid
        pid = self.open(opp_id, kind, row, snap, d, note, symbol) if d.enter and cur.rowcount else None
        if kind == "pool" and cur.rowcount:
            self._write_back(row["id"], d, block, pid)
        return pid

    def _top_draw(self, draw: float) -> bool:
        """Is this draw among the best pool_sample share of the recent ones? Until enough draws are known: a coin flip
        with that share."""
        share = self.s.pool_sample
        if share >= 1:
            return True
        if share <= 0:
            return False
        if len(self.pool_draws) < POOL_DRAWS_MIN:
            return self.rng.random() < share
        ranked = sorted(self.pool_draws)
        return draw >= ranked[min(len(ranked) - 1, int(len(ranked) * (1 - share)))]

    def _write_back(self, oid: int, d, block, pid) -> None:
        """The brain's view on a pooled opportunity, in its own row: prediction, draw, decision, stake."""
        q10 = d.mean - 1.2816 * d.std
        dec = "skip" if not pid else ("learn" if d.explore else "perform")
        self.conn.execute(
            "UPDATE opportunities SET model=?, pred_mean=?, pred_sd=?, pred_q10=?, draw=?, decision=?, account=?, "
            "size_sol=?, decided_at=?, position_id=? WHERE id=?",
            (opportunities_model(self.pm), round(d.mean, 5), round(d.std, 5), round(q10, 5), round(d.sample, 5),
             dec, None if not pid else dec, d.size_sol if pid else None, int(self.clock()), pid, oid))

    def _gate(self, kind: str, mint: str, snap, d, row=None, raw=None) -> str | None:
        """Hard limits the model never overrides: a market to trade, the rug check, risk limits."""
        if kind != "pool" and d.explore:
            rec = self._record(kind)
            if rec:
                # exploring costs the brain's own money only while the kind's real trades pay (18.10. midday: whale
                # -10 % a trade over 92, stops -23 %): a kind whose last trades lost buys only what it is convinced of
                return f"Bewährung: {kind} zuletzt {rec[0]:+.1%} je Trade über {rec[1]}, nur überzeugte Käufe"
        if kind == "market" and not self.market_live():
            if not self.challenge_on(self.clock()):
                return "Nebenquelle: erst beobachten, bis sie im Nachspielen vorne liegt"
            n = self.conn.execute("SELECT COUNT(*) FROM brain_opps WHERE kind='market' AND decision='enter' AND ts>=?",
                                  (int(self.clock()) - 3600,)).fetchone()[0]
            if n >= MARKET_CHALLENGE_PER_H:
                return f"Nebenquelle: {n} Probekäufe in dieser Stunde, mehr erst wenn sie sich bewährt"
        if snap is None or not snap.price_sol:
            return "kein Marktpreis"
        if features.on_curve(snap):
            if snap.fdv < self.s.min_curve_mcap_usd:
                return f"Bonding Curve noch zu klein (Market Cap ${snap.fdv:,.0f})"
        elif snap.liquidity_usd < self.s.min_liquidity_usd:
            return f"Liquidität zu klein (${snap.liquidity_usd:,.0f})"
        c = self.conn
        if c.execute("SELECT 1 FROM brain_positions WHERE mint=? AND status='open'", (mint,)).fetchone():
            return "schon eine offene Position in diesem Coin"
        if c.execute("SELECT 1 FROM brain_positions WHERE mint=? AND exit_reason='rug' AND closed_at>=?",
                     (mint, int(self.clock() - RUG_BAN_S))).fetchone():
            return "Coin als Rug verkauft, 24 h gesperrt"  # 18.10.: DOPE bought again 3 min after its rug sale
        born = (getattr(snap, "pair_created_ms", 0) or 0) / 1000
        young = not born or self.clock() - born <= NAME_COPY_S  # an old coin is the original, not the copy
        copy_of = young and self._name_copy(mint, (snap.symbol if snap else "") or (
            (row["symbol"] if row is not None and "symbol" in row.keys() else "") or ""))
        if copy_of:
            return copy_of
        if c.execute("SELECT 1 FROM brain_positions WHERE mint=? AND (opened_at>=? OR closed_at>=?)",
                     (mint, int(self.clock() - REENTRY_S), int(self.clock() - REENTRY_S))).fetchone():
            return "Coin in den letzten 6 h schon gehandelt"
        if kind == "whale" and row is not None:
            if c.execute("SELECT 1 FROM ct_swaps WHERE wallet=? AND mint=? AND side='sell' AND ts>=?",
                         (row["wallet"], mint, row["ts"])).fetchone():
                return "Wal hat schon wieder verkauft"
            run5 = ((raw or {}).get("intel") or {}).get("px_ret_5m")
            if run5 is None and snap.change_m5 is not None:
                run5 = snap.change_m5 / 100  # fresh coins have no price history in intel yet: DexScreener's 5 min
            if run5 is not None and run5 >= RAN_5M_MAX:
                return f"schon gelaufen ({run5:+.0%} in 5 min vor dem Kauf)"
            fl = (raw or {}).get("flip") or {}
            if fl.get("n", 0) >= 5 and fl.get("share", 0) >= FLIP_MAX:
                return f"Wal verkauft meist sofort wieder ({fl['share']:.0%} seiner Käufe binnen 2 min)"
        # paper has no budget (user, 10/2026): no daily loss stop, no exploration quota, no balance check (the paper
        # balance may go below zero). Each stays available as a setting (0 = off); max_open is a technical ceiling only
        # the learning account (pool) has its own ceiling, so hundreds of small learning buys never crowd out the
        # brain's own sources; a technical limit for the rug checks (~4 RPC calls per buy) and the price polling
        pool = kind == "pool"
        n_open = c.execute("SELECT COUNT(*) FROM brain_positions WHERE status='open' AND (kind='pool')=?",
                           (int(pool),)).fetchone()[0]
        cap = self.s.pool_max_open if pool else self.s.max_open
        if cap and n_open >= cap:
            return f"technische Obergrenze: {n_open} {'Lernkonto-' if pool else ''}Positionen offen"
        t0 = store.day_start(self.clock())
        if self.s.max_daily_loss_sol:
            lost = c.execute("SELECT COALESCE(SUM(pnl_sol),0) FROM brain_positions WHERE status='closed' "
                             "AND closed_at>=?", (t0,)).fetchone()[0]
            if lost <= -self.s.max_daily_loss_sol:
                return f"Tagesverlust-Limit erreicht ({lost:+.2f} SOL)"
        if d.explore and self.s.explore_per_day:
            n = c.execute("SELECT COUNT(*) FROM brain_opps WHERE explore=1 AND decision='enter' AND ts>=?",
                          (t0,)).fetchone()[0]
            if n >= self.s.explore_per_day:
                return f"Erkundungs-Budget für heute verbraucht ({n})"
        try:
            level, blocked, checked, reasons = self._safe or self.safety_check(c, mint)
        except Exception as e:
            log.debug("safety %s: %s", mint[:6], e)
            return "Rug-Prüfung nicht möglich (kein Kauf ohne Prüfung)"
        if blocked:
            return "Rug-Schutz blockiert (" + ("; ".join(r for r in reasons if r) or level) + ")"
        if not checked:
            return "Rug-Prüfung ohne Ergebnis (kein Kauf ohne Prüfung)"
        return None

    # ------------------------------------------------------------------ positions
    def open(self, opp_id: int, kind: str, row, snap, d, note: str, symbol: str) -> int | None:
        c = self.conn
        try:
            fill = self.executor.buy(row["mint"], d.size_sol, snap)
        except Exception as e:
            fill = None
            log.warning("paper buy %s failed: %s", row["mint"][:6], e)
        if not fill or not fill.ok:
            why = getattr(fill, "error", "") or "kein Fill"
            c.execute("UPDATE brain_opps SET decision='skip', note=note || ?, raw=json_set(COALESCE(raw, '{}'), '$.gate', ?) "
                      "WHERE id=?", (f"\nKauf nicht ausgeführt: {why}.", f"Kauf nicht ausgeführt: {why}", opp_id))
            return None
        p = BY_NAME[d.plan]
        now = int(self.clock())
        src_ts = row["ts"] if kind == "whale" else None
        cur = c.execute(
            """INSERT INTO brain_positions(opp_id, kind, mint, symbol, wallet, src_ts, opened_at, entry_price_sol,
                   size_sol, tokens, decimals, plan, take_profit, stop_loss, trail_activate, trail_drawdown, max_hold_min,
                   inflow_exit, whale_exit, entry_liq_usd, high_price_sol, last_price_sol, last_seen, peak_buys_m5,
                   note, meta)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (opp_id, kind, row["mint"], symbol, row["wallet"] if kind == "whale" else "", src_ts, now,
             fill.price_sol, fill.sol, fill.tokens, fill.decimals, p.name, p.take_profit, p.stop_loss,
             p.trail_activate, p.trail_drawdown, p.max_hold_min, p.inflow_exit, int(kind == "whale"),
             _depth(snap), snap.price_sol, snap.price_sol, now, snap.buys_m5, note,
             json.dumps({"fill_source": fill.source, "impact": fill.price_impact, "ref_price": snap.price_sol})))
        pid = cur.lastrowid
        self._book(kind, -fill.sol)
        c.execute("UPDATE brain_opps SET position_id=? WHERE id=?", (pid, opp_id))
        log.info("BUY %s %s %.2f SOL plan=%s", kind, symbol or row["mint"][:6], fill.sol, p.name)
        self.notify(f"🧠 Kauf {symbol or row['mint'][:6]} ({kind})\n{note}")
        return pid

    def manage(self) -> int:
        """Check every open position against its plan. Returns the number closed."""
        c = self.conn
        rows = c.execute("SELECT * FROM brain_positions WHERE status='open'").fetchall()
        if not rows:
            return 0
        try:
            snaps = self.snapshots([r["mint"] for r in rows])
        except Exception as e:
            log.warning("snapshots failed: %s", e)
            return 0
        self._snaps = {m: v for m, v in snaps.items() if v is not None}
        return self._manage(rows, self.mark(snaps))

    def manage_curves(self) -> int:
        """Between two price rounds: the open positions on a bonding curve, at the live curve price (a database read,
        no HTTP), so a curve coin's stop or trail fires within seconds of the trade that crossed it."""
        rows = [r for r in self.conn.execute("SELECT * FROM brain_positions WHERE status='open'").fetchall()
                if r["mint"] in self._snaps]
        if not rows:
            return 0
        base = {r["mint"]: self._snaps[r["mint"]] for r in rows}
        marked = self.mark(base)
        live = [r for r in rows if marked[r["mint"]] is not base[r["mint"]]]
        return self._manage(live, marked) if live else 0

    def _manage(self, rows, snaps) -> int:
        c = self.conn
        now = self.clock()
        organic = self.organic_on(now)
        n = 0
        for p in rows:
            snap = snaps.get(p["mint"])
            half = False
            if organic:
                reason, half = self._organic(p, snap, now)
            else:
                reason = self._exit_reason(p, snap, now)
            if snap and snap.price_sol > 0:
                self.live.observe(p["id"], now, snap.price_sol, snap.volume_m5)
                c.execute("UPDATE brain_positions SET last_price_sol=?, high_price_sol=MAX(high_price_sol, ?), "
                          "last_seen=?, peak_buys_m5=MAX(COALESCE(peak_buys_m5,0), ?) WHERE id=?",
                          (snap.price_sol, snap.price_sol, int(now), snap.buys_m5 or 0, p["id"]))
            if half:
                self.sell_half(p, snap)
            if reason and self.close(p, snap, reason):
                self.live.drop(p["id"])
                n += 1
        return n

    def challenge_on(self, now: float) -> bool:
        """The hourly x2 challenge is active unless switched off or the last 3 full hours together lost more than
        BRAIN_CHALLENGE_STOP (then it pauses 6 h and the normal sizes return)."""
        if self.s.challenge == "off":
            return False
        paused = store.get(self.conn, "challenge_paused_until", 0) or 0
        if now < paused:
            return False
        at, on = self._challenge_at
        if now - at < VERDICT_EVERY_S:
            return on
        from .journal import hour_factors

        hf = [m for _, m in hour_factors(self.conn, now, 3)]
        prod = 1.0
        for m in hf:
            prod *= m
        on = True
        if len(hf) >= 2 and prod < self.s.challenge_stop:
            store.put(self.conn, "challenge_paused_until", int(now + CHALLENGE_PAUSE_S))
            hist = (store.get(self.conn, "challenge_pauses", []) or [])[-29:] + [{"at": int(now), "factor": round(prod, 4)}]
            store.put(self.conn, "challenge_pauses", hist)
            log.info("challenge paused 6 h: last %d hours x%.3f", len(hf), prod)
            on = False
        self._challenge_at = (now, on)
        return on

    def settings(self, now: float, trust: float = 0.5, sharp: float = 1.0):
        """The settings a decision runs with. No gate, a slide: the most one position may take of the account grows
        with the trust of the best pattern it belongs to, from BRAIN_UNPROVEN_STAKE (3 %) at 50 % trust or less to the
        full share (the challenge's ½-Kelly up to 35 % while it is on) at 95 %, and falls back the same way when the
        recent results turn. 18.10.: the model was "convinced" on two market coins and staked a third of the account
        each, in a source whose record after costs was negative; conviction alone no longer moves a third of the
        account, conviction plus a record does (user 18.10.: freer, organic, no fixed rules, but honest numbers)."""
        import dataclasses

        s = self.s
        on = self.challenge_on(now)
        # second brake (plan 18.10.): the slide also needs the predictions of this source to separate winners from
        # losers out of sample (rank correlation, Book.sharpness); a source whose predictions rank like chance (today
        # -0.01) stays at the small stake however good a pattern looks
        w = max(0.0, min(1.0, (trust - 0.5) / 0.45)) * max(0.0, min(1.0, sharp))
        hi_k = s.challenge_kelly_frac if on else s.kelly_frac
        hi_m = s.challenge_max_stake if on else s.max_stake
        lo_m = min(s.unproven_stake, hi_m)
        return dataclasses.replace(s, kelly_frac=s.kelly_frac + w * (hi_k - s.kelly_frac),
                                   max_stake=lo_m + w * (hi_m - lo_m),
                                   explore_share=w * s.challenge_explore_share if on else s.explore_share)

    def market_live(self) -> bool:
        """The side source trades only once its own out-of-sample record earned it: auto = what the brain wanted from
        it made money after costs over at least MARKET_MIN_N replays; on / off force it."""
        mode = self.s.market_trade
        if mode in ("on", "off"):
            return mode == "on"
        now = self.clock()
        at, on = self._market_at
        if now - at >= VERDICT_EVERY_S:
            # recent results only (3 days): a pattern that stops working switches the source off again by itself
            # what it wanted OR explored from the source: the first buys of an unknown source are explorations
            n, mean = self.conn.execute(
                "SELECT COUNT(*), AVG(realized_plan) FROM brain_opps WHERE kind='market' AND (wanted=1 OR explore=1) "
                "AND realized_plan IS NOT NULL AND evaluated_at>=?", (int(now - MARKET_WINDOW_S),)).fetchone()
            on = n >= MARKET_MIN_N and (mean or 0) > 0
            self._market_at = (now, on)
        return on

    def organic_on(self, now: float) -> bool:
        """Organic exits decide when switched on and trained; in auto mode only while their out-of-sample replays
        beat the plans (checked every 10 min)."""
        mode = self.s.organic_exit
        if mode == "off" or not exits.mature(self.xm):
            return False
        if mode == "on":
            return True
        at, on = self._organic_at
        if now - at >= VERDICT_EVERY_S:
            on = exits.verdict(self.conn)["live"]
            self._organic_at = (now, on)
        return on

    def _organic(self, p, snap, now: float) -> tuple[str | None, bool]:
        """(close reason, sell half?) from the coin's state; hard guards first, then one model decision a minute."""
        if not snap or not snap.price_sol:
            return ("no_market" if now - (p["last_seen"] or p["opened_at"]) > NO_MARKET_S else None), False
        entry = _ref_price(p)
        px = snap.price_sol
        meta = _meta(p)
        if meta.get("exit_mode") != "organic":  # the guards below are part of the organic exit: book them there
            meta["exit_mode"] = "organic"
            self.conn.execute("UPDATE brain_positions SET meta=? WHERE id=?", (json.dumps(meta), p["id"]))
        if p["entry_liq_usd"] and _depth(snap) < (1 - RUG_LIQ_DROP) * p["entry_liq_usd"]:
            return "rug", False
        why = self._safety_exit(p, snap, now, max(p["high_price_sol"] or entry, px))
        if why:
            return why, False
        if px <= entry * (1 - exits.CRASH_STOP):
            return "stop", False
        held = (now - p["opened_at"]) / 60
        if held >= exits.MAX_HOLD_MIN:
            return "time", False
        high = max(p["high_price_sol"] or entry, px)
        if high / entry - 1 >= exits.RUNNER_UP and px <= high * (1 - exits.RUNNER_TRAIL):
            return "trail", False  # a doubled coin keeps a 30 % trail from its high (winner study)
        # the model judges a coin only after the grace minutes and a minute of its own observation: right after the
        # buy (or a restart, with empty history) its features hold no price path and the spread reads as a loss
        if held < exits.GRACE_MIN or self.live.seen_for(p["id"], now) < exits.HIST_S:
            return None, False
        if not self.live.due(p["id"], now):
            return None, False
        ago = self.live.price_ago(p["id"], now, 60)
        vpeak = self.live.vpeak.get(p["id"], 0.0)
        o = self.conn.execute("SELECT features FROM brain_opps WHERE id=?", (p["opp_id"],)).fetchone()
        try:
            liq = json.loads(o[0] or "{}").get("liq") if o else None
        except ValueError:
            liq = None
        swaps = exits.coin_swaps(self.conn, p["mint"], p["opened_at"] - 60, now)
        whale = None
        if p["kind"] == "whale" and p["wallet"]:
            whale = any(w == p["wallet"] and side == "sell" and ts >= (p["src_ts"] or p["opened_at"])
                        for ts, w, side in swaps)
        toks = exits.tokens(p["kind"], liq, px / entry - 1, 1 - px / high if high > 0 else 0.0,
                            px / ago - 1 if ago else None,
                            snap.change_m5 / 100 if snap.change_m5 is not None else None,
                            (snap.volume_m5 or 0) / vpeak if vpeak > 0 else None, held, whale,
                            exits.smart_flow(swaps, p["opened_at"], now, p["wallet"]))
        act, mean, pp = exits.advise(self.xm, toks, self.s, bool(meta.get("partial_sol")), px / entry - 1)
        if act == "sell":
            log.info("organic exit %s: next %d min %+.1f%%, P(up) %.0f%%", p["symbol"] or p["mint"][:6],
                     exits.FWD_MIN, mean * 100, pp * 100)
            return "organic", False
        return None, act == "half"

    def _safety_exit(self, p, snap, now: float, high: float) -> str | None:
        """From the stored safety assessment, every 5 min while the coin is young, every 15 min after: a confirmed
        staged pump (out 10 % under the high or 15 min after it, before the drain; a found pattern stays) or the
        insiders leaving (check 'exit' risk >= EXIT_RISK_SELL)."""
        at, hit, risk = self._staged.get(p["mint"], (0.0, False, None))
        every = STAGED_REFRESH_S if (snap.age_hours or 0) <= STAGED_YOUNG_H else STAGED_OLD_REFRESH_S
        if not at or now - at >= every:
            try:
                r = self.staged_check(self.conn, p["mint"])
                st, risk = r if isinstance(r, tuple) else (r, None)
                hit = hit or bool(st)
            except Exception as e:
                log.debug("staged %s: %s", p["mint"][:6], e)
            self._staged[p["mint"]] = (now, hit, risk)
        if risk is not None and risk >= EXIT_RISK_SELL:
            return "exit_risk"
        if not hit:
            return None
        hi_ts = self.live.high_at.get(p["id"], p["opened_at"])
        if snap.price_sol <= high * (1 - STAGED_TRAIL) or now - hi_ts >= STAGED_AFTER_HIGH_S:
            return "staged"
        return None

    def sell_half(self, p, snap) -> bool:
        """Take half off the table: the rest keeps running, the proceeds are booked at once."""
        part = p["tokens"] * 0.5
        try:
            fill = self.executor.sell(p["mint"], part, p["decimals"] if p["decimals"] is not None else 6, snap,
                                      p["last_price_sol"] or p["entry_price_sol"])
        except Exception as e:
            log.warning("paper half sell %s failed: %s", p["mint"][:6], e)
            return False
        if not fill or not fill.ok or getattr(fill, "source", "") == "no_route":
            return False
        meta = _meta(p)
        meta["partial_sol"] = round(meta.get("partial_sol", 0.0) + fill.sol, 9)
        meta["exit_mode"] = "organic"
        done = self.conn.execute("UPDATE brain_positions SET tokens=?, meta=? WHERE id=? AND status='open'",
                                 (p["tokens"] - part, json.dumps(meta), p["id"])).rowcount
        if not done:
            return False
        self._book(p["kind"], fill.sol)
        up = fill.sol / (p["size_sol"] * 0.5) - 1 if p["size_sol"] else 0.0
        log.info("HALF %s %s %+.1f%%", p["kind"], p["symbol"] or p["mint"][:6], up * 100)
        self.notify(f"🧠 Teilverkauf {p['symbol'] or p['mint'][:6]}: Hälfte zu {up:+.1%}, der Rest läuft weiter")
        return True

    def _exit_reason(self, p, snap, now: float) -> str | None:
        held_min = (now - p["opened_at"]) / 60
        if not snap or not snap.price_sol:
            return "no_market" if now - (p["last_seen"] or p["opened_at"]) > NO_MARKET_S else None
        # targets, stops and the trail compare like with like: the market price now against the market price at the
        # buy; the fill price carries slippage and comes from another source, so mixing them fires exits on noise
        entry = _ref_price(p)
        px = snap.price_sol
        high = max(p["high_price_sol"] or entry, px)
        if p["entry_liq_usd"] and _depth(snap) < (1 - RUG_LIQ_DROP) * p["entry_liq_usd"]:
            return "rug"
        why = self._safety_exit(p, snap, now, high)
        if why:
            return why
        trail_on = (p["trail_activate"] and now - p["opened_at"] >= TRAIL_MIN_HOLD_S
                    and high / entry - 1 >= p["trail_activate"])
        stop_px = max(entry * (1 - p["stop_loss"]), high * (1 - p["trail_drawdown"]) if trail_on else 0)
        if px <= stop_px:
            return "trail" if trail_on and stop_px > entry * (1 - p["stop_loss"]) else "stop"
        if px >= entry * (1 + p["take_profit"]):
            return "target"
        peak = max(p["peak_buys_m5"] or 0, snap.buys_m5 or 0)
        if (p["inflow_exit"] and held_min >= 5 and px / entry - 1 > 0.05 and peak >= 10
                and (snap.buys_m5 or 0) < INFLOW_DROP * peak):
            return "inflow_peak"
        if p["whale_exit"] and p["wallet"] and self.conn.execute(
                "SELECT 1 FROM ct_swaps WHERE wallet=? AND mint=? AND side='sell' AND ts>=?",
                (p["wallet"], p["mint"], p["src_ts"] or p["opened_at"])).fetchone():
            return "whale"
        if held_min >= p["max_hold_min"]:
            return "time"
        return None

    def close(self, p, snap, reason: str) -> bool:
        c = self.conn
        if reason in ("stop", "target") and self.clock() - self._held.get(p["id"], -1e18) < HELD_S:
            return False  # the last quote contradicted this trigger: no new quote for a while
        try:
            fill = self.executor.sell(p["mint"], p["tokens"], p["decimals"] if p["decimals"] is not None else 6, snap,
                                      p["last_price_sol"] or p["entry_price_sol"])
        except Exception as e:
            log.warning("paper sell %s failed: %s", p["mint"][:6], e)
            return False
        if not fill or not fill.ok:
            return False
        if getattr(fill, "source", "") == "no_route" and features.on_curve(snap):
            # a coin on the bonding curve has a market, the curve itself: Jupiter often has no route for curve coins,
            # and retrying left time-stopped positions open for hours (18.10.: VANCE2028 198 min, Normie 173 min).
            # Sold on the curve instead: constant product at the live curve price, curve fee, the paper fees
            fill = self._curve_fill(p, snap, fill)
            if fill is None:
                return False
        now = int(self.clock())
        meta = _meta(p)
        partial = meta.get("partial_sol", 0.0) or 0.0  # the half sold earlier was booked then
        proceeds = fill.sol + partial
        pnl = proceeds - p["size_sol"]
        pct = pnl / p["size_sol"] if p["size_sol"] else 0.0
        if (reason == "stop" and pct > 0) or (reason == "target" and pct < 0):
            # the trigger read a stale price (DexScreener runs behind on fast coins), the quote says otherwise: no
            # sale, the position keeps running (18.10.: Elliot +22.9 % and AURORA -6.8 % booked as stop / target)
            self._held[p["id"]] = now
            log.info("exit held %s %s: %s triggered, the quote says %+.1f%%", p["kind"], p["symbol"] or p["mint"][:6],
                     reason, pct * 100)
            return False
        if pct <= -0.9 and reason not in ("rug", "no_market"):
            # whatever fired, a sale at -90 % or worse is a rug (18.10.: NAPCLUB -98.9 % booked as time)
            meta["trigger"] = reason
            reason = "rug"
            c.execute("UPDATE brain_positions SET meta=? WHERE id=?", (json.dumps(meta), p["id"]))
        done = c.execute("UPDATE brain_positions SET status='closed', closed_at=?, exit_price_sol=?, proceeds_sol=?, "
                         "pnl_sol=?, pnl_pct=?, exit_reason=? WHERE id=? AND status='open'",
                         (now, fill.price_sol, proceeds, pnl, pct, reason, p["id"])).rowcount
        if not done:
            return False
        self._book(p["kind"], fill.sol)
        held = (now - p["opened_at"]) / 60
        log.info("SELL %s %s %+.1f%% (%s)", p["kind"], p["symbol"] or p["mint"][:6], pct * 100, reason)
        self.notify(f"🧠 Verkauf {p['symbol'] or p['mint'][:6]}: {pct:+.1%} ({pnl:+.3f} SOL) nach {held:.0f} min, "
                    f"{EXIT_NAMES.get(reason, reason)}")
        closed = c.execute("SELECT * FROM brain_positions WHERE id=?", (p["id"],)).fetchone()
        try:
            self.on_close(closed)
        except Exception as e:
            log.exception("learning from trade %s failed: %s", p["id"], e)
        return True

    def _name_copy(self, mint: str, symbol: str) -> str | None:
        """A coin named like another one that rugged or was dumped in the last hours: the same scam again (18.10.: two
        'Glow' copies of one creator after the Glow launch, one -0.099 SOL rug)."""
        sym = symbol.strip().lower()
        if not sym:
            return None
        c, since = self.conn, int(self.clock() - NAME_COPY_S)
        if c.execute("SELECT 1 FROM brain_positions WHERE lower(symbol)=? AND mint!=? AND exit_reason='rug' AND "
                     "closed_at>=? LIMIT 1", (sym, mint, since)).fetchone():
            return f"Namenskopie: {symbol} wurde in den letzten Stunden schon als Rug verkauft"
        if store.has(c, "sniper_launches"):
            try:
                hit = c.execute("SELECT 1 FROM sniper_launches WHERE created_ts>=? AND lower(symbol)=? AND mint!=? AND "
                                "(rugged=1 OR COALESCE(known_bad,0)>0 OR COALESCE(dev_sold_pct,0)>=90) LIMIT 1",
                                (since, sym, mint)).fetchone()
            except Exception:
                hit = None
            if hit:
                return f"Namenskopie: ein anderer {symbol} wurde in den letzten Stunden gerugged oder abverkauft"
        return None

    def _record(self, kind: str):
        """(mean net, n) of the kind's last RECORD_N closed trades when they lost on average, else None."""
        now = self.clock()
        at, val = self._records.get(kind, (0.0, None))
        if now - at < RECORD_EVERY_S:
            return val
        rows = self.conn.execute("SELECT pnl_pct FROM brain_positions WHERE kind=? AND status='closed' AND pnl_pct IS "
                                 "NOT NULL ORDER BY closed_at DESC LIMIT ?", (kind, RECORD_N)).fetchall()
        m = sum(r[0] for r in rows) / len(rows) if rows else 0.0
        val = (m, len(rows)) if len(rows) >= RECORD_MIN_N and m < 0 else None
        self._records[kind] = (now, val)
        return val

    def _curve_fill(self, p, snap, quote):
        """A paper sale on the bonding curve (no Jupiter route): SOL out of the constant-product curve at the live
        price, curve fee from the output, the executor's fees. None without a price."""
        from copytrade.execution import Fill

        px = snap.price_sol if snap is not None else 0.0
        tokens = p["tokens"] or 0.0
        if not px or tokens <= 0:
            return None
        v_sol = 0.0
        try:
            cv = self.curve_now(self.conn, p["mint"], self.clock())
            v_sol = float((cv or {}).get("v_sol") or 0.0)
        except Exception:
            pass
        if v_sol <= 0:
            v_sol = CURVE_V_SOL_DEFAULT  # pump.fun's virtual SOL at the start: the shallowest the curve gets
        v_tok = v_sol / px
        gross = v_sol - v_sol * v_tok / (v_tok + tokens)
        net = max(0.0, gross * (1 - CURVE_FEE) - (getattr(quote, "fees_sol", 0.0) or 0.0))
        return Fill(True, tokens=tokens, sol=net, price_sol=net / tokens, price_impact=1 - gross / (tokens * px),
                    fees_sol=getattr(quote, "fees_sol", 0.0) or 0.0, tx=f"paper-curve-{int(self.clock() * 1000)}",
                    source="curve_estimate")

    def _book(self, kind: str, delta: float) -> None:
        key = store.account(kind)
        store.adjust_balance(self.conn, delta, self.s.start_balance_sol if key == store.PERFORM else 0.0, key)

    def _open_value(self, learn: bool) -> float:
        return sum((r[0] or 0) * (r[1] or 0) for r in self.conn.execute(
            "SELECT tokens, last_price_sol FROM brain_positions WHERE status='open' AND (kind='pool')=?",
            (int(learn),)).fetchall())

    def equity(self) -> float:
        """The brain's own account now (the learning account apart): cash plus open positions at their last price."""
        return store.balance(self.conn, self.s.start_balance_sol) + self._open_value(False)

    def equity_snapshot(self) -> dict:
        """One row a minute in brain_equity: the brain's own account in the first columns (what the hour factors and
        the challenge read), the learning account beside it (starts at 0, so its equity is its result so far)."""
        c = self.conn
        bal = store.balance(c, self.s.start_balance_sol)
        val = self._open_value(False)
        lbal = store.balance(c, 0.0, store.LEARN)
        lval = self._open_value(True)
        ts = int(self.clock()) // 60 * 60
        c.execute("INSERT OR REPLACE INTO brain_equity(ts, balance_sol, open_value_sol, equity_sol, learn_balance_sol, "
                  "learn_open_sol, learn_equity_sol) VALUES (?,?,?,?,?,?,?)",
                  (ts, bal, val, bal + val, lbal, lval, lbal + lval))
        return {"balance_sol": bal, "open_value_sol": val, "equity_sol": bal + val,
                "learn_balance_sol": lbal, "learn_open_sol": lval, "learn_equity_sol": lbal + lval}


def _meta(p) -> dict:
    try:
        return json.loads(p["meta"] or "{}") or {}
    except (TypeError, ValueError):
        return {}


def _ref_price(p) -> float:
    try:
        ref = json.loads(p["meta"] or "{}").get("ref_price")
    except (TypeError, ValueError):
        ref = None
    return float(ref) if ref else p["entry_price_sol"]
