"""Launch pipeline, independent of the network so tests can drive it with fixture messages.

  create msg  -> LaunchState, subscribe its trades
  trade msg   -> update curve, holders, insider sells; exit rules of an open paper position
  tick(now)   -> stage 1 after SNIPER_DECIDE_SEC (free): skip or queue for stage 2;
                 outcomes after 1/5/15/60 min (skipped launches too); unsubscribe quiet launches
  finalize()  -> stage 2 with the enrichment: bundle/insider verdict, score, safety gate, paper entry
"""
import json
import logging
import threading
import time
from collections import OrderedDict, deque

from sniper import opportunity, research, store
from sniper.analyze import analyze
from sniper.paper import PaperBook
from sniper.state import LaunchState, curve_progress

log = logging.getLogger("sniper")

ORPHAN_SEC, ORPHAN_PER_MINT, ORPHAN_MINTS = 30.0, 40, 20000   # trades held for a create that comes later

SKIP_LABELS = {
    "activity": "zu wenig Aktivität",
    "dev_sold": "Dev verkauft schon",
    "bundle": "Bundle beim Launch",
    "insider": "Insider / gemeinsame Geldquelle",
    "hard": "harte Warnung (Authority, Rug-Schutz)",
    "risk": "Risiko zu hoch",
    "score": "Score zu niedrig",
    "safety": "Rug-Schutz",
    "rug_p": "Rug-Modell (Wahrscheinlichkeit zu hoch)",
    "capacity": "technische Obergrenze oder kein Fill",
    "budget": "API-Budget erschöpft, nicht tief geprüft",
    "external": "Pool außerhalb pump.fun (nur beobachtet)",
    "no_stream": "kein Trade-Datenstrom für diese Plattform (bonk)",
    "no_price": "kein Preis für den Einstieg",
    "unverified": "Launch-Daten nicht prüfbar (pump.fun und RPC ohne Antwort)",
    "market": "Marktphase (Risk-off, höherer Mindest-Score)",
    "no_trades": "keine Trades gesehen (auch beim zweiten Blick)",
}


def _load_market():
    try:
        import market
        return market
    except Exception as e:  # noqa: BLE001 - the sniper works without market/
        log.info("market/ not available, no market gate: %s", e)
        return None


_market = _load_market()


def market_gate(conn, st, a, reason, cfg, now=None):
    """Optional market gate (MARKET_SNIPER_GATE, off by default): risk-off raises the minimum score, a hot
    narrative with better-than-average launches lowers it. Only touches 'score too low' or a clean pass."""
    if _market is None or reason not in (None, "score"):
        return reason
    try:
        adj = _market.sniper_adjust(conn, st.name, st.symbol, now)
    except Exception as e:  # noqa: BLE001
        log.warning("market.sniper_adjust %s failed: %s", st.mint[:6], e)
        return reason
    if not adj.get("delta"):
        return reason
    need = cfg.min_score + adj["delta"]
    if a.score < need:
        if reason is None:
            a.reasons.append({"check": "market", "risk": 0.0, "text": f"Score {a.score:.0f} unter {need:.0f}: {adj['why']}"})
        return "market" if reason is None else reason
    if reason == "score":
        a.positives.append(f"Mindest-Score {need:.0f} statt {cfg.min_score:.0f}: {adj['why']}")
    return None


