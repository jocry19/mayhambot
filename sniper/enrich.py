"""Budgeted network look-ups for launches that passed stage 1. Order of sources, cheapest first:

1. our own tables (no network): dev history, known bundlers, funders from research_funding/safety_wallets,
   research clusters, wallet tiers
2. pump.fun public API (keyless, not Helius): coin metadata + socials, the first trades with slot and
   time, the dev's other coins
3. Solana RPC (Helius free plan = bottleneck of the whole system): only when 2 failed, at most
   SNIPER_MAX_RPC_PER_LAUNCH calls per launch and SNIPER_RPC_PER_MIN per minute for the whole service.

All HTTP goes through core.http (shared per-host limits, 429 back-off, key redaction).
"""
import logging
import threading
import time
from collections import deque

from core import http
from safety import checks as safety_checks
from safety.config import settings as safety_settings
from safety.sources import BudgetExceeded, Fetcher
from sniper import store
from sniper.state import PUMP_DECIMALS

log = logging.getLogger("sniper.enrich")
PUMP_HEADERS = {"Origin": "https://pump.fun", "Referer": "https://pump.fun/", "Accept": "application/json"}


class Budget:
    """Sliding one-minute windows per kind ('rpc', 'api', 'deep'), shared by all threads of the service."""

    def __init__(self, per_min):
        self.per_min = dict(per_min)
        self.calls = {k: deque() for k in self.per_min}
        self.total = {k: 0 for k in self.per_min}
        self.lock = threading.Lock()

    def _trim(self, k, now):
        q = self.calls[k]
        while q and now - q[0] >= 60:
            q.popleft()

    def left(self, kind, now=None):
        now = now or time.time()
        with self.lock:
            self._trim(kind, now)
            return self.per_min[kind] - len(self.calls[kind])

    def take(self, kind, n=1, wait=0.0):
        end = time.time() + wait
        while True:
            now = time.time()
            with self.lock:
                self._trim(kind, now)
                if len(self.calls[kind]) + n <= self.per_min[kind]:
                    self.calls[kind].extend([now] * n)
                    self.total[kind] += n
                    return True
                nxt = self.calls[kind][0] + 60 - now if self.calls[kind] else 0.5
            if now + nxt > end:
                return False
            time.sleep(min(nxt, 1.0))


class LaunchFetcher(Fetcher):
    """safety's Fetcher with the sniper's caps: RPC calls count against the launch and the minute budget."""

    def __init__(self, cfg, budget, rpc_cap, rpc_url):
        super().__init__(safety_settings, max_calls=12, rpc_url=rpc_url)
        self.budget, self.rpc_cap, self.sniper_rpc = budget, rpc_cap, 0

    def rpc(self, method, params):
        if self.sniper_rpc >= self.rpc_cap:
            raise BudgetExceeded(f"sniper: {self.rpc_cap} RPC calls per launch used up")
        if not self.budget.take("rpc", 1, wait=3.0):
            raise BudgetExceeded("sniper: RPC minute budget used up")
        self.sniper_rpc += 1
        j = http.post_json(self.rpc_url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, retries=1)
        if "error" in j:
            raise RuntimeError(f"{method}: {j['error']}")
        return j.get("result")


