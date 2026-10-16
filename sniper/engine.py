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

from sniper import store
from sniper.analyze import analyze
from sniper.paper import PaperBook
from sniper.state import LaunchState

log = logging.getLogger("sniper")

SKIP_LABELS = {
    "activity": "zu wenig Aktivität",
    "dev_sold": "Dev verkauft schon",
    "bundle": "Bundle beim Launch",
    "insider": "Insider / gemeinsame Geldquelle",
    "hard": "harte Warnung (Authority, Rug-Schutz)",
    "risk": "Risiko zu hoch",
    "score": "Score zu niedrig",
    "safety": "Rug-Schutz",
    "capacity": "kein Platz (Limits, Guthaben)",
    "budget": "API-Budget erschöpft, nicht tief geprüft",
    "external": "Pool außerhalb pump.fun (nur beobachtet)",
    "no_price": "kein Preis für den Einstieg",
    "unverified": "Launch-Daten nicht prüfbar (pump.fun und RPC ohne Antwort)",
}


class Engine:
    def __init__(self, cfg, conn, enricher=None, quote_fn=None, sol_usd_fn=None):
        self.cfg, self.conn = cfg, store.ensure_schema(conn)
        self.enricher, self.quote_fn = enricher, quote_fn
        self.sol_usd_fn = sol_usd_fn or (lambda: 0.0)
        self.launches = {}
        self.lock = threading.RLock()
        self.to_sub, self.to_unsub = set(), set()
        self.tiers = {}
        self.book = PaperBook(cfg, conn)
        self.counters = {"creates": 0, "trades": 0, "migrations": 0, "stage1": 0, "stage2": 0, "entries": 0}
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
        st.mcap_sol = st.max_mcap_sol = st.min_mcap_sol = r["mcap_max_sol"] or r["mcap_decide_sol"] or 0.0
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
                return st
            st = self.launches.get(mint)
            if st is None:
                return None
            if kind == "migrate" or m.get("pool") in ("pump-amm", "raydium") and kind not in ("buy", "sell"):
                st.migrated, st.migrated_ts = True, now
                self.counters["migrations"] += 1
                if st.decided_ts:
                    store.update_launch(self.conn, mint, migrated=1, migrated_ts=int(now))
                return st
            if kind in ("buy", "sell"):
                if st.migrated:
                    return st
                st.apply_trade(m, now)
                self.counters["trades"] += 1
                if mint in self.book.open:
                    self.book.check(self.conn, st, now, self.quote_fn)
                return st
        return None

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
                    self._stage1(st, now)
                    if st.decision == "queued":
                        cands.append(st)
                self._outcomes(st, now)
                if mint in self.book.open and st.subscribed and not st.migrated:
                    self.book.check(self.conn, st, now)  # on the curve: no network in the loop thread
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
        if st.decision == "skip" and st.stage <= 1 and quiet >= self.cfg.quiet_unsub_sec:
            self._unsub(st)
        elif now - st.created_ts >= self.cfg.max_watch_sec or st.migrated:
            self._unsub(st)

    def _row(self, st, a, now, decision, reason, stage):
        F = a.features
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
                   migrated=int(st.migrated))
        if stage >= 2:
            row["features_json"] = store.j(dict(F, momentum_parts=a.momentum_parts, insiders=a.insiders))
        return row

    def _stage1(self, st, now):
        st.decided_ts, st.stage = now, 1
        st.base_mcap_sol = st.current_mcap_sol()
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
            return
        a = analyze(st, self.cfg, None, self.tiers, now)
        st.prescore = round(a.momentum * (1 - a.risk / 100), 1)
        F = a.features
        reason = None
        if F["dev_sold_pct"] >= 50:
            reason = "dev_sold"
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
        store.save_launch(self.conn, row)

    def _outcomes(self, st, now):
        if st.decided_ts is None or st.done:
            return
        base = st.base_mcap_sol
        upd = {}
        for h in self.cfg.horizons_min:
            if h in st.outcomes or now < st.decided_ts + h * 60:
                continue
            v = st.current_mcap_sol()
            st.outcomes[h] = v
            if base:
                upd[f"ret_{h}m"] = round(v / base - 1, 4)
        if not upd:
            return
        last = max(self.cfg.horizons_min)
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
            upd["rugged"] = int(r60 is not None and (r60 <= -0.8 or (dump and r60 <= -0.5)))
            if st.stage >= 2:
                store.save_curve(self.conn, st.mint, [p for p in st.curve if p[0] - st.created_ts <= 900])
        store.update_launch(self.conn, st.mint, **upd)

    def due_external(self, now=None, ahead=None):
        """Mints that need a DexScreener price soon: horizon due and no live curve, or open after migration."""
        now = now or time.time()
        ahead = self.cfg.dex_every_sec if ahead is None else ahead
        out = []
        with self.lock:
            for m, st in self.launches.items():
                live = st.subscribed and not st.migrated
                if m in self.book.open and not live:
                    out.append(m)
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
                    st.max_mcap_sol = max(st.max_mcap_sol, v)
                    st.min_mcap_sol = min(st.min_mcap_sol or v, v)
            for m in list(self.book.open):
                st = self.launches.get(m)
                if st and (st.migrated or not st.subscribed):
                    check.append(st)
        for st in check:  # Jupiter quotes for the sell happen here, outside the lock (caller's thread)
            self.book.check(conn or self.conn, st, now, self.quote_fn)

    # ------------------------------------------------------------------ stage 2
    def skip_budget(self, st, now=None):
        """Not checked in depth (budget or queue age): the stage-1 verdict decides the label."""
        with self.lock:
            st.decision = "skip"
            store.update_launch(self.conn, st.mint, decision="skip",
                                skip_reason=getattr(st, "stage1_reason", None) or "budget")

    def finalize(self, st, enr, now=None, conn=None):
        """Stage 2: full analysis with the enrichment, safety gate, paper entry. Runs in the worker thread
        with its own DB connection `conn`."""
        now = now or time.time()
        conn = conn or self.conn
        with self.lock:
            st.stage = 2
            a = analyze(st, self.cfg, enr, self.tiers, now)
            st.insiders.update(a.insiders)
            reason = a.skip_reason(self.cfg)
            if reason is None and self.cfg.require_launch_data and st.source == "pumpportal" \
                    and not enr.get("early") and not enr.get("replay"):
                reason = "unverified"
        self.counters["stage2"] += 1
        safety_res = None
        if reason is None and self.cfg.safety_gate and self.enricher:
            safety_res = self.enricher.safety_gate(conn, st.mint)
            enr["safety"] = safety_res
            with self.lock:
                a = analyze(st, self.cfg, enr, self.tiers, now)
                reason = a.skip_reason(self.cfg)
                if reason is None and safety_res.get("blocked"):
                    reason = "safety"
        qf = None
        if reason is None and self.quote_fn:  # quote outside the lock: the stream keeps flowing meanwhile
            q = self.quote_fn(st.mint, self.cfg.size_sol - self.book._fees(), "buy")
            qf = (lambda *_: q) if q else None
        with self.lock:
            pos = None
            if reason is None:
                pos, why = self.book.enter(conn, st, a.score, now, qf, self.sol_usd_fn() or 0.0)
                if pos is None:
                    reason = "no_price" if why and "Preis" in why else "capacity"
                    a.positives.append(f"Nicht gekauft: {why}.")
                else:
                    self.counters["entries"] += 1
            st.decision = "enter" if pos else "skip"
            row = self._row(st, a, now, st.decision, reason, 2)
            row.update(calls_rpc=enr.get("calls_rpc"), calls_api=enr.get("calls_api"), prescore=st.prescore)
            store.save_launch(conn, row)
            store.save_wallets(conn, st.mint, a.wallets)
            if not pos and st.subscribed and reason in ("bundle", "insider", "hard", "dev_sold"):
                pass  # keep watching: outcomes of skipped launches show what the filter is worth
        log.info("Launch %s %s: %s%s  Score %.0f (Momentum %.0f, Risiko %.0f)%s", st.symbol, st.mint[:6],
                 "KAUF" if pos else "übersprungen", f" [{SKIP_LABELS.get(reason, reason)}]" if reason else "",
                 a.score, a.momentum, a.risk, ("  " + a.reasons[0]["text"]) if a.reasons else "")
        return a, reason, pos
