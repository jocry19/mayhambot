"""The market service: one analysis round every MARKET_EVERY_SEC (default 5 min).
Started by the launcher as service "market" (python -m market run, log logs/market.log).

Per round: refresh ~300 traded coins on DexScreener (10 calls), boosts/profiles every 10 min (3 calls),
CoinGecko every 15 min (SOL history + meme sector, 2 calls), then analysis from the DB. Zero Helius credits."""
import json
import logging
import os
import time

from core import http
from market import analysis, narratives, sources, store
from market.config import settings as default_cfg

log = logging.getLogger("market")


class Service:
    def __init__(self, cfg=default_cfg, db_path=None):
        self.cfg = cfg
        if not os.getenv("MAYHAM_SERVICE"):
            http.set_service("market")
        sources.apply_budgets(cfg)
        self.conn = store.connect(db_path)
        from market import report
        self.report_path = report.report_path(db_path)
        self.last = {"boosts": 0, "coingecko": 0, "sol_hist": 0, "prune": 0, "dynamics": 0}
        self.meme = None
        self.calls = {"dexscreener": 0, "coingecko": 0}
        self.rounds = 0
        self.errors = 0
        self.last_error = None

    # ------------------------------------------------------------ fetch
    def universe_mints(self, now):
        """Coins worth refreshing: what signals tracks (most volume first), fresh launches that migrated or are
        still alive, coins smart wallets traded in the last hour, and everything promoted on DexScreener."""
        c, cfg = self.conn, self.cfg
        mints = []
        if store.has(c, "tokens"):
            mints += [r[0] for r in c.execute(
                "SELECT mint FROM tokens WHERE last_updated>=? ORDER BY COALESCE(volume_h1,0) DESC LIMIT ?",
                (now - cfg.universe_hours * 3600, cfg.universe_max))]
        if store.has(c, "sniper_launches"):
            mints += [r[0] for r in c.execute(
                "SELECT mint FROM sniper_launches WHERE created_ts>=? AND (migrated=1 OR COALESCE(max_ret_60m,0)>=1) "
                "ORDER BY created_ts DESC LIMIT 60", (now - 6 * 3600,))]
        flows = analysis.smart_flows(c, now)
        mints += sorted(flows, key=lambda m: -abs(flows[m]["net"]))[:60]
        if store.has(c, "market_tokens"):
            mints += [r[0] for r in c.execute(
                "SELECT mint FROM market_tokens WHERE boosted>0 AND updated_at>=? LIMIT 60", (now - 6 * 3600,))]
        return list(dict.fromkeys(m for m in mints if m))[: cfg.universe_max + 120]

    def fetch(self, now):
        cfg, promo = self.cfg, {}
        if cfg.use_dexscreener and now - self.last["boosts"] >= cfg.boosts_every_sec:
            promo, n = sources.promoted()
            self.calls["dexscreener"] += n
            self.last["boosts"] = now
        if cfg.use_coingecko and now - self.last["coingecko"] >= cfg.coingecko_every_sec:
            self.last["coingecko"] = now
            if now - self.last["sol_hist"] >= 3600:
                pts = sources.sol_history(8 if not self.last["sol_hist"] else 2)
                self.calls["coingecko"] += 1
                if pts:
                    store.save_sol(self.conn, pts)
                    self.last["sol_hist"] = now
            m = sources.meme_sector()
            self.calls["coingecko"] += 1
            if m:
                self.meme = dict(m, ts=now)
        sol = sources.sol_now()
        if sol:
            store.save_sol(self.conn, [(now, sol)])
        rows = {}
        if cfg.use_dexscreener:
            mints = list(dict.fromkeys(self.universe_mints(now) + list(promo)))
            rows, n = sources.pairs_for(mints)
            self.calls["dexscreener"] += n
        for m, p in promo.items():
            if m in rows:
                rows[m].update(description=p["description"] or None, n_socials=max(rows[m]["n_socials"], p["n_socials"]),
                               boosted=max(rows[m]["boosted"], p["boosted"]), source=p["source"])
        out = []
        for m, r in rows.items():
            primary, hits = narratives.classify(r["name"], r["symbol"], r.get("description"))
            out.append({**{k: r.get(k) for k in ("mint", "symbol", "name", "description", "n_socials", "boosted", "dex",
                                                 "pair_created_at", "mcap_usd", "liquidity_usd", "price_change_h1",
                                                 "price_change_h24", "volume_h1", "volume_h24", "buys_h1", "sells_h1")},
                        "source": r.get("source") or "dexscreener", "sectors_json": json.dumps({"primary": primary, "hits": hits}),
                        "terms_json": json.dumps(sorted(narratives.terms_of(r["name"], r["symbol"]))),
                        "updated_at": now, "first_seen": now})
        store.save_tokens(self.conn, out)
        return len(out)

    # ------------------------------------------------------------ one round
    def round(self, now=None):
        now = int(now or time.time())
        t0 = time.time()
        n_tok = 0
        try:
            n_tok = self.fetch(now)
        except Exception as e:  # noqa: BLE001 - analysis still runs on what the DB has
            self.errors += 1
            self.last_error = str(e)[:200]
            log.exception("market fetch failed")
        meme = self.meme if self.meme and now - self.meme["ts"] <= 3 * self.cfg.coingecko_every_sec else None
        snap, secs, terms, bks = analysis.compute(self.conn, now, {"meme": meme}, live=True, cfg=self.cfg)
        store.save_snapshot(self.conn, snap, secs, terms, bks)
        self.rounds += 1
        if now - self.last["dynamics"] >= self.cfg.dynamics_every_sec:
            self.dynamics(now)
        if now - self.last["prune"] >= 86400:
            store.prune(self.conn, self.cfg.keep_days)
            self.last["prune"] = now
        took = round(time.time() - t0, 1)
        top = [f"{narratives.SECTOR_LABEL.get(s['sector'], s['sector'])} {s['heat']:.0f}" for s in secs[:3] if s["heat"] is not None]
        log.info("Markt %s %s (Abdeckung %.0f%%) · %d Coins · Sektoren: %s · Narrativ: %s · %.1fs",
                 analysis.LABELS.get(snap["label"], "–"), "–" if snap["score"] is None else f"{snap['score']:.0f}",
                 100 * (snap["coverage"] or 0), snap["universe_n"], ", ".join(top) or "–", snap["top_term"] or "–", took)
        self.heartbeat(now, took, n_tok)
        return snap

    def dynamics(self, now=None):
        """Hourly: regime transitions, sector lead ICs, per-component power and learned weights (market_state
        'dynamics' and 'learned_weights'). Heavier queries, so not every round; the dashboard only reads the result."""
        from market import dynamics
        now = int(now or time.time())
        self.last["dynamics"] = now
        try:
            t0 = time.time()
            power = dynamics.component_power(self.conn, now)
            d = {"ts": now, "transitions": dynamics.transitions(self.conn, now),
                 "sector_lead": dynamics.sector_lead(self.conn, now), "components": power}
            store.set_state(self.conn, "dynamics", d)
            if power.get("learned"):
                store.set_state(self.conn, "learned_weights", dict(power["learned"], ts=now))
            lead = [v["label"] for v in d["sector_lead"].values() if v.get("confirmed")]
            ok = [k for k, v in power["rows"].items() if v.get("confirmed")]
            log.info("Markt-Dynamik: %d Stunden mit Launch-Ergebnis, bestätigte Bausteine: %s, Sektor-Vorlauf: %s (%.1fs)",
                     power["n_hours"], ", ".join(ok) or "noch keine", ", ".join(lead) or "noch keiner", time.time() - t0)
            self.report(now)
            return d
        except Exception:  # noqa: BLE001 - the round itself already succeeded
            log.exception("market dynamics failed")
            return None

    def report(self, now=None):
        """reports/market_report.json plus one log line: the evaluation with walk-forward, hourly with the dynamics."""
        from market import report
        try:
            rep = report.write(self.conn, self.report_path, now, self.cfg)
            log.info(report.line(rep))
            return rep
        except Exception:  # noqa: BLE001 - a report never breaks the service
            log.exception("market report failed")
            return None

    def heartbeat(self, now, took=None, n_tok=None):
        """Service status for web/ and cli/ (market_state 'service_status')."""
        store.set_state(self.conn, "service_status", {
            "ts": now, "pid": os.getpid(), "rounds": self.rounds, "round_s": took, "tokens_refreshed": n_tok,
            "calls": dict(self.calls), "errors": self.errors, "last_error": self.last_error,
            "every_sec": self.cfg.every_sec, "meme_ok": bool(self.meme), "sol_hist_ok": bool(self.last["sol_hist"])})

    def backfill(self, hours=24, now=None):
        """Past hours from the DB alone (launches, smart money, breadth from token_prices, SOL history), so the
        dashboard and the evaluation have a history right after the first start. Never overwrites live rows."""
        now = int(now or time.time())
        have = {r[0] for r in self.conn.execute("SELECT ts FROM market_snapshots WHERE ts>=?", (now - hours * 3600 - 3600,))}
        n = 0
        for h in range(hours, 0, -1):
            ts = now - now % 3600 - h * 3600
            if any(abs(ts - t) < 1800 for t in have):
                continue
            snap, secs, terms, _ = analysis.compute(self.conn, ts, {}, live=False, cfg=self.cfg)
            if snap["score"] is None and not terms:
                continue
            store.save_snapshot(self.conn, snap, secs, terms)
            n += 1
        if n:
            log.info("Markt-Historie: %d Stunden aus der Datenbank nachgerechnet", n)
        return n

    def run(self, rounds=None):
        log.info("market service: alle %d s, DexScreener %d/min, CoinGecko %d/min, keine Helius-Credits",
                 self.cfg.every_sec, self.cfg.dexscreener_per_min, self.cfg.coingecko_per_min)
        try:  # SOL history first, so the backfill has the SOL trend too
            if self.cfg.use_coingecko:
                pts = sources.sol_history(8)
                self.calls["coingecko"] += 1
                if pts:
                    store.save_sol(self.conn, pts)
                    self.last["sol_hist"] = int(time.time())
            self.backfill()
        except Exception:  # noqa: BLE001
            log.exception("market backfill failed")
        done = 0
        while rounds is None or done < rounds:
            t0 = time.time()
            try:
                self.round()
            except Exception as e:  # noqa: BLE001 - next round tries again
                self.errors += 1
                self.last_error = str(e)[:200]
                log.exception("market round failed")
                self.heartbeat(int(time.time()))
            done += 1
            if rounds is not None and done >= rounds:
                break
            time.sleep(max(5.0, self.cfg.every_sec - (time.time() - t0)))
