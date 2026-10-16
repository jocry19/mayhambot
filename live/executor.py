"""GuardedLiveExecutor: the copy trader's live executor (copytrade/execution.make_executors).

Buy:  guard (guard.py) -> Jupiter quote with our slippage -> impact and price sanity -> swap tx with capped
      priority fee -> sign locally -> send with preflight (the RPC simulates first, a failing swap costs nothing)
      -> rebroadcast until confirmed or the blockhash expired -> read the landed amounts from the transaction.
Sell: whole on-chain balance of the coin, up to LIVE_SELL_ATTEMPTS with a fresh quote and doubled slippage each
      time, then the empty token account is closed (gets the ~0.002 SOL rent back, which at 0.1 SOL is 2 %).
Dry run (mode 'dry', default while LIVE_DRY_RUN is not false): the same up to the signature, simulated by the
      RPC for buys, never sent; the position is booked as mode 'dry' with a quote-based fill like paper.

Every attempt is a row in live_orders; an order whose fate is unknown blocks new buys until it is resolved.
"""
from __future__ import annotations

import base64
import logging
import threading
import time

from copytrade import chain, market  # type: ignore
from copytrade.config import SOL_MINT, Settings  # type: ignore
from copytrade.execution import Fill, PaperExecutor, paper_adverse  # type: ignore

from . import guard, jupiter, store
from .config import LiveConfig, load
from .wallet import KeyError_, load_keypair, pubkey_of

log = logging.getLogger("live.exec")

ATA_RENT_LAMPORTS = 2_039_280
UNCONFIRMED = "unbestätigt:"  # Fill.error prefix: the swap may still land, keep the position row
MAX_CHAIN_FAILS_24H = 6  # sells of one coin that failed ON CHAIN (fees paid) in 24 h before a manual sell is needed
STUCK_AFTER = 6  # failed sell rounds before the Telegram alert
TOKEN_PROGRAMS = {"TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"}


