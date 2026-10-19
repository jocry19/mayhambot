"""The brain as its own service (`python -m brain run`, started by mayham.py as "brain")."""
from __future__ import annotations

import json
import logging
import re
import threading
import time

from . import config, exits, journal, labels, learner, store
from .model import Brain
from .pool import Pool
from .plans import PLANS
from .trader import Trader

log = logging.getLogger("brain")

SAVE_EVERY_S = 600
EQUITY_EVERY_S = 60
EXIT_SKILL_S = 6 * 3600  # the exit check looks at the labels of the last 6 h
HEARTBEAT_S = 3600  # one "brain hour:" log line per hour (ops can watch it for stalls)


def load_model(s) -> Brain:
    return Brain.load(s.model_path, len(PLANS), s.half_life_h)


def load_pool_model(s) -> Pool:
    return Pool.load(s.pool_model_path, len(PLANS))


def load_exit_model(s) -> Brain:
    return Brain.load(s.exit_model_path, 1, s.half_life_h)


class Service:
    def __init__(self, s=None, conn=None, **trader_kw):
        self.s = s or config.load()
        self.conn = conn or store.connect(self.s.db_path)
        self.model = load_model(self.s)
        self.xm = load_exit_model(self.s)
        self.pm = load_pool_model(self.s)
        notify = trader_kw.pop("notify", None)
        if notify is None:
            notify = journal.send if self.s.telegram == "trades" else (lambda text: False)
        self.trader = Trader(self.conn, self.s, self.model, notify=notify, on_close=self._learn_trade,
                             exit_model=self.xm, pool_model=self.pm, **trader_kw)
        self.dirty = False
        self.learn_s: list[float] = []  # seconds per learning round this hour (the loop waits for them)
        self.max_gap = 0.0  # longest gap between two loop rounds this hour
        self._alarm_hour = None
        self._want: dict[tuple, None] = {}  # candle windows the learning round asked for, fetched by _fetch_loop
        self._want_lock = threading.Lock()

    def alarm(self, eq: dict, now: float) -> bool:
        """One "brain alarm:" line an hour (and a Telegram note where trades are sent) while the brain's own account
        is under BRAIN_EQUITY_ALARM_SOL; the learning account is named beside it, it does not count."""
        lim = self.s.equity_alarm_sol
        hour = int(now) // HEARTBEAT_S
        if not lim or eq["equity_sol"] >= lim or self._alarm_hour == hour:
            return False
        self._alarm_hour = hour
        text = (f"equity {eq['equity_sol']:.3f} SOL under {lim:.2f} (brain's own account; learning account "
                f"{eq.get('learn_equity_sol', 0.0):+.3f} SOL apart)")
        log.warning("brain alarm: %s", text)
        try:
            self.trader.notify(f"🧠 Alarm: {text}")
        except Exception:
            pass
        return True

    def _learn_trade(self, pos) -> None:
        if learner.learn_trade(self.conn, self.model, self.s, pos, pool=self.pm):
            self.dirty = True

    def learn(self, candles_fn=None) -> dict:
        n_prior = learner.warm_start(self.conn, self.model, self.s)
        n_eval = learner.evaluate(self.conn, self.model, self.s, candles_fn, xm=self.xm, pool=self.pm)
        n_back = learner.backfill_exits(self.conn, self.xm, self.s, candles_fn)
        n_lab = learner.learn_labels(self.conn, self.pm, self.s)
        if n_lab:
            self.dirty = True
        shift = learner.regime_check(self.conn, self.model, self.xm)
        if n_prior or n_eval or n_back or shift:
            self.dirty = True
        return {"prior": n_prior, "evaluated": n_eval, "exit_backfill": n_back, "labels": n_lab}

    def heartbeat(self, since: float) -> str:
        c = self.conn
        n, ent, held = c.execute("SELECT COUNT(*), COALESCE(SUM(decision='enter'),0), "
                                 "COALESCE(SUM(decision='skip' AND (wanted=1 OR explore=1)),0) FROM brain_opps WHERE ts>=?",
                                 (int(since),)).fetchone()
        n_open, l_open = c.execute("SELECT COALESCE(SUM(kind!='pool'),0), COALESCE(SUM(kind='pool'),0) "
                                   "FROM brain_positions WHERE status='open'").fetchone()
        snap = self.trader.equity_snapshot()
        eq = snap["equity_sol"]
        l_pnl, l_n = c.execute("SELECT COALESCE(SUM(pnl_sol),0), COUNT(*) FROM brain_positions WHERE kind='pool' AND "
                               "status='closed' AND closed_at>=?", (int(since),)).fetchone()
        late, self.trader.too_late = self.trader.too_late, 0
        lag, self.trader.late_lag = self.trader.late_lag, []
        if lag:
            tr, br = sorted(x for x, _ in lag), sorted(y for _, y in lag)
            late = f"{late}; median lag tracker {tr[len(tr) // 2]:.0f} s, brain {br[len(br) // 2]:.0f} s"
        last_hour = journal.hour_factors(c, time.time(), 1)  # from brain_equity: survives restarts
        hf = f", hour factor x{last_hour[0][1]:.3f}" if last_hour else ""
        return (f"{n} opportunities ({late} whale buys arrived too late), {ent} bought, {held} wanted but blocked by a gate, {n_open} open, equity {eq:.3f} SOL{hf}, "
                f"learning account equity {snap['learn_equity_sol']:+.3f} SOL ({l_open} open, {l_n} closed {l_pnl:+.3f} SOL this hour), "
                f"model {self.model.d - 1} tokens / {self.model.n:.0f} outcomes, exit model {self.xm.n:.0f} minutes "
                f"(organic exits {'on' if self.trader.organic_on(time.time()) else 'off'}, "
                f"replays {exits.verdict(c)['n']}), pool model {self.pm.d - 1} tokens / {self.pm.n:.0f} outcomes, "
                f"{self._main_buys(since)}, {self._pooled(since)}, {self._labelled(since)}, {self._gates(since)}, "
                f"{self._pace()}")

    def _pace(self) -> str:
        ls, gap = self.learn_s, self.max_gap
        self.learn_s, self.max_gap = [], 0.0
        if not ls:
            return f"learning rounds 0, longest loop gap {gap:.0f} s"
        return (f"learning rounds {len(ls)} ({sum(ls):.0f} s in all, longest {max(ls):.0f} s), "
                f"longest loop gap {gap:.0f} s")

    def _gates(self, since: float) -> str:
        """The gates that held wanted buys back this hour, most frequent first (numbers stripped)."""
        by: dict[str, int] = {}
        for (raw,) in self.conn.execute("SELECT raw FROM brain_opps WHERE ts>=? AND decision='skip' "
                                        "AND (wanted=1 OR explore=1)", (int(since),)):
            try:
                g = json.loads(raw or "{}").get("gate")
            except (TypeError, ValueError):
                g = None
            g = re.split(r"[(\d$]", g or "no gate recorded", maxsplit=1)[0].strip(" :,")[:50] or "?"
            by[g] = by.get(g, 0) + 1
        top = sorted(by.items(), key=lambda kv: -kv[1])[:4]
        return "gates " + (", ".join(f"{k} {n}" for k, n in top) or "none")

    def _labelled(self, since: float) -> str:
        try:
            n, rug, m60 = self.conn.execute("SELECT COUNT(*), COALESCE(SUM(label_rug),0), AVG(MIN(label_max60, ?)) "
                                            "FROM opportunities WHERE label_at>=?", (labels.CAP, int(since))).fetchone()
            follow = len((store.get(self.conn, "label_track") or {}).get("rows") or {})
        except Exception:
            return "labels none"
        out = (f"labels {n} written ({rug} rugs, mean best-in-60-min {m60 or 0:+.1%} after costs, capped at +1000%), "
               f"{follow} followed"
               if n else f"labels 0 written, {follow} followed")
        sk = learner.label_skill(self.conn, since)
        if sk and sk.get("rho") is not None:
            # the pool model's ranking on the hour's labels, one row per source and coin; the top tenth's median must
            # stay above 0 after costs for hours before any main-account stake rides on it (a mean is carried by
            # one glitch: 18.10. +155724 %)
            row = [int(since), sk["n"], sk["rho"], sk["top"], sk["top_median"]]
            hist = (store.get(self.conn, "label_skill_hist") or [])[-23:] + [row]
            store.put(self.conn, "label_skill_hist", hist)
            tp = sk.get("top_plan")
            out += (f", skill rho {sk['rho']:+.2f} top tenth held median {sk['top_median']:+.1%} "
                    f"mean {sk['top']:+.1%}"
                    + (f" max60 {sk['top_max60']:+.1%}" if sk.get("top_max60") is not None else "")
                    + (f" best plan {tp[0]} {tp[1]:+.1%}" if tp else "")
                    + f" bottom {sk['bottom']:+.1%} (n {sk['n']} coins)")
        ex = learner.exit_skill(self.conn, since - EXIT_SKILL_S)
        if ex and ex.get("rule"):
            # the exit judged out of sample over the last 6 h: the top tenth's best rule of the first half, measured
            # on the second; a median above 0 here for hours is the first edge (plan 'Hundertmal', lever 3)
            out += (f", exit 6h best {ex['rule']} (in-sample {ex['in_sample']:+.1%}) top tenth out-of-sample mean "
                    f"{ex['oos_mean']:+.1%} median {ex['oos_median']:+.1%} n {ex['oos_n']}, top fiftieth "
                    f"{ex.get('top50_mean') or 0:+.1%} median {ex.get('top50_median') or 0:+.1%} n {ex.get('top50_n', 0)}, "
                    f"all {ex['all_mean'] or 0:+.1%}"
                    + "".join(f"; top tenth {k} entered {v['entered']:.0%} mean {v['mean']:+.1%} median {v['median']:+.1%}"
                              for k, v in sorted(ex.items()) if k.startswith("dip") and isinstance(v, dict))
                    + f"; rules agree {ex['same']:.0%} (of those flat {ex.get('same_flat', 0):.0%}, under 4 points "
                    f"{ex['same_thin']:.0%}, rows under 4 "
                    f"points {ex['thin']:.0%})")
            hist = (store.get(self.conn, "exit_skill_hist") or [])[-23:] + [[int(since), ex]]
            store.put(self.conn, "exit_skill_hist", hist)
        return out

    def _main_buys(self, since: float) -> str:
        """The brain's own account by kind this hour: buys, and the trades closed with their net."""
        try:
            b = dict(self.conn.execute("SELECT kind, COUNT(*) FROM brain_positions WHERE kind!='pool' AND opened_at>=? "
                                       "GROUP BY kind", (int(since),)).fetchall())
            cl = {k: (n, s) for k, n, s in self.conn.execute(
                "SELECT kind, COUNT(*), COALESCE(SUM(pnl_sol),0) FROM brain_positions WHERE kind!='pool' AND "
                "status='closed' AND closed_at>=? GROUP BY kind", (int(since),)).fetchall()}
        except Exception:
            return "main none"
        kinds = sorted(set(b) | set(cl))
        return "main bought/closed/net " + (", ".join(
            f"{k} {b.get(k, 0)}/{cl.get(k, (0, 0))[0]}/{cl.get(k, (0, 0.0))[1]:+.3f}" for k in kinds) or "none")

    def _pooled(self, since: float) -> str:
        try:
            rows = self.conn.execute("SELECT source, COUNT(*), COUNT(decision), "
                                     "COALESCE(SUM(decision IN ('learn','perform')),0) FROM opportunities "
                                     "WHERE reported_at>=? GROUP BY source", (int(since),)).fetchall()
        except Exception:
            return "pooled none"
        # reported / decided / bought: reported but undecided = too old when read (BRAIN_MAX_POOL_AGE_S), the brain's
        # own sources, or the same coin from the same source within 6 h
        sup, self.trader.supp = self.trader.supp, {"read": set(), "old": set(), "own": set()}
        try:
            two = self.conn.execute("SELECT COUNT(*) FROM brain_opps WHERE kind='pool' AND ref LIKE 'opp%+' AND ts>=?",
                                    (int(since),)).fetchone()[0]
        except Exception:
            two = 0
        return ("pooled reported/decided/bought " + (", ".join(f"{r[0]} {r[1]}/{r[2]}/{r[3]}" for r in rows) or "none")
                + f", supplemented read {len(sup['read'])} (old {len(sup['old'])}, own/bought {len(sup['own'])}) decided again {two}")

    def _candles(self, conn, mint: str, lo: int, hi: int, allow_net: bool = True) -> list:
        """The learning round's candles: from the caches only; a window that is missing goes to the fetch thread
        and the replay tries again later. GeckoTerminal (4 s a call, shared) held the 2-s loop for most of every
        minute, so whale buys were read minutes late (18.10.: brain lag 289-331 s on top of the tracker's)."""
        rows = learner._default_candles(conn, mint, lo, hi, allow_net=False)
        if rows or not allow_net:
            return rows
        with self._want_lock:
            if len(self._want) < 200:
                self._want.setdefault((mint, int(lo), int(hi)), None)
        return []

    def _fetch_loop(self, stop: threading.Event) -> None:
        try:
            conn = store.connect(self.s.db_path)
        except Exception as e:
            log.exception("candle fetch: %s", e)
            return
        while not stop.is_set():
            with self._want_lock:
                job = next(iter(self._want), None)
                if job is not None:
                    del self._want[job]
            if job is None:
                stop.wait(2)
                continue
            try:
                learner._default_candles(conn, *job, allow_net=True)  # stored in ct_candles for the next round
            except Exception as e:
                log.debug("candles %s: %s", job[0][:6], e)

    def _label_loop(self, stop: threading.Event) -> None:
        """labels.py in its own thread and connection: its DexScreener batches must not hold up the 2-s loop."""
        try:
            lb = labels.Labeler(store.connect(self.s.db_path), self.s)
        except Exception as e:
            log.exception("labels: %s", e)
            return
        while not stop.is_set():
            try:
                lb.step()
            except Exception as e:
                log.exception("labels: %s", e)
            stop.wait(60)
        try:
            lb.save()
        except Exception:
            pass

    def hour_line(self, now: float) -> bool:
        """The "brain hour:" line, first thing in the loop and on its own: on the clock hour, kept in the DB (deploys
        restart the service often, a timer from the start never reached a full hour), and an error in a later step
        or a long learning round no longer swallows it (no line at 05:00 on 18.10.)."""
        hour = int(now) // HEARTBEAT_S
        try:
            if hour == store.get(self.conn, "heartbeat_hour"):
                return False
            store.put(self.conn, "heartbeat_hour", hour)
            log.info("brain hour: %s", self.heartbeat(hour * HEARTBEAT_S - HEARTBEAT_S))
            return True
        except Exception as e:
            log.exception("brain hour line: %s", e)
            return False

    def save(self) -> None:
        if self.dirty:
            self.model.save(self.s.model_path)
            self.xm.save(self.s.exit_model_path)
            self.pm.save(self.s.pool_model_path)
            store.put(self.conn, "model_saved_at", int(time.time()))
            self.dirty = False

    def run(self, stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()
        if not self.s.enabled:
            log.info("brain disabled (BRAIN_ENABLED=false)")
            stop.wait()
            return
        log.info("brain started: paper account, %d plans, model with %d tokens, %.0f outcomes",
                 len(PLANS), self.model.d - 1, self.model.n)
        if not self.s.model_path.exists():
            log.info("no saved model yet (%s): starting from the prior and learning from copytrade's replays; "
                     "the service keeps running", self.s.model_path)
        self.hour_line(time.time())  # before the warm start, which can take minutes
        try:
            r = self.learn()
            log.info("prior knowledge: %d replays learned", r["prior"])
        except Exception as e:
            log.exception("warm start failed: %s", e)
        self.save()
        threading.Thread(target=self._fetch_loop, args=(stop,), name="brain-candles", daemon=True).start()
        if self.s.labels != "off":
            threading.Thread(target=self._label_loop, args=(stop,), name="brain-labels", daemon=True).start()
        prev = time.time()
        last = {"price": 0.0, "learn": time.time(), "save": time.time(), "eq": 0.0, "diary": 0.0}
        while not stop.is_set():
            now = time.time()
            self.max_gap = max(self.max_gap, now - prev)
            prev = now
            self.hour_line(now)
            try:
                self.trader.scan()
                if now - last["price"] >= self.s.price_every_s:
                    last["price"] = now
                    self.trader.manage()
                else:
                    self.trader.manage_curves()
                if now - last["eq"] >= EQUITY_EVERY_S:
                    last["eq"] = now
                    self.alarm(self.trader.equity_snapshot(), now)
                if now - last["learn"] >= self.s.learn_every_s:
                    last["learn"] = now
                    t0 = time.time()
                    r = self.learn(self._candles)
                    self.learn_s.append(time.time() - t0)
                    if r["prior"] or r["evaluated"]:
                        log.info("learned: %d prior, %d replayed opportunities", r["prior"], r["evaluated"])
                if now - last["diary"] >= 300:
                    last["diary"] = now
                    self.save()  # the diary reads the model; make sure what it says is on disk too
                    if journal.daily(self.conn, self.model, self.s):
                        log.info("diary written")
                if now - last["save"] >= SAVE_EVERY_S:
                    last["save"] = now
                    self.save()
            except Exception as e:  # keep running: a DB lock or a bad row must not kill the service
                log.exception("brain loop: %s", e)
            stop.wait(self.s.scan_every_s)
        self.save()
        log.info("brain stopped")
