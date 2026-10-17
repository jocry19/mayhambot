"""The brain's paper account: sees every opportunity, decides, opens and manages its own positions.

Opportunities are read from the shared DB as the other services write them: whale buys (ct_swaps, from the copy
tracker), smart-money signals (signals) and sniper entries (sniper_launches). Every opportunity is recorded with
its context and the decision, also the ones the brain lets pass: those are what it learns most from.
Paper only: fills come from copytrade's PaperExecutor (real Jupiter quotes, measured slippage, fees).
"""
from __future__ import annotations

import json
import logging
import random
import time

from . import decide, features, store
from .plans import BY_NAME

log = logging.getLogger("brain.trader")

DEDUP_S = 6 * 3600  # one opportunity per (whale entity or kind, coin) in this window: repeat buys are no new idea
REENTRY_S = 6 * 3600  # no second brain position in a coin within this window: a coin we just lost on is no new idea
TRAIL_MIN_HOLD_S = 60  # the trail arms after a minute: the first quotes after a buy are noise, not a run
PRICE_RETRY_S = 30  # a coin without a DexScreener pair yet gets this long to show one before it is decided
FLIP_MAX = 0.6  # wallets that sell this share of their buys within FLIP_S are gone before a copy can follow
NO_MARKET_S = 15 * 60  # an open position without any price this long is closed as "kein Markt"
RUG_LIQ_DROP = 0.8
INFLOW_DROP = 0.6  # 5-min buys under this share of their peak while in profit: the rush is over
EXIT_NAMES = {"target": "Ziel erreicht", "stop": "Stop-Loss", "trail": "Trailing-Stop", "time": "Zeit abgelaufen",
              "whale": "Wal verkauft", "inflow_peak": "Ansturm vorbei", "rug": "Liquidität abgezogen",
              "no_market": "kein Markt"}


def _default_safety(conn, mint):
    """(level, blocked, checked, reasons): the stored rug verdict while fresh, else the quick check (~4 RPC calls)."""
    import safety  # type: ignore

    a = safety.assess(mint, conn, deep=False)
    return a.level, bool(a.blocked), a.level != "unchecked", [r.get("text") for r in (a.reasons or [])[:2]]