class GuardedLiveExecutor(PaperExecutor):
    def __init__(self, settings: Settings, rpc, cfg: LiveConfig, keypair, pubkey: str, dry: bool, conn=None,
                 jup=jupiter, sleep=time.sleep, exit_only: str = ""):
        PaperExecutor.__init__(self, settings, rpc)
        self.exit_only = exit_only
        self.assess = None
        self.dry_seller: "GuardedLiveExecutor | None" = None  # safety.assess; tests inject their own  # reason: only sells of open real positions, no buys
        self.cfg = cfg
        self.keypair = keypair
        self.pubkey = pubkey
        self.dry = dry or keypair is None
        self.mode = "dry" if self.dry else "live"
        self.jup = jup
        self.sleep = sleep
        if conn is None:
            from copytrade import store as ct_store  # type: ignore

            conn = ct_store.connect(settings)
        self.conn = conn
        store.init(conn)
        store.put(conn, "wallet_pubkey", pubkey)
        store.put(conn, "wallet_has_key", "1" if keypair is not None else "0")
        self._buy_lock = threading.Lock()
        self._sell_backoff: dict[str, tuple[int, float]] = {}  # mint -> (failed rounds, not before)

    # ------------------------------------------------------------------ balance
    def balance(self, conn=None) -> float:
        if self.dry:
            v = store.get(self.conn, "dry_balance_sol")
            if v == "":
                store.put(self.conn, "dry_balance_sol", self.cfg.dry_start_sol)
                return self.cfg.dry_start_sol
            return float(v)
        r = self.rpc.call("getBalance", [self.pubkey, {"commitment": "confirmed"}])
        return (r or {}).get("value", 0) / 1e9

    def adjust_balance(self, conn, delta: float) -> None:
        if self.dry:
            store.put(self.conn, "dry_balance_sol", self.balance() + delta)

    # ------------------------------------------------------------------ chain helpers
    def _token_accounts(self, mint: str, rpc=None) -> list[dict]:
        r = (rpc or self.rpc).call("getTokenAccountsByOwner", [self.pubkey, {"mint": mint},
                                                      {"encoding": "jsonParsed", "commitment": "confirmed"}])
        out = []
        for a in (r or {}).get("value") or []:
            acc = a.get("account") or {}
            info = ((acc.get("data") or {}).get("parsed") or {}).get("info") or {}
            out.append({"address": a.get("pubkey"), "program": acc.get("owner"), "lamports": acc.get("lamports", 0),
                        "amount": int((info.get("tokenAmount") or {}).get("amount") or 0),
                        "decimals": (info.get("tokenAmount") or {}).get("decimals")})
        return out

    def _deltas(self, sig: str, mint: str) -> dict | None:
        """Landed amounts of our own swap: token raw delta, SOL lamport delta incl. fee and wSOL, fee."""
        for i in range(5):
            try:
                tx = self.rpc.call("getTransaction", [sig, {"encoding": "jsonParsed",
                                                            "maxSupportedTransactionVersion": 0,
                                                            "commitment": "confirmed"}])
            except Exception as e:  # 429/timeout: the swap DID land; the caller falls back to the wallet balance
                log.debug("getTransaction %s: %s", sig[:8], e)
                tx = None
            if tx:
                return parse_deltas(tx, self.pubkey, mint)
            self.sleep(1.5 + i)
        return None

    def _fee_of(self, sig: str) -> int:
        """Fee of a landed transaction (also of one that failed on chain), 0 if unreadable."""
        try:
            tx = self.rpc.call("getTransaction", [sig, {"encoding": "json", "maxSupportedTransactionVersion": 0,
                                                        "commitment": "confirmed"}])
            return int(((tx or {}).get("meta") or {}).get("fee") or 0)
        except Exception:
            return 0

    def _send(self, b64: str, first: bool) -> str:
        opts = {"encoding": "base64", "maxRetries": 0}
        opts.update({"skipPreflight": False, "preflightCommitment": "processed"} if first else {"skipPreflight": True})
        return self.rpc.call("sendTransaction", [b64, opts])

    def _rebroadcast(self, b64: str) -> None:
        for r in (self.rpc, getattr(self.rpc, "public", None)):
            if r is None:
                continue
            try:
                r.call("sendTransaction", [b64, {"encoding": "base64", "skipPreflight": True, "maxRetries": 0}])
            except Exception:
                pass

    def _confirm(self, sig: str, b64: str, last_valid: int | None) -> str:
        """'landed' | 'failed:<err>' | 'expired' | 'unknown'"""
        t0, n = time.time(), 0
        while time.time() - t0 < self.cfg.confirm_timeout_s:
            n += 1
            self.sleep(1.5)
            try:
                st = self.rpc.call("getSignatureStatuses", [[sig], {"searchTransactionHistory": False}])
                st = ((st or {}).get("value") or [None])[0]
            except Exception:
                st = None
            if st and st.get("confirmationStatus") in ("confirmed", "finalized"):
                return f"failed:{st['err']}" if st.get("err") else "landed"
            if n % 2 == 0:
                self._rebroadcast(b64)
            if last_valid and n % 4 == 0:
                try:
                    if int(self.rpc.call("getBlockHeight", [{"commitment": "confirmed"}])) > last_valid:
                        st = self.rpc.call("getSignatureStatuses", [[sig], {"searchTransactionHistory": True}])
                        st = ((st or {}).get("value") or [None])[0]
                        if st and st.get("confirmationStatus") in ("confirmed", "finalized"):
                            return f"failed:{st['err']}" if st.get("err") else "landed"
                        return "expired"
                except Exception:
                    pass
        return "unknown"

    # ------------------------------------------------------------------ one swap
    def _swap(self, side: str, mint: str, amount_raw: int, slippage_bps: int, attempt: int,
              snap=None, decimals: int | None = None) -> tuple[int, dict]:
        """Runs one swap attempt. Returns (order id, result dict with status and amounts)."""
        from solders.transaction import VersionedTransaction  # type: ignore

        c, cfg = self.conn, self.cfg
        inp, out = (SOL_MINT, mint) if side == "buy" else (mint, SOL_MINT)
        oid = store.new_order(c, dry=self.dry, side=side, mint=mint, in_raw=amount_raw, attempt=attempt)
        q = self.jup.quote(self.s, inp, out, amount_raw, slippage_bps)
        if not q or int(q.get("outAmount") or 0) <= 0:
            store.update(c, oid, status="rejected", error="kein Jupiter-Quote")
            return oid, {"status": "rejected", "error": "kein Jupiter-Quote"}
        impact = float(q.get("priceImpactPct") or 0)
        store.update(c, oid, quote_out_raw=int(q["outAmount"]), min_out_raw=int(q.get("otherAmountThreshold") or 0),
                     price_impact=impact, slippage_bps=slippage_bps,
                     data={"route": [r.get("swapInfo", {}).get("label") for r in q.get("routePlan") or []]})
        if side == "buy":
            if impact > cfg.max_price_impact:
                err = f"Preiseinfluss {impact:.1%} > {cfg.max_price_impact:.0%}"
                store.update(c, oid, status="rejected", error=err)
                return oid, {"status": "rejected", "error": err}
            if snap is not None:
                if snap.ts and time.time() - snap.ts > cfg.max_snapshot_age_s:
                    err = f"Marktdaten {time.time() - snap.ts:.0f}s alt"
                    store.update(c, oid, status="rejected", error=err)
                    return oid, {"status": "rejected", "error": err}
                if snap.price_sol and decimals is not None:
                    qp = (amount_raw / 1e9) / (int(q["outAmount"]) / 10 ** decimals)
                    dev = abs(qp / snap.price_sol - 1)
                    if dev > cfg.max_quote_deviation:
                        err = f"Jupiter-Preis weicht {dev:.0%} vom Marktpreis ab (Daten veraltet?)"
                        store.update(c, oid, status="rejected", error=err)
                        return oid, {"status": "rejected", "error": err}
        try:
            sw = self.jup.swap_tx(self.s, q, self.pubkey, cfg.priority_fee_max_lamports, cfg.priority_level)
            raw = base64.b64decode(sw["swapTransaction"])
            tx = VersionedTransaction.from_bytes(raw)
        except Exception as e:
            store.update(c, oid, status="rejected", error=f"Swap-Aufbau: {e}"[:300])
            return oid, {"status": "rejected", "error": f"Swap-Aufbau fehlgeschlagen: {e}"}
        last_valid = int(sw.get("lastValidBlockHeight") or 0) or None
        prio = int(sw.get("prioritizationFeeLamports") or 0)
        store.update(c, oid, priority_lamports=prio, last_valid_height=last_valid)
        if self.keypair is not None:
            tx = VersionedTransaction(tx.message, [self.keypair])
        b64 = base64.b64encode(bytes(tx)).decode()
        res = {"quote": q, "impact": impact, "priority_lamports": prio}

        if self.dry:
            if side == "sell":  # the dry wallet holds no tokens: building the transaction is the test
                store.update(c, oid, status="dry_built")
                return oid, {**res, "status": "dry_built"}
            try:
                sim = self.rpc.call("simulateTransaction", [b64, {"encoding": "base64", "sigVerify": False,
                                                                   "replaceRecentBlockhash": True,
                                                                   "commitment": "processed"}])
                v = (sim or {}).get("value") or {}
                err = v.get("err")
                store.update(c, oid, status="dry_ok" if not err else "dry_sim_failed",
                             sim_err=str(err) if err else None, sim_units=v.get("unitsConsumed"),
                             data={"logs": (v.get("logs") or [])[-4:]} if err else None)
                return oid, {**res, "status": "dry_ok" if not err else "dry_sim_failed", "error": str(err or "")}
            except Exception as e:
                store.update(c, oid, status="dry_sim_failed", sim_err=str(e)[:300])
                return oid, {**res, "status": "dry_sim_failed", "error": str(e)}

        sig = str(tx.signatures[0])
        store.update(c, oid, status="sending", sig=sig)
        try:
            self._send(b64, first=True)
        except Exception as e:
            if str(e).startswith("rpc sendTransaction:"):  # a JSON-RPC answer: preflight refused, nothing sent
                store.update(c, oid, status="sim_failed", sim_err=str(e)[:300])
                return oid, {**res, "status": "sim_failed", "error": f"Vorab-Simulation: {e}"}
            # timeout / 5xx / connection: the node may have taken it -> confirm like any sent transaction
            log.warning("send %s: transport error, confirming anyway: %s", sig[:8], e)
            store.update(c, oid, error=f"Sendefehler, wird bestätigt: {e}"[:300])
        verdict = self._confirm(sig, b64, last_valid)
        if verdict == "landed":
            d = self._deltas(sig, mint)
            upd = {"status": "landed"}
            if d:
                upd.update(out_raw=abs(d["token_raw"]) if side == "buy" else d["sol_lamports"],
                           sol_delta_lamports=d["sol_lamports"], fee_lamports=d["fee"])
                got = abs(d["token_raw"]) if side == "buy" else d["sol_lamports"] + d["fee"]
                upd["realized_slippage"] = 1 - got / int(q["outAmount"]) if int(q["outAmount"]) else None
            store.update(c, oid, **upd)
            return oid, {**res, "status": "landed", "sig": sig, "deltas": d}
        if verdict.startswith("failed"):
            store.update(c, oid, status="failed", error=verdict[7:][:300], fee_lamports=self._fee_of(sig))
            return oid, {**res, "status": "failed", "sig": sig, "error": f"auf der Chain fehlgeschlagen: {verdict[7:]}"}
        store.update(c, oid, status=verdict, error="Blockhash abgelaufen, nicht gelandet" if verdict == "expired"
                     else "Status unklar, blockiert neue Käufe bis zur Klärung")
        return oid, {**res, "status": verdict, "sig": sig, "error": verdict}

    # ------------------------------------------------------------------ executor interface
    def buy(self, mint: str, size_sol: float, snap: market.PairSnapshot) -> Fill:
        if self.exit_only:
            return Fill(False, error=self.exit_only)
        with self._buy_lock:
            try:
                bal = self.balance()
            except Exception as e:
                return Fill(False, error=f"Guthaben nicht lesbar: {e}")
            size, why = guard.check_buy(self.conn, self.cfg, self.mode, size_sol, bal, mint)
            why = why or guard.check_coin(self.conn, self.cfg, mint, self.assess)
            if why:
                store.event_safe(self.conn, f"[{self.mode}] Kauf {snap.symbol if snap else mint[:6]} blockiert: {why}")
                return Fill(False, error=why)
            dec = chain.token_decimals(self.rpc, mint)
            try:
                oid, r = self._swap("buy", mint, int(size * 1e9), self.cfg.buy_slippage_bps, 1, snap, dec)
            except Exception as e:  # never let the live path crash the trading loop
                log.exception("live buy %s", mint[:6])
                return Fill(False, error=f"Live-Kauf Fehler: {e}")
            q = r.get("quote") or {}
            if self.dry:
                if r["status"] != "dry_ok":
                    return Fill(False, error=f"Dry-Run: {r.get('error') or r['status']}")
                fees = self.s.network_fee_sol + (r.get("priority_lamports") or 0) / 1e9
                tokens = int(q["outAmount"]) / 10 ** dec * (1 - paper_adverse()) * (size - fees) / size
                return Fill(True, tokens=tokens, sol=size, price_sol=size / tokens, price_impact=r["impact"],
                            fees_sol=fees, tx=f"dry-{oid}", source="dry_run")
            if r["status"] == "unknown":  # may still land: the watchdog books it into the 'opening' row (service.py)
                return Fill(False, error=f"{UNCONFIRMED} Kauf {r.get('sig', '')[:12]}… wird geprüft")
            if r["status"] != "landed":
                return Fill(False, error=r.get("error") or r["status"])
            d = r.get("deltas")
            if d and d.get("decimals") is not None:
                dec = d["decimals"]  # getTokenSupply may have failed and guessed 6
            if d and d["token_raw"] > 0:
                tokens, spent, fees = d["token_raw"] / 10 ** dec, -d["sol_lamports"] / 1e9, d["fee"] / 1e9
            else:  # landed but the transaction is not readable yet: the wallet's balance, else the quote, flagged
                try:
                    held = sum(a["amount"] for a in self._token_accounts(mint))
                except Exception:
                    held = 0
                tokens, spent, fees = (held or int(q["outAmount"])) / 10 ** dec, size, 0.0
            return Fill(True, tokens=tokens, sol=spent, price_sol=spent / tokens if tokens else 0,
                        price_impact=r["impact"], fees_sol=fees, tx=r["sig"],
                        source="live" if d else "live_quote_only")

    def sell(self, mint: str, tokens: float, decimals: int, snap, last_price_sol: float) -> Fill:
        if self.dry:
            raw = int(tokens * 10 ** decimals)
            try:
                oid, r = self._swap("sell", mint, raw, self.cfg.sell_slippage_bps, 1)
            except Exception as e:
                oid, r = 0, {"status": "error", "error": str(e)}
            if r["status"] != "dry_built":
                log.warning("dry-run sell %s: %s", mint[:6], r.get("error"))
            q = r.get("quote")
            if q:  # same fill rule as paper, from our quote
                fees = self.s.network_fee_sol + (r.get("priority_lamports") or 0) / 1e9
                net = max(0.0, int(q["outAmount"]) / 1e9 * (1 - paper_adverse()) - fees)
                return Fill(True, tokens=tokens, sol=net, price_sol=net / tokens if tokens else 0,
                            price_impact=r["impact"], fees_sol=fees, tx=f"dry-{oid}", source="dry_run")
            return PaperExecutor.sell(self, mint, tokens, decimals, snap, last_price_sol)
        fails, not_before = self._sell_backoff.get(mint, (0, 0.0))
        if time.time() < not_before:
            return Fill(False, error=f"Verkauf pausiert ({fails} Fehlrunden), nächster Versuch in "
                                     f"{not_before - time.time():.0f}s")
        n_chain = self.conn.execute("SELECT COUNT(*) FROM live_orders WHERE dry=0 AND side='sell' AND mint=? AND "
                                    "status='failed' AND ts>=?", (mint, int(time.time()) - 86400)).fetchone()[0]
        if n_chain >= MAX_CHAIN_FAILS_24H:
            return Fill(False, error=f"{n_chain} Verkäufe in 24 h auf der Chain gescheitert (Gebühren): "
                                     "bitte von Hand verkaufen, der Bot sendet nicht mehr")
        late = self._late_sell(mint, decimals)
        if late is not None:
            return late
        fill = self._sell_live(mint, decimals)
        if fill.ok:
            self._sell_backoff.pop(mint, None)
            return fill
        fails += 1
        self._sell_backoff[mint] = (fails, time.time() + min(600.0, 30.0 * 2 ** (fails - 1)))
        if fails == STUCK_AFTER:
            store.put(self.conn, f"stuck:{mint}", int(time.time()))
            try:
                from .service import send

                send(f"⚠️ Verkauf von {mint[:6]}… scheitert seit {fails} Runden ({fill.error}). "
                     "Bot versucht weiter mit Pausen bis 10 min; prüfen, ob der Coin noch handelbar ist.")
            except Exception:
                pass
        return fill

    def _late_sell(self, mint: str, decimals: int) -> Fill | None:
        """A sell of this coin whose fate was unclear and that landed after all: book that one, never sell twice."""
        last_buy = self.conn.execute("SELECT COALESCE(MAX(id), 0) FROM live_orders WHERE dry=0 AND side='buy' AND "
                                     "mint=? AND status IN ('landed','landed_late')", (mint,)).fetchone()[0]
        for o in self.conn.execute("SELECT * FROM live_orders WHERE dry=0 AND side='sell' AND mint=? AND id>? AND "
                                   "status IN ('unknown','sending','landed_late') ORDER BY id DESC",
                                   (mint, last_buy)).fetchall():
            try:
                st = self.rpc.call("getSignatureStatuses", [[o["sig"]], {"searchTransactionHistory": True}])
                st = ((st or {}).get("value") or [None])[0]
            except Exception:
                return Fill(False, error="früherer Verkauf noch unklar")
            if st and st.get("confirmationStatus") in ("confirmed", "finalized") and not st.get("err"):
                d = self._deltas(o["sig"], mint)
                if not d:
                    return Fill(False, error="früherer Verkauf gelandet, Transaktion noch nicht lesbar")
                store.update(self.conn, o["id"], status="landed", sol_delta_lamports=d["sol_lamports"],
                             fee_lamports=d["fee"], out_raw=d["sol_lamports"])
                sold = abs(d["token_raw"]) / 10 ** decimals
                return Fill(True, tokens=sold, sol=d["sol_lamports"] / 1e9,
                            price_sol=d["sol_lamports"] / 1e9 / sold if sold else 0, fees_sol=d["fee"] / 1e9,
                            tx=o["sig"], source="live_late")
            if st is None and o["last_valid_height"]:
                try:
                    if int(self.rpc.call("getBlockHeight", [{"commitment": "confirmed"}])) <= o["last_valid_height"]:
                        return Fill(False, error="früherer Verkauf kann noch landen")
                except Exception:
                    return Fill(False, error="früherer Verkauf noch unklar")
            store.update(self.conn, o["id"], status="failed" if st else "expired",
                         error=str(st.get("err")) if st else "nie gelandet")
        return None

    def _sell_live(self, mint: str, decimals: int) -> Fill:
        try:
            accs = self._token_accounts(mint)
        except Exception as e:
            return Fill(False, error=f"Token-Bestand nicht lesbar: {e}")
        raw = sum(a["amount"] for a in accs)
        decimals = next((int(a["decimals"]) for a in accs if a["decimals"] is not None), decimals or 6)
        if raw <= 0:  # one node's empty answer is no proof: ask a second one before writing the position off
            other = getattr(self.rpc, "public", None)
            try:
                raw2 = sum(a["amount"] for a in (self._token_accounts(mint, other) if other else []))
            except Exception:
                raw2 = None
            if raw2:
                raw = raw2
            elif other is None or raw2 is None:
                return Fill(False, error="Bestand 0 auf einer RPC, nicht bestätigt (kein Verkauf gebucht)")
        if raw <= 0:
            reclaimed = self._close_accounts(accs)
            return Fill(True, tokens=0, sol=max(0, reclaimed) / 1e9, price_sol=0, tx="", source="live_empty",
                        error="keine Token mehr in der Wallet")
        slip, last = self.cfg.sell_slippage_bps, {}
        for attempt in range(1, max(1, self.cfg.sell_attempts) + 1):
            try:
                oid, r = self._swap("sell", mint, raw, slip, attempt)
            except Exception as e:
                log.exception("live sell %s", mint[:6])
                r = {"status": "error", "error": str(e)}
            last = r
            if r["status"] == "landed":
                break
            if r["status"] == "unknown":  # might still land: never sell twice
                return Fill(False, error="Verkauf unbestätigt, wird geprüft")
            slip = min(self.cfg.sell_slippage_max_bps, slip * 2)
        else:
            return Fill(False, error=last.get("error") or last.get("status", "Verkauf fehlgeschlagen"))
        d = last.get("deltas")
        got = d["sol_lamports"] if d else int(last["quote"]["outAmount"])
        fees = d["fee"] if d else 0
        sold = abs(d["token_raw"]) if d and d["token_raw"] else raw
        left = None
        try:
            accs = self._token_accounts(mint)
            left = sum(a["amount"] for a in accs)
        except Exception:
            accs = []
        if left == 0:
            got += max(0, self._close_accounts(accs))
        sol = got / 1e9
        return Fill(True, tokens=sold / 10 ** decimals, sol=sol, price_sol=sol / (sold / 10 ** decimals),
                    price_impact=last.get("impact", 0), fees_sol=fees / 1e9, tx=last.get("sig", ""),
                    source="live" if d else "live_quote_only")

    def exit_value(self, mint: str, tokens: float, decimals: int) -> tuple[float, float] | None:
        """(SOL a sale of `tokens` would bring now after price impact and token tax, unix ts) from a Jupiter quote,
        None without a route. For stops on open live positions: DexScreener lags 10-30 s."""
        q = self.jup.quote(self.s, mint, SOL_MINT, int(tokens * 10 ** decimals), self.cfg.sell_slippage_bps)
        if not q or int(q.get("outAmount") or 0) <= 0:
            return None
        return int(q["outAmount"]) / 1e9, time.time()

    def _close_accounts(self, accs: list[dict]) -> int:
        """Closes empty token accounts, returns the rent lamports that came back (0 if anything failed)."""
        if self.dry or self.keypair is None:
            return 0
        empty = [a for a in accs if a["amount"] == 0 and a["program"] in TOKEN_PROGRAMS]
        if not empty:
            return 0
        try:
            from solders.hash import Hash  # type: ignore
            from solders.instruction import AccountMeta, Instruction  # type: ignore
            from solders.message import Message  # type: ignore
            from solders.pubkey import Pubkey  # type: ignore
            from solders.transaction import Transaction  # type: ignore

            owner = self.keypair.pubkey()
            ixs = [Instruction(Pubkey.from_string(a["program"]), bytes([9]),
                               [AccountMeta(Pubkey.from_string(a["address"]), False, True),
                                AccountMeta(owner, False, True), AccountMeta(owner, True, False)]) for a in empty]
            bh = self.rpc.call("getLatestBlockhash", [{"commitment": "confirmed"}])["value"]
            h = Hash.from_string(bh["blockhash"])
            tx = Transaction([self.keypair], Message.new_with_blockhash(ixs, owner, h), h)
            b64 = base64.b64encode(bytes(tx)).decode()
            sig = str(tx.signatures[0])
            self._send(b64, first=True)
            if self._confirm(sig, b64, int(bh.get("lastValidBlockHeight") or 0) or None) == "landed":
                return sum(int(a["lamports"] or 0) for a in empty) - 5000
        except Exception as e:
            log.warning("closing token account failed: %s", e)
        return 0