def _ts(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v / 1000 if v > 1e12 else v


def _list(d):
    if isinstance(d, list):
        return d
    if isinstance(d, dict):
        for k in ("coins", "trades", "data", "items"):
            if isinstance(d.get(k), list):
                return d[k]
    return []


def parse_pump_trades(rows, mint):
    """pump.fun /trades/all rows -> oldest-first list of {sig, wallet, side, tokens, sol, slot, ts, idx}."""
    out = []
    for r in rows or []:
        if r.get("mint") not in (None, mint):
            continue
        tok = float(r.get("token_amount") or r.get("tokenAmount") or 0)
        sol = float(r.get("sol_amount") or r.get("solAmount") or 0)
        out.append({"sig": r.get("signature") or r.get("tx"), "wallet": r.get("user") or r.get("traderPublicKey"),
                    "side": "buy" if r.get("is_buy", r.get("isBuy")) else "sell",
                    "tokens": tok / 10 ** PUMP_DECIMALS if tok > 1e12 or isinstance(r.get("token_amount"), int) else tok,
                    "sol": sol / 1e9 if sol > 1e5 or isinstance(r.get("sol_amount"), int) else sol,
                    "slot": r.get("slot"), "ts": _ts(r.get("timestamp")), "idx": r.get("tx_index") or 0})
    out.sort(key=lambda e: (e["slot"] if e["slot"] is not None else 1 << 62, e["idx"], e["ts"] or 0))
    return out


def _keys(tx):
    return [k["pubkey"] if isinstance(k, dict) else k for k in tx["transaction"]["message"]["accountKeys"]]


def parse_rpc_tx(tx, mint):
    """Signer, token and SOL change of one transaction (getTransaction jsonParsed)."""
    if not tx or not tx.get("meta"):
        return None
    signer = _keys(tx)[0]
    got = safety_checks.token_deltas(tx, mint).get(signer, 0.0)
    sol = -safety_checks.sol_deltas(tx).get(signer, 0.0)
    return {"wallet": signer, "side": "buy" if got > 0 else "sell", "tokens": abs(got), "sol": abs(sol),
            "slot": tx.get("slot"), "ts": tx.get("blockTime")}


class Enricher:
    def __init__(self, cfg, budget=None):
        self.cfg = cfg
        self.budget = budget or Budget({"rpc": cfg.rpc_per_min, "api": cfg.pump_api_per_min, "deep": cfg.deep_per_min})
        self.exchanges = safety_checks.load_exchanges(safety_settings.exchanges_file)
        self.pump_ok = cfg.use_pump_api
        self.pump_fail = 0

    # ---- pump.fun public API (keyless)
    def _pump(self, path, params=None):
        if not self.pump_ok or not self.budget.take("api", 1, wait=2.0):
            return None
        try:
            d = http.get_json(f"{self.cfg.pump_api_url}{path}", params=params, headers=PUMP_HEADERS, retries=1, timeout=10)
            self.pump_fail = 0
            return d
        except Exception as e:  # noqa: BLE001 - optional source; RPC is the fallback
            self.pump_fail += 1
            log.debug("pump.fun API %s failed: %s", path, e)
            if self.pump_fail >= 10:  # endpoint moved or blocked: stop asking for 10 minutes
                log.warning("pump.fun API antwortet nicht (%s), Pause 10 min", e)
                self.pump_ok = False
                threading.Timer(600, lambda: setattr(self, "pump_ok", self.cfg.use_pump_api)).start()
                self.pump_fail = 0
            return None

    def run(self, conn, st, now=None):
        now = now or time.time()
        enr = {"exchanges": self.exchanges, "errors": [], "calls_api": 0, "calls_rpc": 0}
        f = LaunchFetcher(self.cfg, self.budget, self.cfg.max_rpc_per_launch, self.cfg.rpc_url)
        api0 = self.budget.total["api"]

        # 2. pump.fun: metadata/socials, early trades with slots, dev's other coins
        if st.source == "pumpportal":
            coin = self._pump(f"/coins/{st.mint}")
            if isinstance(coin, dict) and coin.get("mint", st.mint) == st.mint:
                enr["coin"] = {k: coin.get(k) for k in ("twitter", "telegram", "website", "creator", "complete",
                                                         "usd_market_cap", "created_timestamp")}
            rows = []
            for page in range(2):
                d = _list(self._pump(f"/trades/all/{st.mint}", {"limit": 200, "offset": page * 200, "minimumSize": 0}))
                rows += d
                if len(d) < 200:
                    break
            early = parse_pump_trades(rows, st.mint)
            if early and all(e.get("slot") is not None for e in early[:5]):
                enr.update(early=early, early_src="pump_api")
            if st.creator:
                coins = _list(self._pump(f"/coins/user-created-coins/{st.creator}",
                                         {"offset": 0, "limit": 50, "includeNsfw": "true"}))
                if coins:
                    created = [_ts(c.get("created_timestamp")) or 0 for c in coins]
                    enr["dev_coins"] = {"n": len(coins), "n24": sum(1 for t in created if now - t <= 86400),
                                        "n_complete": sum(1 for c in coins if c.get("complete") and c.get("mint") != st.mint)}

        # 3. RPC fallback for the launch slot (1 call) + up to N unseen bundle txs
        if "early" not in enr:
            try:
                self._rpc_early(f, st, enr)
            except BudgetExceeded as e:
                enr["errors"].append(str(e))
            except Exception as e:  # noqa: BLE001
                enr["errors"].append(f"rpc early: {e}"[:160])

        # 1. own tables
        wallets = self._first_wallets(st, enr)
        enr["creator_hist"] = store.creator_history(conn, st.creator, now) if st.creator else {}
        enr["known_bad"] = store.known_bad(conn, wallets)
        funders = store.cached_funders(conn, wallets)
        enr["clusters"] = store.wallet_clusters(conn, wallets)

        # funders by network only for bundle wallets, only within the launch budget (2 RPC calls each)
        missing = [w for w in wallets[: self.cfg.funders_profiled] if w not in funders]
        if missing and f.sniper_rpc + 2 <= f.rpc_cap:
            ctx = safety_checks.Context(conn, st.mint, f, safety_settings, deep=True)
            for w in missing:
                if f.sniper_rpc + 2 > f.rpc_cap:
                    break
                try:
                    p = ctx.profile(w)
                    if p and p.get("funder"):
                        funders[w] = p["funder"]
                except BudgetExceeded:
                    break
                except Exception as e:  # noqa: BLE001
                    enr["errors"].append(f"funder {w[:6]}: {e}"[:160])
        enr["funders"] = funders

        # model probability, when model/ is trained and knows something about the coin
        try:
            from model.predict import ModelNotTrained, score_token
            try:
                r = score_token(st.mint, conn=conn)
                if r:
                    hz = r.get("horizons") or {}
                    p = (hz.get("1h") or next(iter(hz.values()), {})).get("p_pump")
                    if p is not None:
                        enr["model_p"] = float(p)
            except ModelNotTrained:
                pass
        except Exception as e:  # noqa: BLE001 - optional
            log.debug("model score %s: %s", st.mint, e)

        enr["calls_rpc"] = f.sniper_rpc
        enr["calls_api"] = self.budget.total["api"] - api0
        enr["fetcher"] = f
        return enr

    def _first_wallets(self, st, enr):
        """Bundle wallets first, then the biggest early buyers (creator excluded)."""
        early = enr.get("early") or []
        cs = enr.get("create_slot")
        if cs is None and early:
            cs = early[0].get("slot")
        out = []
        for e in early:
            if e["side"] == "buy" and e["wallet"] != st.creator and cs is not None and e.get("slot") is not None \
                    and e["slot"] < cs + max(1, self.cfg.bundle_slots) and e["wallet"] not in out:
                out.append(e["wallet"])
        by_ts = sorted(st.first_buy_ts.items(), key=lambda kv: kv[1])
        for w, _ in by_ts:
            if w != st.creator and w not in out:
                out.append(w)
        for e in early:
            if e["side"] == "buy" and e["wallet"] != st.creator and e["wallet"] not in out:
                out.append(e["wallet"])
        return out[:40]

    def _rpc_early(self, f, st, enr):
        sigs = f.rpc("getSignaturesForAddress", [st.mint, {"limit": 1000}]) or []
        sigs = [s for s in sigs if not s.get("err")]
        if not sigs:
            return
        slot_of = {s["signature"]: s for s in sigs}
        cs = (slot_of.get(st.create_sig) or {}).get("slot") or min(s["slot"] for s in sigs)
        enr["create_slot"] = cs
        seen = {t.sig: t for t in st.trades}
        early = []
        unseen = []
        for s in sorted(sigs, key=lambda s: s["slot"]):
            if s["slot"] >= cs + max(1, self.cfg.bundle_slots) + 2:
                break
            t = seen.get(s["signature"])
            if t:
                early.append({"sig": t.sig, "wallet": t.wallet, "side": t.side, "tokens": t.tokens, "sol": t.sol,
                              "slot": s["slot"], "ts": s.get("blockTime")})
            elif s["signature"] != st.create_sig:
                unseen.append(s)
        for s in unseen[: self.cfg.bundle_txs_fetched]:
            tx = f.rpc("getTransaction", [s["signature"], {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}])
            e = parse_rpc_tx(tx, st.mint)
            if e:
                e["sig"] = s["signature"]
                early.append(e)
        # unseen txs we could not afford to open still count: spread the curve drop over them
        rest = unseen[self.cfg.bundle_txs_fetched:]
        if rest:
            got = sum(e["tokens"] for e in early if e["side"] == "buy" and e["wallet"] != st.creator
                      and e["sig"] not in seen)
            left = max(0.0, st.unseen_tokens() - got)
            for i, s in enumerate(rest):
                early.append({"sig": s["signature"], "wallet": f"unbekannt-{i}-{s['signature'][:6]}", "side": "buy",
                              "tokens": left / len(rest), "sol": 0.0, "slot": s["slot"], "ts": s.get("blockTime")})
        early.sort(key=lambda e: (e["slot"] or 0))
        if early:
            enr.update(early=early, early_src="rpc")

    # ---- last gate before a paper buy: the safety layer as a buy wish (its own shared RPC budget,
    # public RPC first, Helius capped by safety/), like the copy trader before a buy
    def safety_gate(self, conn, mint):
        import safety
        try:
            safety.assess(mint, conn, deep=self.cfg.safety_deep, force=True, priority=True)
        except Exception as e:  # noqa: BLE001 - a failed check must not open a position
            log.warning("safety.assess %s failed: %s", mint, e)
            return {"factor": 1.0, "blocked": False, "risk": None, "level": "unchecked", "reasons": [], "error": str(e)}
        return safety.signal_penalty(conn, mint)
