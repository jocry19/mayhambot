"""The intel service ("Zentrale"): every INTEL_EVERY_SEC (60 s) one round, from the shared DB only.
Zero network calls, zero Helius credits. Started by the launcher as service "intel" (python -m intel run).

Per round: copy new rug-check results into the holder history, detect ticker events; every 5 min coin and market
features (history row every 15 min); every hour wallet features; every 10 min outcomes of past snapshots."""
import json
import logging
import os
import time

from core import http
from intel import base, coin, evaluate, events, marketctx, sources, store, wallet
from intel.config import settings as default_cfg

log = logging.getLogger("intel")


def universe(conn, now, limit=150):
    """Coins worth watching: most traded in the last 2 h, open copy-trader positions, recent signals, sniper entries
    and migrations, coins the signal engine tracks."""
    out = [r[0] for r in conn.execute("SELECT mint, COUNT(*) n FROM trades WHERE ts>? AND ts<=? GROUP BY mint ORDER BY n DESC LIMIT ?",
                                      (now - 7200, now, limit))]
    if store.has(conn, "ct_positions"):
        out = [r[0] for r in conn.execute("SELECT DISTINCT mint FROM ct_positions WHERE status='open'")] + out
    out += [r[0] for r in conn.execute("SELECT DISTINCT mint FROM signals WHERE ts>?", (now - 6 * 3600,))]
    if store.has(conn, "sniper_launches"):
        out += [r[0] for r in conn.execute("SELECT mint FROM sniper_launches WHERE created_ts>? AND (decision='enter' OR migrated=1) "
                                           "ORDER BY created_ts DESC LIMIT 40", (now - 6 * 3600,))]
    out += [r[0] for r in conn.execute("SELECT mint FROM tokens WHERE tracked=1 AND last_updated>? ORDER BY volume_h1 DESC LIMIT 60",
                                       (now - 3600,))]
    return list(dict.fromkeys(m for m in out if m))[:limit]


def holder_rows(conn, since):
    """New rug-check results (safety_tokens.ts > since) flattened into the holder history."""
    if not store.has(conn, "safety_tokens"):
        return []
    out = []
    now = int(time.time())
    for r in conn.execute("SELECT mint, ts, score, blocked, checks_json FROM safety_tokens WHERE ts>? ORDER BY ts LIMIT 2000", (since,)):
        try:
            ch = json.loads(r["checks_json"] or "{}")
        except ValueError:
            ch = {}

        def d(name, key, ch=ch):
            c = ch.get(name) or {}
            if c.get("status") in ("unknown", "skipped", "error"):
                return None
            v = (c.get("data") or {}).get(key)
            return float(v) if isinstance(v, (int, float)) else None
        out.append({"mint": r["mint"], "safety_ts": r["ts"], "seen_ts": now, "top10_pct": d("holders", "top10_pct"),
                    "top1_pct": d("holders", "top1_pct"), "insider_pct": d("insiders", "insider_pct"),
                    "dev_pct": d("dev", "dev_pct"), "bundle_held_pct": d("bundles", "still_held_pct"),
                    "launch_pct": d("bundles", "launch_pct"), "roundtrip_share": d("wash", "roundtrip_share"),
                    "safety_score": r["score"], "blocked": r["blocked"]})
    return out