class Engine:
    def __init__(self, cfg, conn, enricher=None, quote_fn=None, sol_usd_fn=None):
        self.cfg, self.conn = cfg, store.ensure_schema(conn)
        self.enricher, self.quote_fn = enricher, quote_fn
        self.sol_usd_fn = sol_usd_fn or (lambda: 0.0)
        self.launches = {}
        self.lock = threading.RLock()
        self.to_sub, self.to_unsub = set(), set()
        self.tiers = {}
        if not store.get_state(self.conn, "outcome_fix_ts"):
            # from here on max/min_ret_60m start at the decision price; research trusts them only after this
            store.set_state(self.conn, "outcome_fix_ts", str(int(time.time())))
        store.relabel_rugs(self.conn, research.MIN_BASE_MCAP)
        self._rug_model = (0.0, None, None)
        self.trades_live = True
        # the RPC program stream carries every pump.fun trade anyway: quiet launches stay "subscribed", so their
        # outcomes come from the curve itself instead of DexScreener quotes of untraded coins
        self.free_stream = False
        self.learned = None        # accepted rule from sniper.research (refreshed by the service), None = fixed rules
        self.stream_pools = None   # pools whose trades we receive (None = all); the RPC log stream decodes pump only
        self.book = PaperBook(cfg, conn)
        self.counters = {"creates": 0, "trades": 0, "migrations": 0, "stage1": 0, "stage2": 0, "entries": 0}
        # stream coverage (plan 18.10., goal >= 90 %): (ts, had trades) per pump decision and per migration
        self.cover = {"decide": deque(maxlen=20000), "migrate": deque(maxlen=5000), "migrate_instant": deque(maxlen=5000)}
        # launches decided without a trade that got their first one later: (decision time, seconds after it).
        # Tells "the stream missed them" (many, early) from "nobody traded them" (few)
        self.late_first = deque(maxlen=20000)
        self.orphans, self._orphan_pruned = OrderedDict(), 0.0   # trades that came before their create
        self._resume()

    # ------------------------------------------------------------------ restart
    def _resume(self, now=None):
        """Re-attach unfinished launches (outcomes still due) and open positions after a restart."""
        now = now or time.time()
        rows = self.conn.execute(
            "SELECT * FROM sniper_launches WHERE done=0 AND decided_ts >= ?", (now - self.cfg.max_watch_sec,)).fetchall()
        open_mints = set(self.book.open)
        for r in rows:
            st = self._stub(r)
            st.subscribed = r["mint"] in open_mints and not st.migrated
            self.launches[r["mint"]] = st
        for m in open_mints - set(self.launches):
            r = self.conn.execute("SELECT * FROM sniper_launches WHERE mint=?", (m,)).fetchone()
            if r:
                self.launches[m] = self._stub(r)
            else:   # position without a launch row: still watched (price from DexScreener/Jupiter, time exits)
                p = self.book.open[m]
                self.launches[m] = LaunchState(mint=m, created_ts=p["opened_ts"], source="pumpportal", pool="pump",
                                               symbol=p.get("symbol") or "", creator="", create_sig="")
                self.launches[m].decision, self.launches[m].stage, self.launches[m].done = "enter", 2, True
        for m in open_mints:
            if m in self.launches and not self.launches[m].migrated:
                self.launches[m].subscribed = True
                self.to_sub.add(m)
        self.conn.execute("UPDATE sniper_launches SET done=1, outcome_src=COALESCE(outcome_src,'restart') "
                          "WHERE done=0 AND decided_ts < ?", (now - self.cfg.max_watch_sec,))
        self.conn.commit()

    def _stub(self, r):
        st = LaunchState(mint=r["mint"], created_ts=r["created_ts"] or r["decided_ts"], source=r["source"] or "pumpportal",
                         pool=r["pool"] or "pump", symbol=r["symbol"] or "", creator=r["creator"] or "",
                         create_sig=r["create_sig"] or "")
        st.stage, st.decision, st.decided_ts = r["stage"] or 1, r["decision"] or "skip", r["decided_ts"]
        st.base_mcap_sol = r["mcap_decide_sol"]
        # no live price after a restart: 0 until a trade or DexScreener delivers one (the stored high as current
        # price sold open positions at a fake take-profit on the first tick)
        st.mcap_sol = 0.0
        base = st.base_mcap_sol or 0.0
        st.max_mcap_sol = base * (1 + r["max_ret_60m"]) if base and r["max_ret_60m"] is not None else base
        st.min_mcap_sol = base * (1 + r["min_ret_60m"]) if base and r["min_ret_60m"] is not None else base
        st.migrated = bool(r["migrated"])
        for h in self.cfg.horizons_min:
            v = r[f"ret_{h}m"]
            if v is not None and st.base_mcap_sol:
                st.outcomes[h] = st.base_mcap_sol * (1 + v)
        try:
            feats = json.loads(r["features_json"] or "{}")
            st.insiders = feats.get("insiders") or {}
        except ValueError:
            pass
        return st

    # ------------------------------------------------------------------ stream
    def _orphan(self, mint, m, now):
        q = self.orphans.get(mint)
        if q is None:
            q = self.orphans[mint] = deque(maxlen=ORPHAN_PER_MINT)
        q.append((now, m))
        while len(self.orphans) > ORPHAN_MINTS:
            self.orphans.popitem(last=False)
        if now - self._orphan_pruned > ORPHAN_SEC:
            self._orphan_pruned = now
            for k in [k for k, v in self.orphans.items() if not v or now - v[-1][0] > ORPHAN_SEC]:
                del self.orphans[k]

    def handle(self, m, now=None):
        now = now or time.time()
        kind = (m.get("txType") or "").lower()
        mint = m.get("mint")
        if not mint:
            return None
        with self.lock:
            if kind == "create":
                if mint in self.launches:
                    return None
                st = LaunchState(mint=mint, created_ts=now, source="pumpportal")
                st.apply_create(m, now)
                self.launches[mint] = st
                self.to_sub.add(mint)
                self.counters["creates"] += 1
                early = self.orphans.pop(mint, None)
                if early:
                    st.created_ts = min(st.created_ts, early[0][0])   # the create happened before its slot's trades
                    for t, om in early:
                        if now - t <= ORPHAN_SEC and st.apply_trade(om, t) is not None:
                            self.counters["trades"] += 1
                            self.counters["orphans_replayed"] = self.counters.get("orphans_replayed", 0) + 1
                return st
            st = self.launches.get(mint)
            if st is None and kind == "migrate":
                # a launch no longer in memory (restart, finished): its row still gets the migration time, which
                # the decision maker reads as the coin's age on the AMM
                r = self.conn.execute("SELECT migrated_ts FROM sniper_launches WHERE mint=?", (mint,)).fetchone()
                if r is not None and not r[0]:
                    store.update_launch(self.conn, mint, migrated=1, migrated_ts=int(now))
                return None
            if st is None:
                # a trade of the create's own slot can reach us before the create (second stream, bundles): keep
                # it a few seconds, the create replays it (PROINU 18.10.: a 74 % buy in the create slot was lost)
                if kind in ("buy", "sell"):
                    self._orphan(mint, m, now)
                return None
            if kind == "migrate" or m.get("pool") in ("pump-amm", "raydium") and kind not in ("buy", "sell"):
                if st.migrated and st.migrated_ts:
                    return st          # the same migration from the second stream
                st.migrated, st.migrated_ts = True, now
                self.counters["migrations"] += 1
                if st.pool == "pump":
                    # a curve the create alone completed (dev buys all of it, 18.10. "Masche") migrates with no
                    # curve trade at all: nothing a stream can see, so it is counted apart
                    if not st.n_seen and (curve_progress(st.v_tok0) or 0) >= 0.99:
                        self.cover["migrate_instant"].append((now, True))
                    else:
                        self.cover["migrate"].append((now, st.n_seen > 0))
                if st.decided_ts:
                    store.update_launch(self.conn, mint, migrated=1, migrated_ts=int(now))
                return st
            if kind in ("buy", "sell"):
                if st.migrated:
                    return st
                if st.apply_trade(m, now) is None:
                    self.counters["dupes"] = self.counters.get("dupes", 0) + 1
                    return st          # duplicate (both RPC streams, replay) or the dev buy of the create transaction
                self.counters["trades"] += 1
                if st.zero_decide_ts and not st.late_seen:
                    st.late_seen = True
                    self.late_first.append((st.zero_decide_ts, now - st.zero_decide_ts))
                if mint in self.book.open:
                    self.book.check(self.conn, st, now, self.quote_fn)
                return st
        return None

    def curve_live(self, since):
        """Curve state of every launch that traded after `since` (for store.save_curve_live)."""
        with self.lock:
            return [{"mint": st.mint, "ts": st.last_trade_ts, "pool": st.pool, "price_sol": st.price_sol,
                     "v_sol": st.v_sol, "mcap_sol": st.mcap_sol, "n_trades": st.n_seen, "complete": st.migrated}
                    for st in self.launches.values() if st.last_trade_ts > since and st.price_sol > 0]

    def block_due(self, now, fast_sec=60, wait_sec=10):
        """Decided launches that migrated within `fast_sec` of their creation and whose launch-block buyers are
        not read yet (the curve stream never sees those AMM buys)."""
        with self.lock:
            out = [st for st in self.launches.values()
                   if st.migrated and not st.block_checked and st.decided_ts and st.pool == "pump"
                   and st.migrated_ts and st.migrated_ts - st.created_ts <= fast_sec and now - st.migrated_ts >= wait_sec]
            for st in out:
                st.block_checked = True
            return out

    def coverage(self, now, window=3600):
        """Share of pump.fun launches with at least one trade at the decision, and of migrations whose run we saw."""
        out = {}
        with self.lock:
            for k, q in self.cover.items():
                xs = [ok for t, ok in q if now - t <= window]
                out[k] = {"n": len(xs), "share": round(sum(xs) / len(xs), 3) if xs else None}
            # decisions at least 5 min old: how many without a trade got one within 5 min after all
            old = [ok for t, ok in self.cover["decide"] if 300 <= now - t <= window + 300]
            late = sum(1 for t, dt in self.late_first if 300 <= now - t <= window + 300 and dt <= 300)
            zero = sum(1 for ok in old if not ok)
            seen = sum(old)
            out["late"] = {"zero": zero, "late_5m": late, "never_5m": zero - late,
                           "share_of_traded": round(seen / (seen + late), 3) if seen + late else None}
        return out

    def attach_wallet(self, mint, sig, wallet):
        with self.lock:
            st = self.launches.get(mint)
            ok = bool(st and st.attach_wallet(sig, wallet))
            self.counters["wallets_attached"] = self.counters.get("wallets_attached", 0) + int(ok)
            return ok

    def add_external(self, mint, created_ts, symbol="", name="", pool="", mcap_sol=0.0, now=None, info=None):
        """A new pool outside pump.fun (GeckoTerminal). Watched for outcomes; traded only if enabled."""
        now = now or time.time()
        with self.lock:
            if mint in self.launches or self.conn.execute("SELECT 1 FROM sniper_launches WHERE mint=?", (mint,)).fetchone():
                return None
            st = LaunchState(mint=mint, created_ts=created_ts or now, source="geckoterminal", pool=pool or "dex",
                             symbol=symbol, name=name)
            st.subscribed = False
            st.mcap_sol = st.mcap0_sol = st.max_mcap_sol = st.min_mcap_sol = mcap_sol
            st.external_mcap_sol, st.external_ts = mcap_sol, now
            st.ext_info = info or {}
            self.launches[mint] = st
            return st

    # ------------------------------------------------------------------ periodic
    def tick(self, now=None):
        """Returns the launches to check in depth (best first)."""
        now = now or time.time()
        cands = []
        with self.lock:
            for mint, st in list(self.launches.items()):
                if st.decision == "pending" and now - st.created_ts >= self.cfg.decide_sec:
                    if not st.n_seen and st.source == "pumpportal" and st.pool == "pump" and self.trades_live \
                            and now - st.created_ts < self.cfg.recheck_sec:
                        if not getattr(st, "_deferred", False):
                            st._deferred = True       # no trade yet: a second look later instead of "activity"
                            self.counters["deferred"] = self.counters.get("deferred", 0) + 1
                        continue
                    if getattr(st, "_deferred", False) and st.n_seen:
                        self.counters["deferred_found"] = self.counters.get("deferred_found", 0) + 1
                    self._stage1(st, now)
                    if st.decision == "queued":
                        cands.append(st)
                self._outcomes(st, now)
                if mint in self.book.open and st.subscribed and not st.migrated and self.book.mark(st, now) > 0:
                    self.book.check(self.conn, st, now)  # on the curve: no network in the loop thread (no live
                    #                                      price yet: apply_external quotes and exits it)
                self._prune_sub(st, now)
                if st.done and mint not in self.book.open:
                    self.launches.pop(mint, None)
            subs = [s for s in self.launches.values() if s.subscribed]
            if len(subs) > self.cfg.max_subscribed:
                # drop the least interesting first: stage-1 skips, oldest first
                subs.sort(key=lambda s: (s.decision != "skip" or s.stage > 1, s.mint in self.book.open, s.created_ts))
                for s in subs[: len(subs) - self.cfg.max_subscribed]:
                    self._unsub(s)
        cands.sort(key=lambda s: -(s.prescore or 0))
        return cands

    def _unsub(self, st):
        if st.subscribed:
            st.subscribed = False
            if st.source == "pumpportal":
                self.to_unsub.add(st.mint)

    def _prune_sub(self, st, now):
        if not st.subscribed or st.mint in self.book.open:
            return
        quiet = now - (st.last_trade_ts or st.created_ts)
        if st.decision == "skip" and st.stage <= 1 and quiet >= self.cfg.quiet_unsub_sec and not self.free_stream:
            self._unsub(st)
        elif now - st.created_ts >= self.cfg.max_watch_sec or st.migrated:
            self._unsub(st)

    def rug_p(self, F, now=None):
        """Rug probability within 60 min from the rug protection's learned launch model (safety.backtest,
        retrained hourly on these launches); None without a model. The model is re-read every 10 minutes."""
        now = now or time.time()
        if now - self._rug_model[0] > 600:
            try:
                from safety import backtest, store as sstore
                self._rug_model = (now, sstore.cache_get(self.conn, "launch_model", 86400 * 2),
                                  lambda m, f: backtest.predict(m, backtest._clean(f)))  # cleaned as the training rows
            except Exception as e:  # noqa: BLE001 - the sniper works without the model
                log.debug("launch_model not available: %s", e)
                self._rug_model = (now, None, None)
        _ts, model, predict = self._rug_model
        if not model:
            return None
        try:
            return round(predict(model, F), 4)
        except Exception:  # noqa: BLE001
            return None

    def _row(self, st, a, now, decision, reason, stage):
        F = a.features
        if "rug_p" not in F:
            F["rug_p"] = self.rug_p(F, now)
        sol_usd = self.sol_usd_fn() or 0.0
        row = dict(mint=st.mint, source=st.source, pool=st.pool, symbol=st.symbol, name=st.name, uri=st.uri,
                   creator=st.creator, created_ts=int(st.created_ts), create_sig=st.create_sig,
                   create_slot=F.get("create_slot"), decided_ts=int(st.decided_ts or now), stage=stage,
                   decision=decision, skip_reason=reason, score=a.score, momentum=a.momentum, risk=a.risk,
                   bundle_flag=int(a.bundle), insider_flag=int(a.insider), hard_flag=int(a.hard),
                   n_trades=F.get("n_trades"), n_buyers=F.get("n_buyers"), n_sellers=F.get("n_sellers"),
                   buy_sol=F.get("buy_sol"), sell_sol=F.get("sell_sol"), net_sol=F.get("net_sol"),
                   dev_buy_pct=F.get("dev_buy_pct"), dev_sold_pct=F.get("dev_sold_pct"),
                   bundle_wallets=F.get("bundle_wallets"), bundle_pct=F.get("bundle_pct"), unseen_pct=F.get("unseen_pct"),
                   top10_pct=F.get("top10_pct"), fast_buyers=F.get("fast_buyers"),
                   shared_funder_groups=F.get("shared_funder_groups"), dev_launches=F.get("dev_launches"),
                   dev_launches_24h=F.get("dev_launches_24h"), known_bad=F.get("known_bad"),
                   n_ultra=F.get("n_ultra"), n_smart=F.get("n_smart"), n_good=F.get("n_good"),
                   curve_progress=F.get("curve_progress"), has_socials=F.get("has_socials"),
                   safety_risk=F.get("safety_risk"), safety_level=F.get("safety_level"), model_p=F.get("model_p"),
                   mcap_decide_sol=st.base_mcap_sol, mcap_decide_usd=(st.base_mcap_sol or 0) * sol_usd or None,
                   sol_usd=sol_usd or None, reasons_json=store.j(a.reasons[:8]), positives_json=store.j(a.positives[:6]),
                   migrated=int(st.migrated), migrated_ts=int(st.migrated_ts) if st.migrated_ts else None,
                   rug_p=F.get("rug_p"), block0_wallets=F.get("block0_wallets"),
                   fee_pct=st.fee_pct, start_mcap_sol=st.start_mcap_sol,
                   quote_mint=st.quote_mint)
        if stage >= 2:
            row["features_json"] = store.j(dict(F, momentum_parts=a.momentum_parts, insiders=a.insiders))
        return row

    def _stage1(self, st, now):
        st.decided_ts, st.stage = now, 1
        st.base_mcap_sol = st.current_mcap_sol()
        st.max_mcap_sol = st.min_mcap_sol = st.base_mcap_sol   # outcome extremes start at the decision price
        self.counters["stage1"] += 1
        if st.source != "pumpportal":
            a = analyze(st, self.cfg, None, self.tiers, now)
            info = getattr(st, "ext_info", {})
            a.momentum = a.score = float(info.get("momentum") or 0)
            st.prescore = a.score
            reason = None if self.cfg.trade_external and a.score >= self.cfg.prescore_min else "external"
            st.decision = "queued" if reason is None else "skip"
            st.queued_ts = now if reason is None else None
            store.save_launch(self.conn, self._row(st, a, now, st.decision, reason, 1))
            opportunity.report_decision(self.conn, st.mint, self.cfg)
            return
        a = analyze(st, self.cfg, None, self.tiers, now)
        st.prescore = round(a.momentum * (1 - a.risk / 100), 1)
        F = a.features
        self.counters["stage1_pump"] = self.counters.get("stage1_pump", 0) + 1
        if not F.get("n_trades"):
            self.counters["stage1_zero"] = self.counters.get("stage1_zero", 0) + 1
        if st.pool == "pump":
            self.cover["decide"].append((now, bool(F.get("n_trades"))))
            if not F.get("n_trades"):
                st.zero_decide_ts = now
        reason = None
        if st.pool == "pump" and (st.base_mcap_sol or 0) < st.base_floor():
            reason = "no_price"   # below the curve's start price: a broken quote, neither tradable nor measurable
        elif (self.stream_pools is not None and st.pool not in self.stream_pools) or \
                (not self.trades_live and not F.get("n_trades")):
            reason = "no_stream"
        elif not F.get("n_trades"):
            reason = "no_trades"  # nothing seen at the second look either: no data is no verdict on activity
        elif F["dev_sold_pct"] >= 50:
            reason = "dev_sold"
        elif self.learned and research.matches(self.learned, dict(F, mcap_decide_sol=st.base_mcap_sol, risk=a.risk)):
            st.learned = True   # the accepted research rule picks it; stage 2 still checks bundles and safety
        elif F["n_buyers"] < self.cfg.min_buyers or F["net_sol"] < self.cfg.min_inflow_sol \
                or a.momentum < self.cfg.prescore_min:
            reason = "activity"
        # verdict from the stream alone, used when the deep check cannot run (budget): the curve estimate
        # of bundled supply, or a risk that already drags the pre-score below the bar
        st.stage1_reason = "bundle" if a.bundle else ("risk" if st.prescore < self.cfg.prescore_min else None)
        st.decision = "skip" if reason else "queued"
        st.queued_ts = None if reason else now
        row = self._row(st, a, now, st.decision, reason, 1)
        row["prescore"] = st.prescore
        # what the learned rule saw (stage 2 overwrites these columns): research trains on exactly this
        row["stage1_json"] = store.j({k: row.get(k) for k in research.BASE_FEATURES})
        store.save_launch(self.conn, row)
        opportunity.report_decision(self.conn, st.mint, self.cfg)   # skips and queued ones now; stage 2 supplements

    def _outcomes(self, st, now):
        if st.decided_ts is None or st.done:
            return
        base = st.base_mcap_sol
        upd, new = {}, False
        last = max(self.cfg.horizons_min)
        for h in self.cfg.horizons_min:
            if h in st.outcomes or now < st.decided_ts + h * 60:
                continue
            v = self._outcome_mcap(st, now)
            if not v:      # no current price (restarted stub, stale or vanished pair): wait for one
                continue
            st.outcomes[h] = v
            new = True
            if base:
                upd[f"ret_{h}m"] = round(v / base - 1, 4)
        if not new:        # without a base there are no returns, but the launch must still finish (and leave memory)
            if now > st.decided_ts + (last + 15) * 60:
                # never a fresh price for the last horizon: give up without a label instead of keeping it forever
                # (or writing the last stale quote as the 60 min result)
                st.done = True
                store.update_launch(self.conn, st.mint, done=1, outcome_src="no_fresh_price")
            return
        if base:
            upd["max_ret_60m"] = round(max(st.max_mcap_sol, *st.outcomes.values()) / base - 1, 4)
            upd["min_ret_60m"] = round(min(st.min_mcap_sol or base, *st.outcomes.values()) / base - 1, 4)
        upd["mcap_max_sol"] = max(st.max_mcap_sol, *st.outcomes.values())
        upd["outcome_src"] = "stream" if st.subscribed and not st.migrated else ("dexscreener" if st.external_ts else "last_trade")
        if last in st.outcomes:
            st.done = True
            upd["done"] = 1
            r60 = upd.get(f"ret_{last}m")
            dev_sold = self.conn.execute("SELECT dev_sold_pct FROM sniper_launches WHERE mint=?", (st.mint,)).fetchone()
            dump = (dev_sold and (dev_sold[0] or 0) >= 50) or any(s >= 0.5 for w, s in st.insider_sold.items())
            if not base or (st.pool == "pump" and base < st.base_floor()):
                upd["rugged"] = None     # broken base: returns are meaningless, keep it out of every rug model
            else:
                # a crash to -90% inside the hour is a rug even if a dead-cat bounce lifts the 60m price again
                crash = upd.get("min_ret_60m") is not None and upd["min_ret_60m"] <= -0.9
                upd["rugged"] = int(r60 is not None and (r60 <= -0.8 or crash or (dump and r60 <= -0.5)))
            if st.stage >= 2:
                store.save_curve(self.conn, st.mint, [p for p in st.curve if p[0] - st.created_ts <= 900])
            if st.n_seen or st.external_ts:   # a path with movement: replays of exit rules need it
                store.save_path(self.conn, st.mint, st.created_ts, st.decided_ts, base, st.path.points())
        store.update_launch(self.conn, st.mint, **upd)
        if upd.get("done"):
            opportunity.report_result(self.conn, st.mint, self.cfg)

    def _outcome_mcap(self, st, now):
        """Market cap for an outcome at `now`: the live curve, else a DexScreener price at most 3 DexScreener rounds
        (>= 90 s) old. The last quote of a pair that stopped updating is no outcome (it was written as the result)."""
        if st.subscribed and not st.migrated and st.mcap_sol > 0:
            return st.mcap_sol
        fresh = max(90, 3 * self.cfg.dex_every_sec)
        if st.external_mcap_sol and now - (st.external_ts or 0) <= fresh:
            return st.external_mcap_sol
        return 0.0

    def due_external(self, now=None, ahead=None):
        """Mints that need a DexScreener price soon: horizon due and no live curve, or open after migration."""
        now = now or time.time()
        ahead = self.cfg.dex_every_sec if ahead is None else ahead
        out = []
        with self.lock:
            for m, st in self.launches.items():
                live = st.subscribed and not st.migrated
                if m in self.book.open and (not live or self.book.mark(st, now) <= 0):
                    out.append(m)     # also a curve position without a live price (dead curve after a restart)
                elif not live and st.decided_ts and not st.done and any(
                        h not in st.outcomes and st.decided_ts + h * 60 <= now + ahead for h in self.cfg.horizons_min):
                    out.append(m)
        return out

    def apply_external(self, prices, now=None, conn=None):
        """prices: {mint: mcap_sol} from DexScreener (missing = no pair = dead or not indexed)."""
        now = now or time.time()
        check = []
        with self.lock:
            for m, v in prices.items():
                st = self.launches.get(m)
                if st and v:
                    st.external_mcap_sol, st.external_ts = v, now
                    st.path.add(now, v)
                    if st.decided_ts is not None:
                        st.max_mcap_sol = max(st.max_mcap_sol, v)
                        st.min_mcap_sol = min(st.min_mcap_sol or v, v)
            for m in list(self.book.open):
                st = self.launches.get(m)
                if st and (st.migrated or not st.subscribed or self.book.mark(st, now) <= 0):
                    check.append(st)
        for st in check:
            # the Jupiter quote is fetched here, outside the lock (network); the exit itself runs under the lock,
            # because the stream thread checks and sells the same positions (lost updates, double sells)
            p = self.book.open.get(st.mint)
            if not p:
                continue
            n = p["remaining"]
            q = self.quote_fn(st.mint, n, "sell") if self.quote_fn and n > 0 else None
            qf = (lambda _m, tokens, _side, q=q, n=n: dict(q, out=q["out"] * tokens / n)) if q and q.get("out") else None
            with self.lock:
                if st.mint in self.book.open:
                    self.book.check(conn or self.conn, st, now, qf, quoted=self.quote_fn is not None)

    # ------------------------------------------------------------------ stage 2
    def skip_budget(self, st, now=None):
        """Not checked in depth (budget or queue age): the stage-1 verdict decides the label."""
        with self.lock:
            st.decision = "skip"
            store.update_launch(self.conn, st.mint, decision="skip",
                                skip_reason=getattr(st, "stage1_reason", None) or "budget")
            opportunity.report_decision(self.conn, st.mint, self.cfg, followup=True)

    def _gates(self, st, a, enr, p_rug, conn, now):
        """Skip reason after stage 2: the analysis, the rug model, the learned rule (replaces the score), the
        market gate (moves the score bar), then the hard duties. The duties come last so that nothing that lowers
        the bar (learned rule, hot narrative) can let an unchecked or rug-prone launch through."""
        reason = a.skip_reason(self.cfg)
        a.features["rug_p"] = p_rug
        if reason == "score" and st.learned:
            reason = None   # the accepted rule replaces the momentum score; bundles, insiders, risk still block
        reason = market_gate(conn, st, a, reason, self.cfg, now)
        if p_rug is not None and p_rug >= self.cfg.rug_p_block and reason in (None, "score", "risk", "market"):
            reason = "rug_p"
            a.reasons.append({"check": "rug_p", "risk": round(100 * p_rug, 1),
                              "text": f"Rug-Modell: {100 * p_rug:.0f} % Rug-Wahrscheinlichkeit in 60 min"})
        if reason is None and self.cfg.require_launch_data and st.source == "pumpportal" \
                and not enr.get("early") and not enr.get("replay"):
            reason = "unverified"
        return reason

    def finalize(self, st, enr, now=None, conn=None):
        """Stage 2: full analysis with the enrichment, safety gate, paper entry. Runs in the worker thread
        with its own DB connection `conn`."""
        now = now or time.time()
        conn = conn or self.conn
        with self.lock:
            st.stage = 2
            a = analyze(st, self.cfg, enr, self.tiers, now)
            st.insiders.update(a.insiders)
            for w in a.wallets:   # insiders' bags we never saw bought, so their sells can trigger the insider exit
                if w.get("role") in ("bundle", "insider") and (w.get("tokens") or 0) > 0:
                    st.insider_base.setdefault(w["wallet"], float(w["tokens"]))
            p_rug = self.rug_p(a.features, now)
        reason = self._gates(st, a, enr, p_rug, conn, now)
        self.counters["stage2"] += 1
        safety_res = None
        if reason is None and self.cfg.safety_gate and self.enricher:
            safety_res = self.enricher.safety_gate(conn, st.mint)
            enr["safety"] = safety_res
            with self.lock:
                a = analyze(st, self.cfg, enr, self.tiers, now)
            reason = self._gates(st, a, enr, p_rug, conn, now)
            if reason is None and safety_res.get("blocked"):
                reason = "safety"
        if reason is None and st.pool == "pump" and st.current_mcap_sol() < st.base_floor():
            reason = "no_price"   # a buy at "MCAP 0.0" happened on a broken curve state
        qf = None
        if reason is None and self.quote_fn:  # quote outside the lock: the stream keeps flowing meanwhile
            q = self.quote_fn(st.mint, self.cfg.size_sol - self.book._fees(), "buy")
            qf = (lambda *_: q) if q else None
        with self.lock:
            pos = None
            if reason is None:
                learned = self.learned if getattr(st, "learned", False) else None
                pos, why = self.book.enter(conn, st, a.score, now, qf, self.sol_usd_fn() or 0.0,
                                           dict(learned["exit"], learned=True) if learned else None)
                if pos is None:
                    reason = "no_price" if why and "Preis" in why else "capacity"
                    a.positives.append(f"Nicht gekauft: {why}.")
                else:
                    self.counters["entries"] += 1
            st.decision = "enter" if pos else "skip"
            row = self._row(st, a, now, st.decision, reason, 2)
            row.update(calls_rpc=enr.get("calls_rpc"), calls_api=enr.get("calls_api"), prescore=st.prescore)
            store.save_launch(conn, row)
            opportunity.report_decision(conn, st.mint, self.cfg, followup=True)
            store.save_wallets(conn, st.mint, a.wallets)
            if not pos and st.subscribed and reason in ("bundle", "insider", "hard", "dev_sold"):
                pass  # keep watching: outcomes of skipped launches show what the filter is worth
        log.info("Launch %s %s: %s%s  Score %.0f (Momentum %.0f, Risiko %.0f)%s", st.symbol, st.mint[:6],
                 "KAUF" if pos else "übersprungen", f" [{SKIP_LABELS.get(reason, reason)}]" if reason else "",
                 a.score, a.momentum, a.risk, ("  " + a.reasons[0]["text"]) if a.reasons else "")
        return a, reason, pos