def parse_deltas(tx: dict, owner: str, mint: str) -> dict | None:
    """Raw token delta of `mint` and lamport delta (native + wSOL, fee included) for `owner` in a parsed tx."""
    meta = tx.get("meta") or {}
    if meta.get("err"):
        return None
    keys = [k["pubkey"] if isinstance(k, dict) else k for k in tx["transaction"]["message"]["accountKeys"]]

    def tok(entries, m):
        return sum(int((b.get("uiTokenAmount") or {}).get("amount") or 0)
                   for b in entries or [] if b.get("owner") == owner and b.get("mint") == m)

    token = tok(meta.get("postTokenBalances"), mint) - tok(meta.get("preTokenBalances"), mint)
    wsol = tok(meta.get("postTokenBalances"), SOL_MINT) - tok(meta.get("preTokenBalances"), SOL_MINT)
    native = 0
    if owner in keys:
        i = keys.index(owner)
        native = meta["postBalances"][i] - meta["preBalances"][i]
    dec = next((int((b.get("uiTokenAmount") or {}).get("decimals")) for b in (meta.get("postTokenBalances") or [])
                + (meta.get("preTokenBalances") or []) if b.get("mint") == mint
                and (b.get("uiTokenAmount") or {}).get("decimals") is not None), None)
    return {"token_raw": token, "sol_lamports": native + wsol, "fee": int(meta.get("fee") or 0), "decimals": dec}


