"""Paper positions of the sniper. Fills like the copy trader: a Jupiter quote for the exact size when
reachable (real route and price impact), otherwise the bonding-curve formula on the live curve state;
then adverse slippage for latency plus network and priority fees. Exits on the live curve price."""
import json
import logging

from core import db
from sniper import store
from sniper.state import curve_buy, curve_sell

log = logging.getLogger("sniper.paper")


class PaperBook:
    def __init__(self, cfg, conn):
        self.cfg = cfg
        self.open = {}  # mint -> position dict
        for r in conn.execute("SELECT * FROM sniper_positions WHERE status='open'"):
            p = dict(r)
            p["events"] = json.loads(p.get("events_json") or "[]")
            p["insider_base"] = {}
            self.open[p["mint"]] = p

    # ---- balance
    def balance(self, conn):
        v = store.get_state(conn, "paper_balance_sol")
        if v is None:
            store.set_state(conn, "paper_balance_sol", self.cfg.paper_start_sol)
            return self.cfg.paper_start_sol
        return float(v)

    def _adjust(self, conn, delta):
        store.set_state(conn, "paper_balance_sol", self.balance(conn) + delta)

    def capacity(self, conn, now):
        if len(self.open) >= self.cfg.max_open:
            return f"schon {len(self.open)} offene Positionen (Maximum {self.cfg.max_open})"
        n = conn.execute("SELECT COUNT(*) FROM sniper_positions WHERE opened_ts >= ?", (now - 3600,)).fetchone()[0]
        if n >= self.cfg.max_entries_per_hour:
            return f"{n} Käufe in der letzten Stunde (Maximum {self.cfg.max_entries_per_hour})"
        if self.balance(conn) < self.cfg.size_sol:
            return "Papier-Guthaben zu klein"
        return None

    # ---- fills
    def _fees(self):
        return self.cfg.priority_fee_sol + self.cfg.network_fee_sol

    def fill_buy(self, st, size_sol, quote_fn=None):
        spend = size_sol - self._fees()
        tokens, impact, src = 0.0, 0.0, ""
        if quote_fn:
            q = quote_fn(st.mint, spend, "buy")
            if q and q.get("out", 0) > 0:
                tokens, impact, src = q["out"], q.get("impact", 0.0), "jupiter_quote"
        if tokens <= 0 and st.v_sol > 0 and not st.migrated:
            tokens = curve_buy(st.v_sol, st.v_tok, spend, self.cfg.curve_fee_pct)
            p0 = st.price_sol
            impact = 1 - p0 / (spend / tokens) if tokens and p0 else 0.0
            src = "bonding_curve"
        tokens *= 1 - self.cfg.adverse_slippage
        return tokens, abs(impact), src

    def fill_sell(self, st, tokens, quote_fn=None):
        gross, src = 0.0, ""
        on_curve = st is not None and st.subscribed and not st.migrated and st.v_sol > 0
        if on_curve:
            gross, src = curve_sell(st.v_sol, st.v_tok, tokens, self.cfg.curve_fee_pct), "bonding_curve"
        elif quote_fn:
            q = quote_fn(st.mint, tokens, "sell")
            if q and q.get("out", 0) > 0:
                gross, src = q["out"], "jupiter_quote"
        if not src:
            price = (st.current_mcap_sol() / st.supply) if st else 0.0
            gross, src = tokens * price * 0.95, "price_estimate"
        gross *= 1 - self.cfg.adverse_slippage
        return max(0.0, gross - self._fees()), src

    # ---- open / close
    def enter(self, conn, st, score, now, quote_fn=None, sol_usd=0.0):
        why = self.capacity(conn, now)
        if why:
            return None, why
        size = self.cfg.size_sol
        tokens, impact, src = self.fill_buy(st, size, quote_fn)
        if tokens <= 0:
            return None, "kein Preis für den Einstieg"
        price = size / tokens
        p = {"mint": st.mint, "symbol": st.symbol, "mode": "paper", "opened_ts": int(now), "entry_price_sol": price,
             "entry_mcap_sol": st.current_mcap_sol(), "entry_mcap_usd": st.current_mcap_sol() * sol_usd if sol_usd else None,
             "size_sol": size, "tokens": tokens, "remaining": tokens, "fees_sol": self._fees(), "fill_src": src,
             "score": score, "peak_price_sol": price, "last_price_sol": price, "tp1_done": 0, "realized_sol": 0.0,
             "status": "open", "params_json": store.j(self.cfg.exit_params()),
             "events": [{"ts": int(now), "kind": "buy", "sol": size, "tokens": tokens, "price": price,
                         "impact": round(impact, 4), "src": src}]}
        p["insider_base"] = dict(st.insider_sold)

        def write():
            cur = conn.execute(
                """INSERT INTO sniper_positions(mint, symbol, mode, opened_ts, entry_price_sol, entry_mcap_sol,
                       entry_mcap_usd, size_sol, tokens, remaining, fees_sol, fill_src, score, peak_price_sol,
                       last_price_sol, tp1_done, realized_sol, status, params_json, events_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (p["mint"], p["symbol"], "paper", p["opened_ts"], price, p["entry_mcap_sol"], p["entry_mcap_usd"],
                 size, tokens, tokens, p["fees_sol"], src, score, price, price, 0, 0.0, "open", p["params_json"],
                 store.j(p["events"])))
            conn.commit()
            return cur.lastrowid
        p["id"] = db.retry_locked(conn, write)
        self._adjust(conn, -size)
        self.open[st.mint] = p
        log.info("PAPIER-KAUF %s %s: %.3f SOL zu MCAP %.1f SOL (%s, Score %.0f)", st.symbol, st.mint[:6], size,
                 p["entry_mcap_sol"], src, score)
        return p, None

    def _save(self, conn, p):
        def write():
            conn.execute(
                """UPDATE sniper_positions SET remaining=?, peak_price_sol=?, last_price_sol=?, tp1_done=?,
                       realized_sol=?, status=?, closed_ts=?, exit_reason=?, pnl_sol=?, pnl_pct=?, events_json=?
                   WHERE id=?""",
                (p["remaining"], p["peak_price_sol"], p["last_price_sol"], p["tp1_done"], p["realized_sol"],
                 p["status"], p.get("closed_ts"), p.get("exit_reason"), p.get("pnl_sol"), p.get("pnl_pct"),
                 store.j(p["events"]), p["id"]))
            conn.commit()
        db.retry_locked(conn, write)

    def sell(self, conn, st, p, frac, reason, now, quote_fn=None):
        qty = p["remaining"] if frac >= 0.999 else p["remaining"] * frac
        if qty <= 0:
            return 0.0
        sol, src = self.fill_sell(st, qty, quote_fn)
        p["remaining"] -= qty
        p["realized_sol"] += sol
        p["events"].append({"ts": int(now), "kind": "sell", "reason": reason, "tokens": qty, "sol": round(sol, 6),
                            "price": round(sol / qty, 12) if qty else None, "src": src})
        self._adjust(conn, sol)
        if p["remaining"] <= p["tokens"] * 1e-6:
            p.update(status="closed", closed_ts=int(now), exit_reason=reason, remaining=0.0,
                     pnl_sol=p["realized_sol"] - p["size_sol"], pnl_pct=p["realized_sol"] / p["size_sol"] - 1)
            self.open.pop(p["mint"], None)
            log.info("PAPIER-VERKAUF %s %s (%s): %+.4f SOL (%+.0f %%)", p["symbol"], p["mint"][:6], reason,
                     p["pnl_sol"], 100 * p["pnl_pct"])
        self._save(conn, p)
        return sol

    def check(self, conn, st, now, quote_fn=None):
        """Apply the exit rules to the open position of this launch (if any). Returns the exit reason or None."""
        p = self.open.get(st.mint)
        if not p or st is None:
            return None
        c = self.cfg
        mcap = st.current_mcap_sol()
        if mcap <= 0:
            return None
        price = mcap / st.supply
        if not st.migrated and st.subscribed and st.v_tok > 0:
            price = st.price_sol
        p["last_price_sol"] = price
        p["peak_price_sol"] = max(p["peak_price_sol"], price)
        entry, held = p["entry_price_sol"], (now - p["opened_ts"]) / 60
        reason, frac = None, 1.0
        base = p.get("insider_base") or {}
        if c.exit_on_insider_sell and any(100 * (s - base.get(w, 0.0)) >= c.insider_sell_min_pct
                                          for w, s in st.insider_sold.items()):
            reason = "insider_sell"
        elif price <= entry * (1 - c.stop_pct / 100):
            reason = "stop"
        elif not p["tp1_done"] and price >= entry * (1 + c.tp1_pct / 100):
            reason, frac = "tp1", c.tp1_frac
        elif p["tp1_done"] and price <= p["peak_price_sol"] * (1 - c.trail_pct / 100):
            reason = "trail"
        elif not p["tp1_done"] and held >= c.time_stop_min:
            reason = "time"
        elif held >= c.max_hold_min:
            reason = "max_hold"
        if not reason:
            return None
        if reason == "tp1":
            p["tp1_done"] = 1
        self.sell(conn, st, p, frac, reason, now, quote_fn)
        if reason == "insider_sell":
            p["insider_base"] = dict(st.insider_sold)
        return reason
