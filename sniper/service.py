"""The sniper service: one PumpPortal websocket, one tick loop, one worker thread for stage-2 checks.
Started by the launcher as service "sniper" (python -m sniper run, log logs/sniper.log)."""
import asyncio
import json
import logging
import os
import queue
import threading
import time

from core import http
from sniper import feed, store
from sniper.engine import Engine
from sniper.enrich import Budget, Enricher

log = logging.getLogger("sniper")


class Service:
    def __init__(self, cfg, db_path=None, record=None, verbose_decisions=False):
        self.cfg, self.db_path = cfg, db_path
        if not os.getenv("MAYHAM_SERVICE"):
            http.set_service("sniper")  # the launcher sets it; foreground runs (watch) get the same budgets
        self.conn = store.connect(db_path)
        self.budget = Budget({"rpc": cfg.rpc_per_min, "api": cfg.pump_api_per_min, "deep": cfg.deep_per_min})
        self.enricher = Enricher(cfg, self.budget)
        self._sol = [0.0, 0.0]
        self.engine = Engine(cfg, self.conn, self.enricher, feed.make_quote_fn(cfg), self.sol_usd)
        self.engine.tiers = store.tier_map(self.conn)
        self.q = queue.Queue()
        self.stop = threading.Event()
        self.record = open(record, "a") if record else None
        self.connected = False
        self.ws_msgs = 0
        self._dex_conn = None
        self._hb_conn = store.connect(db_path)

    def sol_usd(self):
        """Cached SOL price; never blocks (the event loop calls this). Refreshed by sol_round."""
        return self._sol[0]

    def sol_round(self):
        v = feed.sol_usd()
        if v:
            self._sol[:] = [v, time.time()]

    # ---------------------------------------------------------------- worker (stage 2, blocking network)
    def worker(self):
        conn = store.connect(self.db_path)
        while not self.stop.is_set():
            try:
                st = self.q.get(timeout=1)
            except queue.Empty:
                continue
            try:
                if time.time() - (st.queued_ts or 0) > self.cfg.max_queue_sec:
                    self.engine.skip_budget(st)
                    continue
                enr = self.enricher.run(conn, st)
                self.engine.finalize(st, enr, conn=conn)
            except Exception:  # noqa: BLE001 - one broken launch must not stop the worker
                log.exception("stage 2 for %s failed", st.mint)
                self.engine.skip_budget(st)

    # ---------------------------------------------------------------- websocket
    async def ws_loop(self):
        try:
            import websockets
        except ImportError:
            log.error("Paket 'websockets' fehlt (pip install -r requirements.txt); kein Launch-Datenstrom")
            while not self.stop.is_set():
                await asyncio.sleep(5)
            return
        backoff = 2
        while not self.stop.is_set():
            try:
                async with websockets.connect(self.cfg.pumpportal_url, ping_interval=20, ping_timeout=20,
                                              max_size=2 ** 22, open_timeout=15) as ws:
                    self.connected, backoff = True, 2
                    await ws.send(json.dumps({"method": "subscribeNewToken"}))
                    await ws.send(json.dumps({"method": "subscribeMigration"}))
                    with self.engine.lock:
                        resub = {m for m, s in self.engine.launches.items() if s.subscribed and s.source == "pumpportal"}
                        self.engine.to_sub |= resub
                    log.info("PumpPortal verbunden (%d Coins im Trade-Abo)", len(resub))
                    sender = asyncio.create_task(self._sender(ws))
                    try:
                        async for raw in ws:
                            now = time.time()
                            self.ws_msgs += 1
                            if self.record:
                                self.record.write(json.dumps({"t": round(now, 3), "m": raw if isinstance(raw, str) else raw.decode()}) + "\n")
                            m = feed.parse(raw)
                            if m:
                                self.engine.handle(m, now)
                            if self.stop.is_set():
                                break
                    finally:
                        sender.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - reconnect with back-off
                log.warning("PumpPortal getrennt (%s), neuer Versuch in %d s", e, backoff)
            self.connected = False
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    async def _sender(self, ws):
        while True:
            await asyncio.sleep(0.25)
            with self.engine.lock:
                sub, unsub = self.engine.to_sub, self.engine.to_unsub
                self.engine.to_sub, self.engine.to_unsub = set(), set()
            for msg in feed.sub_msgs(sub - unsub, unsub - sub):
                await ws.send(msg)

    # ---------------------------------------------------------------- periodic loops
    async def tick_loop(self):
        while not self.stop.is_set():
            await asyncio.sleep(1)
            try:
                for st in self.engine.tick():
                    # launches the stream already marks as bundled only get a deep look when nothing better waits
                    if st.stage1_reason and self.q.qsize() > 0:
                        self.engine.skip_budget(st)
                    elif self.q.qsize() < 3 and self.budget.take("deep", 1):
                        self.q.put(st)
                    else:
                        self.engine.skip_budget(st)
            except Exception:  # noqa: BLE001
                log.exception("tick failed")

    async def every(self, sec, fn, first=0):
        await asyncio.sleep(first)
        while not self.stop.is_set():
            try:
                await asyncio.to_thread(fn)
            except Exception:  # noqa: BLE001
                log.exception("%s failed", getattr(fn, "__name__", fn))
            await asyncio.sleep(sec)

    def dex_round(self):
        mints = self.engine.due_external()
        if mints:
            if self._dex_conn is None:
                self._dex_conn = store.connect(self.db_path)
            self.engine.apply_external(feed.dex_prices(mints, self.sol_usd()), conn=self._dex_conn)

    def gt_round(self):
        for p in feed.new_pools(self.sol_usd()):
            self.engine.add_external(p["mint"], p["created_ts"], p["symbol"], p["name"], p["pool"], p["mcap_sol"],
                                     info=p["info"])

    def tiers_round(self):
        self.engine.tiers = store.tier_map(self.conn)

    def heartbeat(self):
        """Service status for web/ and cli/ (they cannot see this process's memory): sniper_state 'service_status'."""
        with self.engine.lock:
            subs = sum(1 for s in self.engine.launches.values() if s.subscribed)
            n = len(self.engine.launches)
        now = time.time()
        budget = {k: {"used_last_min": self.budget.per_min[k] - self.budget.left(k, now), "per_min": self.budget.per_min[k],
                      "total": self.budget.total[k]} for k in self.budget.per_min}
        store.set_state(self._hb_conn, "service_status", json.dumps({
            "ts": int(now), "connected": self.connected, "ws_msgs": self.ws_msgs, "subscribed": subs, "in_memory": n,
            "queue": self.q.qsize(), "open_positions": len(self.engine.book.open), "budget": budget,
            "counters": dict(self.engine.counters), "pump_api_ok": self.enricher.pump_ok}))

    def prune_round(self):
        store.prune(self.conn, self.cfg.keep_days)

    def report(self):
        e = self.engine
        with e.lock:
            subs = sum(1 for s in e.launches.values() if s.subscribed)
            n = len(e.launches)
        c = e.counters
        log.info("Stand: %d Launches, %d Trades, %d Migrationen | Stufe 1: %d, tief geprüft: %d, Käufe: %d | "
                 "%d im Speicher, %d im Trade-Abo, Warteschlange %d | Calls gesamt RPC %d, pump.fun %d | offen %d, "
                 "Papier-Guthaben %.3f SOL", c["creates"], c["trades"], c["migrations"], c["stage1"], c["stage2"],
                 c["entries"], n, subs, self.q.qsize(), self.budget.total["rpc"], self.budget.total["api"],
                 len(e.book.open), e.book.balance(self.conn))

    async def main(self, duration=None):
        threading.Thread(target=self.worker, daemon=True, name="sniper-stage2").start()
        tasks = [asyncio.create_task(self.ws_loop()), asyncio.create_task(self.tick_loop()),
                 asyncio.create_task(self.every(300, self.sol_round)),
                 asyncio.create_task(self.every(10, self.heartbeat, 1)),
                 asyncio.create_task(self.every(self.cfg.dex_every_sec, self.dex_round, 10)),
                 asyncio.create_task(self.every(600, self.tiers_round, 600)),
                 asyncio.create_task(self.every(3600, self.prune_round, 120)),
                 asyncio.create_task(self.every(300 if duration is None else 60, self.report, 60))]
        if self.cfg.use_geckoterminal:
            tasks.append(asyncio.create_task(self.every(self.cfg.gt_every_sec, self.gt_round, 30)))
        try:
            if duration:
                await asyncio.sleep(duration)
            else:
                await asyncio.gather(*tasks)
        finally:
            self.stop.set()
            for t in tasks:
                t.cancel()
            self.connected = False
            try:
                self.heartbeat()
            except Exception:  # noqa: BLE001
                pass
            self.report()
            if self.record:
                self.record.close()