class Trader:
    def __init__(self, conn, s, model, *, snapshots=None, executor=None, safety_check=None, notify=None,
                 on_close=None, rng: random.Random | None = None, clock=time.time):
        self.conn, self.s, self.model = conn, s, model
        self._snapshots = snapshots
        self._executor = executor
        self.safety_check = safety_check or _default_safety
        self.notify = notify or (lambda text: None)
        self.on_close = on_close or (lambda pos: None)
        self.rng = rng or random.Random()
        self.clock = clock
        self.pending: dict[tuple[str, str], tuple] = {}  # (kind, ref) -> (row, entity, first seen): waiting for a pair

    # ------------------------------------------------------------------ wiring
    def snapshots(self, mints) -> dict:
        if self._snapshots is None:
            from copytrade import market

            return market.snapshots(mints)
        return self._snapshots(mints)

    @property
    def executor(self):
        if self._executor is None:
            from copytrade import chain, config as ct_config
            from copytrade.execution import PaperExecutor

            cs = ct_config.load()
            self._executor = PaperExecutor(cs, chain.Rpc(cs))
        return self._executor

    # ------------------------------------------------------------------ cursors
    def _cursor(self, key: str, sql: str) -> int:
        v = store.get(self.conn, key)
        if v is None:  # first start: begin with what comes next, never trade the backlog
            try:
                v = self.conn.execute(sql).fetchone()[0] or 0
            except Exception:
                v = 0
            store.put(self.conn, key, v)
        return int(v)

    def candidates(self) -> list[tuple[str, str, object]]:
        """New opportunities since the last scan: [(kind, ref, row)]."""
        now = self.clock()
        out = []
        c = self.conn
        if store.has(c, "ct_swaps"):
            cur = self._cursor("cursor_whale", "SELECT MAX(rowid) FROM ct_swaps")
            rows = c.execute("SELECT rowid AS rid, * FROM ct_swaps WHERE rowid>? ORDER BY rowid LIMIT 500",
                             (cur,)).fetchall()
            for r in rows:
                if (r["side"] == "buy" and (r["sol_amount"] or 0) >= self.s.min_whale_sol
                        and now - (r["ts"] or 0) <= self.s.max_whale_delay_s):
                    out.append(("whale", r["sig"], r))
            if rows:
                store.put(c, "cursor_whale", rows[-1]["rid"])
        if store.has(c, "signals"):
            cur = self._cursor("cursor_signal", "SELECT MAX(id) FROM signals")
            rows = c.execute("SELECT * FROM signals WHERE id>? ORDER BY id LIMIT 200", (cur,)).fetchall()
            for r in rows:
                if now - (r["ts"] or 0) <= self.s.max_signal_age_s:
                    out.append(("signal", str(r["id"]), r))
            if rows:
                store.put(c, "cursor_signal", rows[-1]["id"])
        if store.has(c, "sniper_launches") and store.has(c, "sniper_positions"):
            # 'enter' is written at stage 2 together with the sniper's paper position, after decided_ts (stage 1):
            # the cursor runs on the position's opened_ts, as copytrade.engine.follow_launches does
            cur = self._cursor("cursor_launch", "SELECT MAX(opened_ts) FROM sniper_positions")
            rows = c.execute("""SELECT l.*, p.opened_ts AS entered_ts FROM sniper_launches l
                                JOIN sniper_positions p ON p.mint = l.mint
                                WHERE l.decision='enter' AND p.opened_ts>? ORDER BY p.opened_ts LIMIT 200""",
                             (cur,)).fetchall()
            for r in rows:
                if now - (r["entered_ts"] or 0) <= self.s.max_launch_age_s:
                    out.append(("launch", r["mint"], r))
            if rows:
                store.put(c, "cursor_launch", rows[-1]["entered_ts"])
        return out

    def _seen(self, kind: str, ref: str, mint: str, entity: str, now: float) -> bool:
        if self.conn.execute("SELECT 1 FROM brain_opps WHERE kind=? AND ref=?", (kind, ref)).fetchone():
            return True
        return self.conn.execute("SELECT 1 FROM brain_opps WHERE kind=? AND mint=? AND entity=? AND ts>?",
                                 (kind, mint, entity, int(now - DEDUP_S))).fetchone() is not None

    # ------------------------------------------------------------------ deciding
    def scan(self) -> int:
        """Decide every new opportunity. Returns the number of positions opened."""
        cands = self.candidates()
        if not cands and not self.pending:
            return 0
        now = self.clock()
        fresh = [(kind, ref, row, ent) for (kind, ref), (row, ent, _) in self.pending.items()]
        for kind, ref, row in cands:
            ent = kind
            if kind == "whale":
                from copytrade import store as ct_store

                ent = ct_store.entity(self.conn, row["wallet"])
            if (kind, ref) not in self.pending and not self._seen(kind, ref, row["mint"], ent, now):
                fresh.append((kind, ref, row, ent))
                self.pending[(kind, ref)] = (row, ent, now)
        if not fresh:
            return 0
        try:
            snaps = self.snapshots([r["mint"] for _, _, r, _ in fresh])
        except Exception as e:
            log.warning("snapshots failed: %s", e)
            snaps = {}
        opened = 0
        for kind, ref, row, ent in fresh:
            snap = snaps.get(row["mint"])
            if (snap is None or not snap.price_sol) and now - self.pending[(kind, ref)][2] < PRICE_RETRY_S:
                continue  # brand-new coins often show up at DexScreener a few seconds after the first buys
            self.pending.pop((kind, ref), None)
            try:
                opened += bool(self.decide(kind, ref, row, ent, snaps.get(row["mint"])))
            except Exception as e:  # one bad row must not stop the others
                log.exception("decision %s %s failed: %s", kind, ref, e)
        return opened

    def decide(self, kind: str, ref: str, row, entity: str, snap) -> int | None:
        now = self.clock()
        builder = {"whale": features.whale, "signal": features.signal, "launch": features.launch}[kind]
        feats, raw = builder(self.conn, row, snap, now)
        symbol = (snap.symbol if snap else "") or (row["symbol"] if "symbol" in row.keys() else "") or ""
        d = decide.choose(self.model, kind, feats, self.s, self.rng)
        block = None
        wanted = d.enter and not d.explore  # convinced before the hard gates: what the honest score judges
        if d.enter:
            block = self._gate(kind, row["mint"], snap, d, row, raw)
        if block:
            d.enter = False
            fl = raw.get("flip") or {}
            log.info("PASS %s %s plan=%s%s: %s", kind, symbol or row["mint"][:6], d.plan,
                     f" flip={fl['share']:.0%}/{fl['n']}" if fl.get("share") is not None else "", block)
        note = decide.note(kind, raw, symbol, d, block or "")
        src_ts = row["entered_ts"] if kind == "launch" else row["ts"]
        cur = self.conn.execute(
            """INSERT OR IGNORE INTO brain_opps(ts, decided_at, kind, ref, mint, symbol, wallet, entity, src_ts,
                   price_sol, features, raw, decision, wanted, plan, pred_mean, pred_std, pred_p, sample, explore, size_sol,
                   note)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (int(now), now, kind, ref, row["mint"], symbol, row["wallet"] if kind == "whale" else "", entity,
             src_ts, snap.price_sol if snap else None, json.dumps(feats), json.dumps(raw, default=str),
             "enter" if d.enter else "skip", int(wanted), d.plan, d.mean, d.std, d.p_pos, d.sample, int(d.explore),
             d.size_sol if d.enter else None, note))
        opp_id = cur.lastrowid
        if not d.enter or not cur.rowcount:
            return None
        return self.open(opp_id, kind, row, snap, d, note, symbol)

    def _gate(self, kind: str, mint: str, snap, d, row=None, raw=None) -> str | None:
        """Hard limits the model never overrides: a market to trade, the rug check, risk limits."""
        if snap is None or not snap.price_sol:
            return "kein Marktpreis"
        if features.on_curve(snap):
            if snap.fdv < self.s.min_curve_mcap_usd:
                return f"Bonding Curve noch zu klein (Market Cap ${snap.fdv:,.0f})"
        elif snap.liquidity_usd < self.s.min_liquidity_usd:
            return f"Liquidität zu klein (${snap.liquidity_usd:,.0f})"
        c = self.conn
        if c.execute("SELECT 1 FROM brain_positions WHERE mint=? AND status='open'", (mint,)).fetchone():
            return "schon eine offene Position in diesem Coin"
        if c.execute("SELECT 1 FROM brain_positions WHERE mint=? AND opened_at>=?",
                     (mint, int(self.clock() - REENTRY_S))).fetchone():
            return "Coin in den letzten 6 h schon gehandelt"
        if kind == "whale" and row is not None:
            if c.execute("SELECT 1 FROM ct_swaps WHERE wallet=? AND mint=? AND side='sell' AND ts>=?",
                         (row["wallet"], mint, row["ts"])).fetchone():
                return "Wal hat schon wieder verkauft"
            fl = (raw or {}).get("flip") or {}
            if fl.get("n", 0) >= 5 and fl.get("share", 0) >= FLIP_MAX:
                return f"Wal verkauft meist sofort wieder ({fl['share']:.0%} seiner Käufe binnen 2 min)"
        n_open = c.execute("SELECT COUNT(*) FROM brain_positions WHERE status='open'").fetchone()[0]
        if n_open >= self.s.max_open:
            return f"schon {n_open} Positionen offen"
        t0 = store.day_start(self.clock())
        lost = c.execute("SELECT COALESCE(SUM(pnl_sol),0) FROM brain_positions WHERE status='closed' AND closed_at>=?",
                         (t0,)).fetchone()[0]
        if lost <= -self.s.max_daily_loss_sol:
            return f"Tagesverlust-Limit erreicht ({lost:+.2f} SOL)"
        if d.explore:
            n = c.execute("SELECT COUNT(*) FROM brain_opps WHERE explore=1 AND decision='enter' AND ts>=?",
                          (t0,)).fetchone()[0]
            if n >= self.s.explore_per_day:
                return f"Erkundungs-Budget für heute verbraucht ({n})"
        if store.balance(c, self.s.start_balance_sol) < d.size_sol + 0.01:
            return "Guthaben zu klein"
        try:
            level, blocked, checked, reasons = self.safety_check(c, mint)
        except Exception as e:
            log.debug("safety %s: %s", mint[:6], e)
            return "Rug-Prüfung nicht möglich (kein Kauf ohne Prüfung)"
        if blocked:
            return "Rug-Schutz blockiert (" + ("; ".join(r for r in reasons if r) or level) + ")"
        if not checked:
            return "Rug-Prüfung ohne Ergebnis (kein Kauf ohne Prüfung)"
        return None

    # ------------------------------------------------------------------ positions
    def open(self, opp_id: int, kind: str, row, snap, d, note: str, symbol: str) -> int | None:
        c = self.conn
        try:
            fill = self.executor.buy(row["mint"], d.size_sol, snap)
        except Exception as e:
            fill = None
            log.warning("paper buy %s failed: %s", row["mint"][:6], e)
        if not fill or not fill.ok:
            why = getattr(fill, "error", "") or "kein Fill"
            c.execute("UPDATE brain_opps SET decision='skip', note=note || ? WHERE id=?",
                      (f"\nKauf nicht ausgeführt: {why}.", opp_id))
            return None
        p = BY_NAME[d.plan]
        now = int(self.clock())
        src_ts = row["ts"] if kind == "whale" else None
        cur = c.execute(
            """INSERT INTO brain_positions(opp_id, kind, mint, symbol, wallet, src_ts, opened_at, entry_price_sol,
                   size_sol, tokens, decimals, plan, take_profit, stop_loss, trail_activate, trail_drawdown, max_hold_min,
                   inflow_exit, whale_exit, entry_liq_usd, high_price_sol, last_price_sol, last_seen, peak_buys_m5,
                   note, meta)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (opp_id, kind, row["mint"], symbol, row["wallet"] if kind == "whale" else "", src_ts, now,
             fill.price_sol, fill.sol, fill.tokens, fill.decimals, p.name, p.take_profit, p.stop_loss,
             p.trail_activate, p.trail_drawdown, p.max_hold_min, p.inflow_exit, int(kind == "whale"),
             snap.liquidity_usd, snap.price_sol, snap.price_sol, now, snap.buys_m5, note,
             json.dumps({"fill_source": fill.source, "impact": fill.price_impact, "ref_price": snap.price_sol})))
        pid = cur.lastrowid
        store.adjust_balance(c, -fill.sol, self.s.start_balance_sol)
        c.execute("UPDATE brain_opps SET position_id=? WHERE id=?", (pid, opp_id))
        log.info("BUY %s %s %.2f SOL plan=%s", kind, symbol or row["mint"][:6], fill.sol, p.name)
        self.notify(f"🧠 Kauf {symbol or row['mint'][:6]} ({kind})\n{note}")
        return pid

    def manage(self) -> int:
        """Check every open position against its plan. Returns the number closed."""
        c = self.conn
        rows = c.execute("SELECT * FROM brain_positions WHERE status='open'").fetchall()
        if not rows:
            return 0
        try:
            snaps = self.snapshots([r["mint"] for r in rows])
        except Exception as e:
            log.warning("snapshots failed: %s", e)
            return 0
        now = self.clock()
        n = 0
        for p in rows:
            snap = snaps.get(p["mint"])
            reason = self._exit_reason(p, snap, now)
            if snap and snap.price_sol > 0:
                c.execute("UPDATE brain_positions SET last_price_sol=?, high_price_sol=MAX(high_price_sol, ?), "
                          "last_seen=?, peak_buys_m5=MAX(COALESCE(peak_buys_m5,0), ?) WHERE id=?",
                          (snap.price_sol, snap.price_sol, int(now), snap.buys_m5 or 0, p["id"]))
            if reason:
                n += self.close(p, snap, reason)
        return n

    def _exit_reason(self, p, snap, now: float) -> str | None:
        held_min = (now - p["opened_at"]) / 60
        if not snap or not snap.price_sol:
            return "no_market" if now - (p["last_seen"] or p["opened_at"]) > NO_MARKET_S else None
        # targets, stops and the trail compare like with like: the market price now against the market price at the
        # buy; the fill price carries slippage and comes from another source, so mixing them fires exits on noise
        entry = _ref_price(p)
        px = snap.price_sol
        high = max(p["high_price_sol"] or entry, px)
        if p["entry_liq_usd"] and snap.liquidity_usd < (1 - RUG_LIQ_DROP) * p["entry_liq_usd"]:
            return "rug"
        trail_on = (p["trail_activate"] and now - p["opened_at"] >= TRAIL_MIN_HOLD_S
                    and high / entry - 1 >= p["trail_activate"])
        stop_px = max(entry * (1 - p["stop_loss"]), high * (1 - p["trail_drawdown"]) if trail_on else 0)
        if px <= stop_px:
            return "trail" if trail_on and stop_px > entry * (1 - p["stop_loss"]) else "stop"
        if px >= entry * (1 + p["take_profit"]):
            return "target"
        peak = max(p["peak_buys_m5"] or 0, snap.buys_m5 or 0)
        if (p["inflow_exit"] and held_min >= 5 and px / entry - 1 > 0.05 and peak >= 10
                and (snap.buys_m5 or 0) < INFLOW_DROP * peak):
            return "inflow_peak"
        if p["whale_exit"] and p["wallet"] and self.conn.execute(
                "SELECT 1 FROM ct_swaps WHERE wallet=? AND mint=? AND side='sell' AND ts>=?",
                (p["wallet"], p["mint"], p["src_ts"] or p["opened_at"])).fetchone():
            return "whale"
        if held_min >= p["max_hold_min"]:
            return "time"
        return None

    def close(self, p, snap, reason: str) -> bool:
        c = self.conn
        try:
            fill = self.executor.sell(p["mint"], p["tokens"], p["decimals"] if p["decimals"] is not None else 6, snap,
                                      p["last_price_sol"] or p["entry_price_sol"])
        except Exception as e:
            log.warning("paper sell %s failed: %s", p["mint"][:6], e)
            return False
        if not fill or not fill.ok:
            return False
        if getattr(fill, "source", "") == "no_route" and features.on_curve(snap):
            return False  # a coin on the bonding curve has a market; a missing quote is a hiccup, retry next round
        now = int(self.clock())
        proceeds = fill.sol
        pnl = proceeds - p["size_sol"]
        pct = pnl / p["size_sol"] if p["size_sol"] else 0.0
        done = c.execute("UPDATE brain_positions SET status='closed', closed_at=?, exit_price_sol=?, proceeds_sol=?, "
                         "pnl_sol=?, pnl_pct=?, exit_reason=? WHERE id=? AND status='open'",
                         (now, fill.price_sol, proceeds, pnl, pct, reason, p["id"])).rowcount
        if not done:
            return False
        store.adjust_balance(c, proceeds, self.s.start_balance_sol)
        held = (now - p["opened_at"]) / 60
        log.info("SELL %s %s %+.1f%% (%s)", p["kind"], p["symbol"] or p["mint"][:6], pct * 100, reason)
        self.notify(f"🧠 Verkauf {p['symbol'] or p['mint'][:6]}: {pct:+.1%} ({pnl:+.3f} SOL) nach {held:.0f} min, "
                    f"{EXIT_NAMES.get(reason, reason)}")
        closed = c.execute("SELECT * FROM brain_positions WHERE id=?", (p["id"],)).fetchone()
        try:
            self.on_close(closed)
        except Exception as e:
            log.exception("learning from trade %s failed: %s", p["id"], e)
        return True

    def equity_snapshot(self) -> dict:
        c = self.conn
        bal = store.balance(c, self.s.start_balance_sol)
        val = sum((r[0] or 0) * (r[1] or 0) for r in c.execute(
            "SELECT tokens, last_price_sol FROM brain_positions WHERE status='open'").fetchall())
        ts = int(self.clock()) // 60 * 60
        c.execute("INSERT OR REPLACE INTO brain_equity VALUES (?,?,?,?)", (ts, bal, val, bal + val))
        return {"balance_sol": bal, "open_value_sol": val, "equity_sol": bal + val}


def _ref_price(p) -> float:
    try:
        ref = json.loads(p["meta"] or "{}").get("ref_price")
    except (TypeError, ValueError):
        ref = None
    return float(ref) if ref else p["entry_price_sol"]
