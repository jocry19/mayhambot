"""The sniper service: one PumpPortal websocket, one tick loop, one worker thread for stage-2 checks.
Started by the launcher as service "sniper" (python -m sniper run, log logs/sniper.log)."""
import asyncio
import json
from collections import deque
import logging
import os
import queue
import threading
import time

from core import http
from sniper import feed, rpcws, store
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
        self.record = open(record, "a", buffering=1) if record else None  # line-buffered: survives a crash
        key = cfg.pumpportal_api_key
        self.trades_via = "pumpportal" if key and cfg.trade_source in ("auto", "pumpportal") else "rpc"
        if cfg.trade_source == "pumpportal" and not key:
            log.warning("SNIPER_TRADE_SOURCE=pumpportal ohne SNIPER_PUMPPORTAL_API_KEY: PumpPortal liefert Trades "
                        "nur mit aufgeladenem Key, nehme den RPC-Log-Datenstrom")
        self.logs = rpcws.LogStream(cfg.ws_url, self._on_rpc_trade, cfg.ws_max_subs, subs_per_sec=cfg.ws_subs_per_sec,
                                    mode=cfg.trade_sub_mode, program=rpcws.PUMP_PROGRAM)
        self.logs.on_mode = self._save_mode
        try:
            saved = json.loads(store.get_state(self.conn, "rpc_trade_mode") or "{}")
        except ValueError:
            saved = {}
        if cfg.trade_sub_mode == "auto" and saved.get("mode") == "program" and time.time() - saved.get("ts", 0) < 86400:
            self.logs.mode = "program"   # the public RPC refused per-coin subscriptions recently: start where we were
        srcs = cfg.launch_sources.lower()
        self.use_pp = srcs in ("both", "pumpportal")
        self.creates = rpcws.LogStream(cfg.ws_url, lambda m: self._on_create(m, "rpc"), 1, parser=rpcws.parse_creates,
                                       name="RPC-Launch-Datenstrom", stale_sec=cfg.stale_sec) \
            if srcs in ("both", "rpc") else None
        if self.creates:
            self.creates.subscribe({cfg.create_mention})
        self.launch_log = {"pumpportal": deque(maxlen=5000), "rpc": deque(maxlen=5000)}   # times of creates seen
        self.first_log = {"pumpportal": deque(maxlen=5000), "rpc": deque(maxlen=5000)}    # creates this source delivered first
        self._warned_reject = False
        self.engine.stream_pools = None if self.trades_via == "pumpportal" else {"pump"}
        self.connected = False
        self.pp_ws = None
        self.pp_fails = 0
        self.trade_samples = deque(maxlen=120)    # (ts, trades, rpc trades) every heartbeat, for trades per minute
        self.rpc_trades = 0
        self.started = time.time()
        self._blind_warned = 0.0
        self._research_ts = None
        self.ws_msgs = 0
        self._dex_conn = None
        self._hb_conn = store.connect(db_path)

    def _on_create(self, m, src):
        now = time.time()
        if self.record and src == "rpc":
            self.record.write(json.dumps({"t": round(now, 3), "m": json.dumps(m)}) + "\n")
        self.launch_log[src].append(now)
        if self.engine.handle(m, now) is not None:
            self.first_log[src].append(now)

    def _save_mode(self, mode):
        store.set_state(self._hb_conn if hasattr(self, "_hb_conn") else self.conn, "rpc_trade_mode",
                        json.dumps({"mode": mode, "ts": int(time.time())}))

    def launch_rates(self, now=None):
        now = now or time.time()
        out = {}
        for src in ("pumpportal", "rpc"):
            seen = sum(1 for t in self.launch_log[src] if now - t <= 300)
            first = sum(1 for t in self.first_log[src] if now - t <= 300)
            connected = self.connected if src == "pumpportal" else bool(self.creates and self.creates.connected)
            out[src] = {"enabled": self.use_pp if src == "pumpportal" else self.creates is not None,
                        "connected": connected, "per_min": round(seen / 5, 1), "first_per_min": round(first / 5, 1),
                        "last_ts": int(self.launch_log[src][-1]) if self.launch_log[src] else None}
        return out

    def _on_rpc_trade(self, m):
        now = time.time()
        if self.record:  # same format as PumpPortal lines, so replay sees the RPC trades too
            self.record.write(json.dumps({"t": round(now, 3), "m": json.dumps(m)}) + "\n")
        self.rpc_trades += 1
        self.engine.handle(m, now)

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
        if not self.use_pp:
            return
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
                url = self.cfg.pumpportal_url
                if self.cfg.pumpportal_api_key:
                    url += ("&" if "?" in url else "?") + "api-key=" + self.cfg.pumpportal_api_key
                async with websockets.connect(url, ping_interval=20, ping_timeout=20,
                                              max_size=2 ** 22, open_timeout=15) as ws:
                    self.connected, backoff = True, 2
                    if self.pp_fails:
                        log.info("PumpPortal wieder erreichbar nach %d Fehlversuchen", self.pp_fails)
                    self.pp_fails = 0
                    await ws.send(json.dumps({"method": "subscribeNewToken"}))
                    await ws.send(json.dumps({"method": "subscribeMigration"}))
                    with self.engine.lock:
                        resub = {m for m, s in self.engine.launches.items() if s.subscribed and s.source == "pumpportal"}
                        if self.trades_via == "pumpportal":
                            self.engine.to_sub |= resub
                    log.info("PumpPortal verbunden (%d Coins im Trade-Abo)", len(resub))
                    self.pp_ws = ws
                    try:
                        while True:
                            try:
                                raw = await asyncio.wait_for(ws.recv(), timeout=self.cfg.stale_sec)
                            except asyncio.TimeoutError:
                                log.warning("PumpPortal: %d s keine Nachricht, verbinde neu", self.cfg.stale_sec)
                                break
                            now = time.time()
                            self.ws_msgs += 1
                            if self.record:
                                self.record.write(json.dumps({"t": round(now, 3), "m": raw if isinstance(raw, str) else raw.decode()}) + "\n")
                            m = feed.parse(raw)
                            if m is None and self.trades_via == "pumpportal" and "only available when connecting" in str(raw):
                                if not self._warned_reject:
                                    log.warning("PumpPortal lehnt Trade-Abos ab (Key ohne 0,02 SOL Guthaben?): "
                                                "wechsle auf den kostenlosen RPC-Log-Datenstrom")
                                    self._warned_reject = True
                                if self.trades_via != "rpc":
                                    self.trades_via = "rpc"
                                    self.engine.stream_pools = {"pump"}
                                    with self.engine.lock:
                                        self.logs.subscribe({k for k, s in self.engine.launches.items()
                                                             if s.subscribed and s.pool == "pump"})
                            if m and (m.get("txType") or "").lower() == "create":
                                self._on_create(m, "pumpportal")
                            elif m:
                                self.engine.handle(m, now)
                            if self.stop.is_set():
                                break
                    finally:
                        self.pp_ws = None
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - reconnect with back-off
                self.pp_fails += 1
                # a long outage (HTTP 502 for hours) while the RPC launch source works: try every 10 minutes and
                # log only now and then instead of a warning per minute
                cap = 600 if self.pp_fails >= 10 and self.creates and self.creates.connected else 60
                backoff = min(backoff, cap)
                if self.pp_fails <= 3 or self.pp_fails % 10 == 0:
                    log.warning("PumpPortal getrennt (%s), Fehlversuch %d, neuer Versuch in %d s", e, self.pp_fails,
                                backoff)
            self.connected = False
            await asyncio.sleep(backoff)
            cap = 600 if self.pp_fails >= 10 and self.creates and self.creates.connected else 60
            backoff = min(backoff * 2, cap)

    async def route_subs(self):
        """Hand new (un)subscriptions to the active trade source. Runs on its own, so the RPC log stream gets its
        coins even while PumpPortal is down; PumpPortal-bound changes wait until its socket is back."""
        with self.engine.lock:
            sub, unsub = self.engine.to_sub, self.engine.to_unsub
            self.engine.to_sub, self.engine.to_unsub = set(), set()
            if self.trades_via != "pumpportal":
                pump = {m for m in sub if self.engine.launches.get(m) and self.engine.launches[m].pool == "pump"}
        if self.trades_via == "pumpportal":
            ws = self.pp_ws
            if ws is None:
                with self.engine.lock:  # keep them for the next connection
                    self.engine.to_sub |= sub - unsub
                    self.engine.to_unsub |= unsub - sub
                return
            for msg in feed.sub_msgs(sub - unsub, unsub - sub):
                await ws.send(msg)
        else:
            self.logs.subscribe(pump - unsub)
            self.logs.unsubscribe(unsub)

    def demote_dead_subs(self, now=None):
        """Coins whose RPC trade subscription never got confirmed (refused, over the limit) would keep a frozen
        curve price and exit on it: they fall back to DexScreener prices like unsubscribed coins."""
        if self.trades_via != "rpc" or not self.logs.connected:
            return []
        now = now or time.time()
        dead = self.logs.not_live(now)
        if not dead:
            return []
        with self.engine.lock:
            for m in dead:
                st = self.engine.launches.get(m)
                if st and st.subscribed:
                    st.subscribed = False
            self.engine.counters["subs_lost"] = self.engine.counters.get("subs_lost", 0) + len(dead)
        self.logs.unsubscribe(dead)
        return dead

    async def sub_loop(self):
        n = 0
        while not self.stop.is_set():
            await asyncio.sleep(0.25)
            n += 1
            try:
                await self.route_subs()
                if n % 20 == 0:
                    self.demote_dead_subs()
            except Exception as e:  # noqa: BLE001 - PumpPortal socket closed mid-send; ws_loop reconnects
                log.debug("Abo-Weitergabe: %s", e)

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

    def trade_rate(self, now, window=300, idx=1):
        """Trades per minute over the last `window` seconds from the heartbeat samples (idx 1 all, 2 RPC only)."""
        old = [x for x in self.trade_samples if now - x[0] <= window]
        if len(old) < 2 or old[-1][0] - old[0][0] <= 0:
            return None
        return round((old[-1][idx] - old[0][idx]) / ((old[-1][0] - old[0][0]) / 60), 1)

    def stream_health(self, now):
        """Blind = launches arrive but no trades for 5 minutes: every launch would be skipped as 'activity'.
        The engine then labels such launches 'no_stream', and the log warns every 10 minutes."""
        tpm = self.trade_rate(now)
        lpm = self._launch_status(now)["launches_per_min"]
        with self.engine.lock:
            watched = sum(1 for s in self.engine.launches.values() if s.subscribed)
        blind = bool(now - self.started >= 300 and lpm > 0 and watched > 0 and tpm == 0)
        self.engine.trades_live = not blind
        if blind and now - self._blind_warned >= 600:
            self._blind_warned = now
            log.warning("ALARM: %.1f Launches/min, aber seit 5 min keine Trades (Quelle %s, %d RPC-Abos, %d "
                        "Ablehnungen): der Sniper ist blind, Launches werden als no_stream markiert",
                        lpm, self.trades_via, self.logs.active_subs(), self.logs.rejects)
        return {"trades_per_min": tpm, "blind": blind}

    def heartbeat(self):
        """Service status for web/ and cli/ (they cannot see this process's memory): sniper_state 'service_status'."""
        now = time.time()
        self.trade_samples.append((now, self.engine.counters["trades"], self.rpc_trades))
        with self.engine.lock:
            subs = sum(1 for s in self.engine.launches.values() if s.subscribed)
            n = len(self.engine.launches)
        budget = {k: {"used_last_min": self.budget.per_min[k] - self.budget.left(k, now), "per_min": self.budget.per_min[k],
                      "total": self.budget.total[k]} for k in self.budget.per_min}
        store.set_state(self._hb_conn, "service_status", json.dumps({
            # connected = any data source open (PumpPortal, RPC launches or RPC trades); per source below
            "ts": int(now), "connected": self.any_connected(), "pumpportal_connected": self.connected,
            "trade_sources": self.trade_sources(now), "ws_msgs": self.ws_msgs, "subscribed": subs, "in_memory": n,
            "queue": self.q.qsize(), "open_positions": len(self.engine.book.open), "budget": budget,
            "counters": dict(self.engine.counters), "pump_api_ok": self.enricher.pump_ok if self.cfg.use_pump_api else None,
            "trade_source": self.trades_via, "rpc_ws_connected": self.logs.connected, "rpc_ws_subs": len(self.logs.subs),
            "rpc_ws_msgs": self.logs.msgs, "rpc_ws_mode": self.logs.mode, "rpc_ws_wanted": len(self.logs.want),
            "rpc_ws_attempts": self.logs.attempts, "rpc_ws_rejects": self.logs.rejects,
            "rpc_ws_reconnects": self.logs.reconnects,
            "learned_rule": (self.engine.learned or {}).get("conditions"), "research_ts": self._research_ts, **self.stream_health(now), **self._launch_status(now)}))

    def any_connected(self):
        return bool(self.connected or self.logs.connected or (self.creates and self.creates.connected))

    def trade_sources(self, now):
        tot, rpc = self.trade_rate(now), self.trade_rate(now, idx=2)
        return {"pumpportal": {"active": self.trades_via == "pumpportal", "connected": self.connected,
                               "trades_per_min": None if tot is None or rpc is None else round(tot - rpc, 1)},
                "rpc": {"active": self.trades_via == "rpc", "connected": self.logs.connected, "mode": self.logs.mode,
                        "subs": self.logs.active_subs(), "wanted": len(self.logs.want), "attempts": self.logs.attempts,
                        "rejects": self.logs.rejects, "reconnects": self.logs.reconnects,
                        "server_drops": self.logs.server_drops, "trades_per_min": rpc}}

    def _launch_status(self, now):
        rates = self.launch_rates(now)
        ok = [s for s, r in rates.items() if r["connected"] and r["first_per_min"] > 0]
        active = max(ok, key=lambda s: rates[s]["first_per_min"]) if ok else \
            next((s for s, r in rates.items() if r["connected"]), None)
        return {"launch_sources": rates, "active_launch_source": active,
                "launches_per_min": round(sum(r["first_per_min"] for r in rates.values()), 1)}

    def research_round(self):
        """Hourly: research on the real outcomes (own DB connection, CPU only, no network); an accepted rule is
        handed to the engine, a rule that no longer holds is withdrawn."""
        from sniper import research
        conn = store.connect(self.db_path)
        try:
            rep = research.run(conn, self.cfg, hours=self.cfg.research_hours, save=True)
            rule = research.learned_rule(conn) if self.cfg.use_learned_rule else None
        finally:
            conn.close()
        self._research_ts = rep.get("ts")
        with self.engine.lock:
            changed = (rule or {}).get("conditions") != (self.engine.learned or {}).get("conditions")
            self.engine.learned = rule
        for line in research.summary(rep, top=5).splitlines():
            log.info("Forschung: %s", line)
        if changed:
            log.warning("Gelernte Regel %s", "AKTIV (Papier): " + json.dumps(rule["conditions"]) if rule else "aus")

    def prune_round(self):
        store.prune(self.conn, self.cfg.keep_days)

    def report(self):
        e = self.engine
        with e.lock:
            subs = sum(1 for s in e.launches.values() if s.subscribed)
            n = len(e.launches)
        c = e.counters
        lr = self.launch_rates()
        log.info("Launch-Quellen (5 min): PumpPortal %s %.1f/min, RPC %s %.1f/min",
                 "verbunden" if lr["pumpportal"]["connected"] else "getrennt", lr["pumpportal"]["per_min"],
                 "verbunden" if lr["rpc"]["connected"] else "getrennt", lr["rpc"]["per_min"])
        h = self.stream_health(time.time())
        log.info("Trade-Datenstrom: %s Trades/min, Quelle %s, RPC %s (%d Abos von %d gewünscht, %d Ablehnungen)%s",
                 "?" if h["trades_per_min"] is None else h["trades_per_min"], self.trades_via, self.logs.mode,
                 self.logs.active_subs(), len(self.logs.want), self.logs.rejects, " BLIND" if h["blind"] else "")
        log.info("Stand: %d Launches, %d Trades, %d Migrationen | Stufe 1: %d, tief geprüft: %d, Käufe: %d | "
                 "%d im Speicher, %d im Trade-Abo (%s, RPC-Logs %d Abos), Warteschlange %d | Calls gesamt RPC %d, API/Metadaten %d | offen %d, "
                 "Papier-Guthaben %.3f SOL", c["creates"], c["trades"], c["migrations"], c["stage1"], c["stage2"],
                 c["entries"], n, subs, self.trades_via, len(self.logs.subs), self.q.qsize(), self.budget.total["rpc"], self.budget.total["api"],
                 len(e.book.open), e.book.balance(self.conn))

    async def main(self, duration=None):
        threading.Thread(target=self.worker, daemon=True, name="sniper-stage2").start()
        stop_ev = asyncio.Event()
        self._stop_ev = stop_ev
        tasks = [asyncio.create_task(self.ws_loop()), asyncio.create_task(self.tick_loop()),
                 asyncio.create_task(self.sub_loop()),
                 asyncio.create_task(self.logs.run(stop_ev)),
                 *([asyncio.create_task(self.creates.run(stop_ev))] if self.creates else []),
                 asyncio.create_task(self.every(300, self.sol_round)),
                 asyncio.create_task(self.every(10, self.heartbeat, 1)),
                 asyncio.create_task(self.every(self.cfg.dex_every_sec, self.dex_round, 10)),
                 asyncio.create_task(self.every(600, self.tiers_round, 600)),
                 asyncio.create_task(self.every(3600, self.prune_round, 120)),
                 asyncio.create_task(self.every(3600, self.research_round, 300)),
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
            stop_ev.set()
            for t in tasks:
                t.cancel()
            self.connected = self.logs.connected = False
            if self.creates:
                self.creates.connected = False
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