class Service:
    def __init__(self, cfg=default_cfg, db_path=None, net=None):
        self.cfg = cfg
        self.conn = store.connect(db_path)
        sources.ensure(self.conn)
        if not os.getenv("MAYHAM_SERVICE"):
            http.set_service("intel")
        http.set_budget(cfg.jupiter_url.split("/")[2], cfg.jupiter_per_min)
        http.set_budget("api.coingecko.com", cfg.coingecko_per_min)
        http.set_budget("api.alternative.me", 2)
        self.net = ("PYTEST_CURRENT_TEST" not in os.environ) if net is None else net  # tests never call out
        self.last = {"coins": 0, "persist": 0, "wallets": 0, "label": 0, "prune": 0, "quotes": 0, "macro": 0}
        st = store.get_state(self.conn, "service_status") or {}
        self.holders_since = int(st.get("holders_since") or 0)
        self.rounds = 0
        self.errors = 0
        self.last_error = None
        self.counts = {}

    def step(self, name, fn):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 - one failing part never stops the others
            self.errors += 1
            self.last_error = f"{name}: {e}"[:200]
            log.exception("intel %s failed", name)
            return None

    def coins(self, now, persist):
        mints = universe(self.conn, now, self.cfg.universe_max)
        rows = []
        for m in mints:
            r = coin.coin_features(self.conn, m, now, self.cfg)
            rows.append({"key": m, "ts": now, "coverage": r["coverage"]["all"], "features": r["features"],
                         "meta": dict(r["meta"], coverage=r["coverage"])})
        store.save_features(self.conn, "coin", rows, persist)
        mk = marketctx.market_features(self.conn, now)
        store.save_features(self.conn, "market", [{"key": "solana", "ts": now, "coverage": mk["coverage"]["all"],
                                                   "features": mk["features"], "meta": mk["meta"]}], persist)
        return len(rows)

    def wallets(self, now):
        rows = []
        for w in wallet.universe(self.conn, now, self.cfg.wallets_max):
            r = wallet.wallet_features(self.conn, w, now)
            rows.append({"key": w, "ts": now, "coverage": r["coverage"]["all"], "features": r["features"], "meta": r["meta"]})
        store.save_features(self.conn, "wallet", rows, persist=True)
        return len(rows)

    def round(self, now=None):
        now = int(now or time.time())
        t0 = time.time()
        base.clear_cache()
        c = self.counts
        hs = self.step("holders", lambda: holder_rows(self.conn, self.holders_since)) or []
        if hs:
            store.save_holders(self.conn, hs)
            self.holders_since = max(h["safety_ts"] for h in hs)
        c["holders"] = len(hs)
        c["attention"] = self.step("attention", lambda: sources.save(self.conn, "intel_attention", sources.attention_rows(self.conn, now)))
        if self.net and self.cfg.macro_every_sec and now - self.last["macro"] >= self.cfg.macro_every_sec:
            self.step("macro", lambda: sources.macro_round(self.conn, now))
            self.last["macro"] = now
        c["events"] = self.step("events", lambda: store.add_events(self.conn, events.detect(self.conn, now, self.cfg))) or 0
        if now - self.last["coins"] >= self.cfg.coins_every_sec:
            persist = now - self.last["persist"] >= self.cfg.persist_every_sec
            c["coins"] = self.step("coins", lambda: self.coins(now, persist))
            self.last["coins"] = now
            if persist:
                self.last["persist"] = now
        if self.net and self.cfg.quotes_every_sec and now - self.last["quotes"] >= self.cfg.quotes_every_sec:
            c["quotes"] = self.step("quotes", lambda: sources.quote_round(self.conn, now, self.cfg))
            self.last["quotes"] = now
        if now - self.last["wallets"] >= self.cfg.wallets_every_sec:
            c["wallets"] = self.step("wallets", lambda: self.wallets(now))
            self.last["wallets"] = now
        if now - self.last["label"] >= self.cfg.label_every_sec:
            c["labeled"] = self.step("label", lambda: evaluate.label(self.conn, now))
            self.step("evaluate", lambda: store.set_state(self.conn, "evaluation", evaluate.evaluate(self.conn, now=now)))
            self.last["label"] = now
        if now - self.last["prune"] >= 86400:
            self.step("prune", lambda: store.prune(self.conn, self.cfg.keep_days, now))
            self.last["prune"] = now
        self.rounds += 1
        took = round(time.time() - t0, 2)
        if c.get("coins") is not None and now == self.last["coins"]:
            log.info("Zentrale: %s Coins, %s Wallets, %d neue Ereignisse, %d Holder-Stände, %s Ergebnisse · %.1fs",
                     c.get("coins"), c.get("wallets", "–"), c["events"], c["holders"], c.get("labeled", "–"), took)
        self.heartbeat(now, took)
        return dict(c)

    def heartbeat(self, now, took=None):
        store.set_state(self.conn, "service_status", {
            "ts": now, "pid": os.getpid(), "rounds": self.rounds, "round_s": took, "counts": self.counts,
            "errors": self.errors, "last_error": self.last_error, "every_sec": self.cfg.every_sec,
            "holders_since": self.holders_since, "last": self.last})

    def run(self, rounds=None):
        log.info("intel service (Zentrale): alle %d s, Coins alle %d s, Wallets alle %d s, keine Netz-Abfragen",
                 self.cfg.every_sec, self.cfg.coins_every_sec, self.cfg.wallets_every_sec)
        done = 0
        while rounds is None or done < rounds:
            t0 = time.time()
            try:
                self.round()
            except Exception as e:  # noqa: BLE001
                self.errors += 1
                self.last_error = str(e)[:200]
                log.exception("intel round failed")
            done += 1
            if rounds is not None and done >= rounds:
                break
            time.sleep(max(5.0, self.cfg.every_sec - (time.time() - t0)))