def run(cfg, db_path=None, duration=None, record=None):
    svc = Service(cfg, db_path, record)
    log.info("Sniper startet (Papiermodus). RPC %s, Budget %d RPC/min, %d pro Launch, %d tiefe Prüfungen/min",
             http.redact(cfg.rpc_url), cfg.rpc_per_min, cfg.max_rpc_per_launch, cfg.deep_per_min)
    try:
        asyncio.run(svc.main(duration))
    except KeyboardInterrupt:
        pass
    return svc


def replay(cfg, path, db_path=None, speed=None):
    """Feed recorded websocket messages through the engine with their original times (no network:
    stage 2 uses the stream only). For tuning the detection on real launches."""
    conn = store.connect(db_path)
    eng = Engine(cfg, conn, enricher=None, quote_fn=None, sol_usd_fn=lambda: 0.0)
    eng.tiers = store.tier_map(conn)
    last_tick = None
    n = 0
    with open(path) as f:
        for line in f:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            t = rec["t"]
            m = feed.parse(rec["m"])
            if m:
                eng.handle(m, t)
                n += 1
            if last_tick is None or t - last_tick >= 1:
                for st in eng.tick(t):
                    eng.finalize(st, {"replay": True}, now=t)
                last_tick = t
    end = (last_tick or time.time()) + 3700
    for k in range(0, 3700, 5):
        for st in eng.tick((last_tick or 0) + k):
            eng.finalize(st, {"replay": True}, now=(last_tick or 0) + k)
    return eng, n, end