def _open_mode(settings: Settings, mode: str) -> int:
    try:
        from copytrade import store as ct_store  # type: ignore

        c = ct_store.connect(settings)
        return c.execute("SELECT COUNT(*) FROM ct_positions WHERE mode=? AND status IN ('open','closing','opening')",
                         (mode,)).fetchone()[0]
    except Exception:
        return 0


def _open_real(settings: Settings) -> int:
    return _open_mode(settings, "live")


def make_executor(settings: Settings, rpc) -> GuardedLiveExecutor | None:
    """None unless LIVE_TRADING=true and a key (or LIVE_WALLET_PUBKEY for a keyless dry run) is set.
    Open real positions always get a real executor for their sells, even after LIVE_TRADING or LIVE_DRY_RUN was
    switched back: a dry or paper 'sell' of a real position would book an exit that never happened on chain."""
    cfg = load()
    open_real = _open_real(settings)
    if not cfg.enabled and not open_real:
        if _open_mode(settings, "dry"):  # dry positions left after LIVE_TRADING=false: dry exits, no wallet needed
            return GuardedLiveExecutor(settings, rpc, cfg, None, cfg.wallet_pubkey or "-", True,
                                       exit_only="Echtgeld aus: nur Dry-Positionen schließen")
        return None
    try:
        kp = load_keypair(settings.keypair_path, settings.private_key, cfg.keychain_service)
    except KeyError_ as e:
        log.error("live: %s", e)
        kp = None
    if open_real and (not cfg.enabled or cfg.dry_run):
        if kp is None:
            log.error("live: %d offene Echtgeld-Positionen, aber kein Schlüssel: Verkauf nicht möglich", open_real)
            return None
        why = ("Echtgeld aus (LIVE_TRADING)" if not cfg.enabled else "Dry-Run angefordert") + \
            f": nur Verkauf der {open_real} offenen Echtgeld-Position(en), danach neu starten"
        log.warning("live executor: %s", why)
        ex = GuardedLiveExecutor(settings, rpc, cfg, kp, pubkey_of(kp), False, exit_only=why)
        ex.dry_seller = GuardedLiveExecutor(settings, rpc, cfg, None, ex.pubkey, True, conn=ex.conn, exit_only=why)
        return ex
    pub = pubkey_of(kp) if kp else cfg.wallet_pubkey
    if not pub:
        log.error("live: LIVE_TRADING=true, aber kein Schlüssel und keine LIVE_WALLET_PUBKEY")
        return None
    ex = GuardedLiveExecutor(settings, rpc, cfg, kp, pub, cfg.dry_run)
    if not ex.dry:  # dry positions from before the switch are closed the dry way, never with real tokens
        ex.dry_seller = GuardedLiveExecutor(settings, rpc, cfg, None, pub, True, conn=ex.conn)
    log.warning("live executor: %s, Wallet %s…, %s", "DRY-RUN (sendet nichts)" if ex.dry else "ECHTGELD",
                pub[:6], cfg.stage.name)
    return ex
