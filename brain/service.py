"""The brain as its own service (`python -m brain run`, started by mayham.py as "brain")."""
from __future__ import annotations

import logging
import threading
import time

from . import config, exits, journal, learner, store
from .model import Brain
from .plans import PLANS
from .trader import Trader

log = logging.getLogger("brain")

SAVE_EVERY_S = 600
EQUITY_EVERY_S = 60
HEARTBEAT_S = 3600  # one "brain hour:" log line per hour (ops can watch it for stalls)


def load_model(s) -> Brain:
    return Brain.load(s.model_path, len(PLANS), s.half_life_h)


def load_exit_model(s) -> Brain:
    return Brain.load(s.exit_model_path, 1, s.half_life_h)


class Service:
    def __init__(self, s=None, conn=None, **trader_kw):
        self.s = s or config.load()
        self.conn = conn or store.connect(self.s.db_path)
        self.model = load_model(self.s)
        self.xm = load_exit_model(self.s)
        notify = trader_kw.pop("notify", None)
        if notify is None:
            notify = journal.send if self.s.telegram == "trades" else (lambda text: False)
        self.trader = Trader(self.conn, self.s, self.model, notify=notify, on_close=self._learn_trade,
                             exit_model=self.xm, **trader_kw)
        self.dirty = False

    def _learn_trade(self, pos) -> None:
        if learner.learn_trade(self.conn, self.model, self.s, pos):
            self.dirty = True

    def learn(self, candles_fn=None) -> dict:
        n_prior = learner.warm_start(self.conn, self.model, self.s)
        n_eval = learner.evaluate(self.conn, self.model, self.s, candles_fn, xm=self.xm)
        n_back = learner.backfill_exits(self.conn, self.xm, self.s, candles_fn)
        if n_prior or n_eval or n_back:
            self.dirty = True
        return {"prior": n_prior, "evaluated": n_eval, "exit_backfill": n_back}

    def heartbeat(self, since: float) -> str:
        c = self.conn
        n, ent, held = c.execute("SELECT COUNT(*), COALESCE(SUM(decision='enter'),0), "
                                 "COALESCE(SUM(decision='skip' AND (wanted=1 OR explore=1)),0) FROM brain_opps WHERE ts>=?",
                                 (int(since),)).fetchone()
        n_open = c.execute("SELECT COUNT(*) FROM brain_positions WHERE status='open'").fetchone()[0]
        eq = self.trader.equity_snapshot()["equity_sol"]
        late, self.trader.too_late = self.trader.too_late, 0
        return (f"{n} opportunities ({late} whale buys arrived too late), {ent} bought, {held} wanted but blocked by a gate, {n_open} open, equity {eq:.3f} SOL, "
                f"model {self.model.d - 1} tokens / {self.model.n:.0f} outcomes, exit model {self.xm.n:.0f} minutes "
                f"(organic exits {'on' if self.trader.organic_on(time.time()) else 'off'}, "
                f"replays {exits.verdict(c)['n']})")

    def save(self) -> None:
        if self.dirty:
            self.model.save(self.s.model_path)
            self.xm.save(self.s.exit_model_path)
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
        try:
            r = self.learn()
            log.info("prior knowledge: %d replays learned", r["prior"])
        except Exception as e:
            log.exception("warm start failed: %s", e)
        self.save()
        last = {"price": 0.0, "learn": time.time(), "save": time.time(), "eq": 0.0, "diary": 0.0,
                "hour": time.time()}
        while not stop.is_set():
            now = time.time()
            try:
                self.trader.scan()
                if now - last["price"] >= self.s.price_every_s:
                    last["price"] = now
                    self.trader.manage()
                if now - last["eq"] >= EQUITY_EVERY_S:
                    last["eq"] = now
                    self.trader.equity_snapshot()
                if now - last["learn"] >= self.s.learn_every_s:
                    last["learn"] = now
                    r = self.learn()
                    if r["prior"] or r["evaluated"]:
                        log.info("learned: %d prior, %d replayed opportunities", r["prior"], r["evaluated"])
                if now - last["diary"] >= 300:
                    last["diary"] = now
                    self.save()  # the diary reads the model; make sure what it says is on disk too
                    if journal.daily(self.conn, self.model, self.s):
                        log.info("diary written")
                if now - last["hour"] >= HEARTBEAT_S:
                    last["hour"] = now
                    log.info("brain hour: %s", self.heartbeat(now - HEARTBEAT_S))
                if now - last["save"] >= SAVE_EVERY_S:
                    last["save"] = now
                    self.save()
            except Exception as e:  # keep running: a DB lock or a bad row must not kill the service
                log.exception("brain loop: %s", e)
            stop.wait(self.s.scan_every_s)
        self.save()
        log.info("brain stopped")
