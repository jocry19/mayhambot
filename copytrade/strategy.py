"""Entry and exit decisions for both strategies.

Mode 1 · whale: a tracked whale buys >= CT_MIN_WHALE_BUY_SOL, the wallet's ranking
says its buys are followed by pumps we can still catch, we are fast enough (buy
younger than CT_MAX_ENTRY_DELAY_S, price not up more than CT_MAX_RUN_SINCE_WHALE,
copy-trader rush not there yet), liquidity and preset limits are OK.

Mode 2 · signal: the signal engine (core `signals` table) fires a smart-money signal
with score >= preset.min_score and >= preset.min_smart_buyers smart wallets, the
signal is younger than preset.max_signal_age_s, liquidity/mcap fit the preset.
If the research model (model/) is installed, its prediction gates the entry.

Exit (first that fires; in "semi" mode only shown as a hint, you sell):
  stop         price <= entry * (1 - stop_loss)
  target       price >= entry * (1 + take_profit) (whale: learned per wallet)
  inflow_peak  whale only: the copy-trader rush peaked (5-min buys 40 % under peak) while in profit
  trail        after +trail_activate the price fell trail_drawdown from its high
  whale        the copied whale sells
  smart_exit   signal only: the signal's smart wallets sold more than half of what they bought
  time         held longer than max_hold_min
"""
from __future__ import annotations

import json
import logging
import time

from . import control, learn, market, ranking, store
from .chain import Swap, _decimals_cache
from .config import Settings

log = logging.getLogger("copytrade.strategy")

COIN_WINDOW_S = 6 * 3600  # window of Settings.max_copies_per_coin
LAUNCH_MAX_RUN = 0.3  # mode 3: market cap up more than this since the sniper decided -> not bought
LAUNCH_MAX_AGE_S = 60  # mode 3: a sniper decision older than this is not bought (launch prices move in seconds)
EXIT_NAMES = {"target": "Ziel erreicht", "inflow_peak": "Copy-Ansturm vorbei", "trail": "Trailing-Stop",
              "stop": "Stop-Loss", "time": "Zeit abgelaufen", "whale": "Wal verkauft", "no_market": "kein Markt",
              "smart_exit": "Smart Money steigt aus", "manual": "von dir verkauft"}


class Strategy:
    def __init__(self, conn, s: Settings, executors):
        """executors: {'paper': PaperExecutor, 'live': LiveExecutor or None} (or one executor, for tests)"""
        self.conn = conn
        self.s = s
        if not isinstance(executors, dict):
            executors = {executors.mode: executors}
        self.exs = executors
        self._missing: dict[int, int] = {}
        self._pending: list[tuple[float, Swap]] = []  # whale buys waiting for the learned entry delay
        control.init(conn)
        try:  # a sell that never finished (crash mid-close): back to open so the exits can retry
            conn.execute("UPDATE ct_positions SET status='open', closed_at=NULL WHERE status='closing' AND closed_at<?",
                         (int(time.time()) - 300,))
            conn.commit()
        except Exception:
            pass

    # ------------------------------------------------------------------ helpers
    def executor(self, strategy: str):
        if strategy in control.STRATEGIES and control.live_wanted(self.conn, strategy) and self.exs.get("live"):
            return self.exs["live"]
        return self.exs.get("paper") or next(iter(self.exs.values()))

    def _ex_for_position(self, p):
        return self.exs.get(p["mode"]) or self.executor(p["strategy"] or "whale")

    def open_positions(self, strategy: str | None = None, mode: str | None = None) -> list:
        q, args = "SELECT * FROM ct_positions WHERE status='open'", []
        if strategy:
            q += " AND strategy=?"
            args.append(strategy)
        if mode:
            q += " AND mode=?"
            args.append(mode)
        return self.conn.execute(q, args).fetchall()

    def _daily_pnl(self, strategy: str, mode: str) -> float:
        r = self.conn.execute(
            """SELECT COALESCE(SUM(pnl_sol),0) FROM ct_positions
               WHERE mode=? AND strategy=? AND status='closed' AND closed_at>=?""",
            (mode, strategy, int(time.time()) - 86400),
        ).fetchone()
        return float(r[0])

    def _risk_block(self, strategy: str, p: control.Preset, size: float, mint: str) -> str | None:
        ex = self.executor(strategy)
        if control.paused(self.conn):
            return "Handel pausiert"
        if any(r["mint"] == mint for r in self.open_positions()):
            return "schon im Coin"
        if len(self.open_positions(strategy, ex.mode)) >= p.max_open:
            return f"max. {p.max_open} offene Positionen"
        if self._daily_pnl(strategy, ex.mode) <= -p.max_daily_loss_sol:
            return "Tagesverlust-Limit erreicht"
        if ex.balance(self.conn) < size + 0.01:
            return "Guthaben zu klein"
        return None

    def _safety(self, strategy: str, mint: str) -> tuple[dict, str | None]:
        """Rug/honeypot/insider check (safety/) right before every buy, paper and live alike: the stored result while
        it is fresh, else the quick check (about 4 RPC calls). A coin no check could clear is not bought, so the
        paper results show what the protection is worth (10/2026: 28 of 57 paper buys had no check at all)."""
        try:
            import safety  # type: ignore

            a = safety.assess(mint, self.conn, deep=False)
            info = {"level": a.level, "score": round(a.score, 1) if a.score is not None else None,
                    "blocked": bool(a.blocked), "checked": a.level != "unchecked",
                    "reasons": [r["text"] for r in (a.reasons or [])[:3]]}
        except Exception as e:
            log.debug("safety check %s failed: %s", mint[:6], e)
            return {"level": "unchecked", "checked": False}, "Rug-Prüfung nicht möglich (kein Kauf ohne Prüfung)"
        if info["blocked"]:
            why = "; ".join(info["reasons"][:2]) or info["level"]
            return info, f"Rug-Schutz blockiert ({why})"
        if not info["checked"]:
            return info, "Rug-Prüfung ohne Ergebnis (kein Kauf ohne Prüfung)"
        return info, None

    def _skip(self, strategy: str, mint: str, why: str, snap=None, **data) -> None:
        sym = snap.symbol if snap else ""
        store.event(self.conn, "skip", f"[{strategy}] {sym or mint[:6] + '…'}: {why}",
                    data={"strategy": strategy, "mint": mint, "symbol": sym, "reason": why, **data})

    # ------------------------------------------------------------------ mode 1: whales
    def on_whale_buy(self, swap: Swap, deferred: bool = False) -> int | None:
        s = self.s
        m = control.mode(self.conn, "whale")
        if m == "off" or swap.sol_amount < s.min_whale_buy_sol:
            return None
        age = time.time() - swap.block_time
        lp = learn.params_for(self.conn, "whale", swap.wallet)
        if lp and not deferred and m != "manual" and lp.get("delay_s", 0) > age + 5:
            # learned: entering a bit after the whale beats entering at once for this whale / mode
            self._pending.append((swap.block_time + lp["delay_s"], swap))
            store.event(self.conn, "wait", f"[whale] {swap.mint[:6]}…: warte {lp['delay_s'] - age:.0f}s "
                                           f"(gelernte Einstiegs-Verzögerung)", data={"wallet": swap.wallet})
            return None
        if age > s.max_entry_delay_s + (lp or {}).get("delay_s", 0) * deferred:
            return self._skip("whale", swap.mint, f"zu spät erkannt ({age:.0f}s)", wallet=swap.wallet)
        p = control.preset(self.conn, "whale")
        ex = self.executor("whale")
        nb, kb = launch_bot_share(self.conn, swap.wallet)
        if nb >= LAUNCH_BOT_MIN_N and kb / nb >= LAUNCH_BOT_SHARE:
            return self._skip("whale", swap.mint, f"Launch-/Dev-Bot ({kb} von {nb} Käufen in den ersten "
                              f"{LAUNCH_BOT_S} s eines Coins), nicht kopierbar", wallet=swap.wallet)
        rank = ranking.wallet_rank(self.conn, swap.wallet)
        size, explore, edge = p.size_sol, False, None
        if rank is None or rank["samples"] < s.min_wallet_samples:
            if not (s.explore_unranked and ex.mode == "paper"):
                return self._skip("whale", swap.mint, "Wallet noch nicht bewertet (nur mitgemessen)",
                                  wallet=swap.wallet)
            explore, size = True, size * 0.5
        elif rank["score"] < s.min_wallet_score:
            return self._skip("whale", swap.mint, f"Wallet-Score zu niedrig ({rank['score'] * 100:.1f})",
                              wallet=swap.wallet)
        elif s.require_edge:
            edge = learn.wallet_edge(self.conn, swap.wallet)
            if edge["n"] < s.edge_min_n or edge["lower"] <= 0:
                return self._skip("whale", swap.mint,
                                  f"Vorteil nach Kosten nicht nachgewiesen ({edge['n']} Käufe, "
                                  f"Ø {edge['mean']:+.1%}, sicher nur {edge['lower']:+.1%}; nur mitgemessen)",
                                  wallet=swap.wallet, edge=edge)
            size *= edge["size_factor"]  # conviction: more size for a clearer edge, never above 1.5x
        if s.max_copies_per_coin and self._coin_copies(swap.mint) >= s.max_copies_per_coin:
            return self._skip("whale", swap.mint, f"schon {s.max_copies_per_coin}× in diesem Coin kopiert (6 h)",
                              wallet=swap.wallet)

        snap = market.snapshot(swap.mint, s)
        if not snap or snap.price_sol <= 0:
            return self._skip("whale", swap.mint, "kein Marktpreis")
        if snap.liquidity_usd < p.min_liquidity_usd:
            return self._skip("whale", swap.mint, f"Liquidität zu klein (${snap.liquidity_usd:,.0f})", snap)
        if snap.fdv and snap.fdv > p.max_mcap_usd:
            return self._skip("whale", swap.mint, f"Market Cap zu groß (${snap.fdv:,.0f})", snap)
        if snap.age_hours > s.max_token_age_h:
            return self._skip("whale", swap.mint, f"Coin zu alt ({snap.age_hours:.0f}h)", snap)
        run = snap.price_sol / swap.price_sol - 1 if swap.price_sol else 0
        if run > s.max_price_run_since_whale:
            return self._skip("whale", swap.mint, f"Preis schon +{run:.0%} seit Wal-Kauf", snap)
        avg_m5 = snap.buys_h1 / 12 if snap.buys_h1 else 0
        if avg_m5 and snap.buys_m5 > 3 * avg_m5 and snap.change_m5 > 30:
            return self._skip("whale", swap.mint, "Copy-Trader sind schon drin", snap)
        safe, why = self._safety("whale", swap.mint)
        if why:
            return self._skip("whale", swap.mint, why, snap, wallet=swap.wallet, safety=safe)
        wash = wash_share(self.conn, swap.mint)
        if wash is not None and wash >= s.max_wash_share:
            return self._skip("whale", swap.mint, f"Wash-Trading: {wash:.0%} des Volumens hin und her", snap,
                              wallet=swap.wallet)

        tp = p.take_profit
        if p.learned_tp and rank and rank["take_profit"]:
            tp = rank["take_profit"]
        if s.exit_mode == "target":
            tp = p.take_profit
        elif s.exit_mode == "inflow_peak":
            tp = s.tp_max
        hold = rank["max_hold_min"] if (p.learned_tp and rank and rank["max_hold_min"]) else p.max_hold_min
        winfo = store.wallet_info(self.conn, swap.wallet)
        tier = winfo.get("tier") or "none"
        ent = store.entity(self.conn, swap.wallet)
        if ent != swap.wallet and self._cluster_copied(ent, swap.mint):
            return self._skip("whale", swap.mint, "gleicher Wallet-Cluster schon kopiert", snap, wallet=swap.wallet)
        ibuckets, iraw = learn.intel_context(self.conn, swap.mint, swap.wallet, time.time())
        feats = {**learn.wallet_features(self.conn, swap.wallet), **ibuckets, "size": learn.bucket_size(swap.sol_amount),
                 "mcap": learn.bucket_mcap(snap.fdv),
                 "liq": learn.bucket_liq(snap.liquidity_usd), "explore": explore, "safety": safe.get("level") or "-"}
        why = learn.blocked(self.conn, "whale", feats)
        if why:
            return self._skip("whale", swap.mint, f"gelernter Filter: {why} verliert", snap, wallet=swap.wallet)
        exits = self._learned_exits(lp, p, tp, hold, inflow=1)
        info = {"explore": explore, "whale_sol": swap.sol_amount, "delay_s": round(age, 1),
                "wallet_score": round(rank["score"] * 100, 2) if rank else None,
                "wallet_hit_rate": rank["hit_rate"] if rank else None, "model_tier": tier,
                "model_score": winfo.get("model_score"), "cluster": ent if ent != swap.wallet else None,
                "exit_capture": winfo.get("exit_capture"), "capture": feats["capture"],
                "param_version": exits["version"], "safety": safe, "edge": edge, "intel": iraw,
                "thesis": learn.thesis(iraw)}
        reason = (f"Wal {swap.wallet[:6]}… kaufte {swap.sol_amount:.1f} SOL"
                  + (f", Wal-Score {info['wallet_score']}" if rank else ", unbewertet")
                  + (f", Vorteil Ø {edge['mean']:+.1%} aus {edge['n']} Käufen, Größe ×{edge['size_factor']:.2f}"
                     if edge else "")
                  + (f"; {info['thesis']}" if info["thesis"] else ""))
        if m == "manual":
            control.alert(self.conn, "whale", swap.mint, snap.symbol, "signal", reason, wallet=swap.wallet,
                          price_sol=snap.price_sol, price_usd=snap.price_usd, mcap_usd=snap.fdv,
                          score=info["wallet_score"] or 0, data={**info, "take_profit": exits["tp"]})
            return None
        block = self._risk_block("whale", p, size, swap.mint)
        if block:
            return self._skip("whale", swap.mint, block, snap)
        return self.open("whale", m, swap.mint, size, snap, p, reason=reason, wallet=swap.wallet,
                         whale=swap, meta=info, score=info["wallet_score"] or 0, **exits)

    def _coin_copies(self, mint: str) -> int:
        """Whale-mode positions in this coin within COIN_WINDOW_S, any wallet."""
        return self.conn.execute("SELECT COUNT(*) FROM ct_positions WHERE mint=? AND strategy='whale' AND opened_at>?",
                                 (mint, int(time.time()) - COIN_WINDOW_S)).fetchone()[0]

    def _cluster_copied(self, ent: str, mint: str, window_s: int = 6 * 3600) -> bool:
        """Another wallet of the same cluster was already copied on this coin recently (count the entity once)."""
        for r in self.conn.execute("SELECT wallet FROM ct_positions WHERE mint=? AND strategy='whale' AND opened_at>?",
                                   (mint, int(time.time()) - window_s)).fetchall():
            if r["wallet"] and store.entity(self.conn, r["wallet"]) == ent:
                return True
        return False

    def process_pending(self) -> int:
        """Enter whale buys whose learned delay has passed (unless the whale already sold)."""
        now, due, n = time.time(), [], 0
        for item in self._pending:
            (due if item[0] <= now else []).append(item)
        self._pending = [x for x in self._pending if x[0] > now]
        for _, sw in due:
            sold = self.conn.execute("SELECT 1 FROM ct_swaps WHERE wallet=? AND mint=? AND side='sell' AND ts>=?",
                                     (sw.wallet, sw.mint, sw.block_time)).fetchone()
            if sold:
                self._skip("whale", sw.mint, "Wal hat während der Wartezeit verkauft", wallet=sw.wallet)
                continue
            n += bool(self.on_whale_buy(sw, deferred=True))
        return n

    def _learned_exits(self, lp: dict | None, p: control.Preset, tp: float, hold: float, inflow: int) -> dict:
        """Exit parameters for a new position: learned version (capped by the preset) or the preset."""
        if not lp:
            return {"tp": tp, "hold": hold, "sl": p.stop_loss, "trail": (p.trail_activate, p.trail_drawdown),
                    "inflow": inflow, "version": None}
        return {"tp": lp["take_profit"], "hold": min(lp["max_hold_min"], p.max_hold_min),
                "sl": min(lp["stop_loss"], p.stop_loss), "trail": (lp["trail_activate"], lp["trail_drawdown"]),
                "inflow": int(lp.get("inflow_exit", inflow)), "version": lp.get("version")}

    # ------------------------------------------------------------------ mode 2: smart-money signals
    def on_signal(self, sig) -> int | None:
        """sig: a row of core's `signals` table."""
        m = control.mode(self.conn, "signal")
        if m == "off":
            return None
        p = control.preset(self.conn, "signal")
        mint = sig["mint"]
        age = time.time() - sig["ts"]
        if age > p.max_signal_age_s:
            return self._skip("signal", mint, f"Signal zu alt ({age:.0f}s)")
        model = model_view(sig, self.conn)
        if model:  # trained research model: its score is P(pump within 1 h) x 100
            score = model["score"]
            if score < p.min_model_score:
                return self._skip("signal", mint, f"Modell {score:.0f} (Note {model.get('grade')}) < {p.min_model_score:.0f}")
        else:  # model not trained yet: the signal engine's own score
            if p.require_model:
                return self._skip("signal", mint, "Modell noch nicht trainiert")
            score = sig["score"]
            if score < p.min_score:
                return self._skip("signal", mint, f"Signal-Score {score:.0f} < {p.min_score:.0f}")
        if (sig["smart_buyers"] or 0) < p.min_smart_buyers:
            return self._skip("signal", mint, f"nur {sig['smart_buyers']} Smart Wallets")
        feats = learn.signal_features(sig)
        if model:
            feats["grade"] = model.get("grade") or "-"
        why = learn.blocked(self.conn, "signal", feats)
        if why:
            return self._skip("signal", mint, f"gelernter Filter: {why} verliert")
        snap = market.snapshot(mint, self.s)
        if not snap or snap.price_sol <= 0:
            return self._skip("signal", mint, "kein Marktpreis")
        if snap.liquidity_usd < p.min_liquidity_usd:
            return self._skip("signal", mint, f"Liquidität zu klein (${snap.liquidity_usd:,.0f})", snap)
        if snap.fdv and snap.fdv > p.max_mcap_usd:
            return self._skip("signal", mint, f"Market Cap zu groß (${snap.fdv:,.0f})", snap)
        if sig["price_usd"] and snap.price_usd / sig["price_usd"] - 1 > 0.5:
            return self._skip("signal", mint, "seit dem Signal schon +50 %", snap)
        safe, why = self._safety("signal", mint)
        if why:
            return self._skip("signal", mint, why, snap, safety=safe)
        try:
            wallets = [w.get("wallet") for w in json.loads(sig["wallets_json"] or "[]")]
        except (ValueError, TypeError, AttributeError):
            wallets = []
        exits = self._learned_exits(learn.params_for(self.conn, "signal"), p, p.take_profit, p.max_hold_min, inflow=0)
        info = {"signal_id": sig["id"], "signal_score": sig["score"], "smart_buyers": sig["smart_buyers"],
                "net_usd": sig["net_usd"], "avg_quality": sig["avg_quality"], "wallets": wallets[:20],
                "model": model, "signal_age_s": round(age, 1), "param_version": exits["version"], "safety": safe}
        reason = (f"Signal-Score {sig['score']:.0f}, {sig['smart_buyers']} Smart Wallets, "
                  f"${(sig['net_usd'] or 0):,.0f} netto" + (f", Modell {score:.0f}" if model else ""))
        if m == "manual":
            control.alert(self.conn, "signal", mint, snap.symbol, "signal", reason, price_sol=snap.price_sol,
                          price_usd=snap.price_usd, mcap_usd=snap.fdv, score=score, data=info)
            return None
        size = p.size_sol
        if model and model.get("size_factor"):
            size *= max(0.25, min(2.0, float(model["size_factor"])))
        block = self._risk_block("signal", p, size, mint)
        if block:
            return self._skip("signal", mint, block, snap)
        return self.open("signal", m, mint, size, snap, p, reason=reason, meta=info, signal_id=sig["id"],
                         score=score, **exits)

    # ------------------------------------------------------------------ mode 3: launches of the sniper
    def on_launch(self, row) -> int | None:
        """row: a sniper_launches row with decision='enter'. Exits follow the sniper's settings (SNIPER_*):
        stop = SNIPER_STOP_PCT, trailing from SNIPER_TP1_PCT with SNIPER_TRAIL_PCT, SNIPER_MAX_HOLD_MIN.
        The sniper's partial sale at TP1 and its insider-sell exit stay in sniper/ (paper)."""
        m = control.mode(self.conn, "launch")
        if m == "off":
            return None
        mint = row["mint"]
        entered = row["entered_ts"] if "entered_ts" in row.keys() and row["entered_ts"] else row["decided_ts"]
        age = time.time() - (entered or 0)
        if age > LAUNCH_MAX_AGE_S:
            return self._skip("launch", mint, f"Launch-Entscheidung zu alt ({age:.0f}s)")
        p = control.preset(self.conn, "launch")
        snap = market.snapshot(mint, self.s)
        if not snap or snap.price_sol <= 0:
            return self._skip("launch", mint, "kein Marktpreis")
        dec_mcap = row["mcap_decide_usd"] if "mcap_decide_usd" in row.keys() else None
        if dec_mcap and snap.fdv and snap.fdv / dec_mcap - 1 > LAUNCH_MAX_RUN:
            return self._skip("launch", mint, f"seit der Sniper-Entscheidung schon +{snap.fdv / dec_mcap - 1:.0%}", snap)
        safe, why = self._safety("launch", mint)
        if why:
            return self._skip("launch", mint, why, snap, safety=safe)
        ex = sniper_exits()
        info = {"launch_score": row["score"], "launch_risk": row["risk"], "launch_source": row["source"],
                "decided_ts": row["decided_ts"], "sniper_exits": ex, "safety": safe}
        reason = f"Sniper-Einstieg, Score {row['score'] or 0:.0f}, Risiko {row['risk'] or 0:.0f}"
        if m == "manual":
            control.alert(self.conn, "launch", mint, snap.symbol, "signal", reason, price_sol=snap.price_sol,
                          price_usd=snap.price_usd, mcap_usd=snap.fdv, score=row["score"] or 0, data=info)
            return None
        block = self._risk_block("launch", p, p.size_sol, mint)
        if block:
            return self._skip("launch", mint, block, snap)
        return self.open("launch", m, mint, p.size_sol, snap, p, reason=reason, meta=info, score=row["score"] or 0,
                         tp=ex["take_profit"], hold=min(ex["max_hold_min"], p.max_hold_min), sl=ex["stop_loss"],
                         trail=(ex["trail_activate"], ex["trail_drawdown"]), inflow=0)

    # ------------------------------------------------------------------ open / close
    def open(self, strategy: str, managed: str, mint: str, size: float, snap, p: control.Preset, *, tp: float,
             hold: float, reason: str, wallet: str = "", whale: Swap | None = None, meta: dict | None = None,
             signal_id: int | None = None, score: float = 0, sl: float | None = None,
             trail: tuple[float, float] | None = None, inflow: int | None = None,
             version: int | None = None) -> int | None:
        sl = p.stop_loss if sl is None else min(sl, p.stop_loss)  # never wider than your preset
        trail = trail or (p.trail_activate, p.trail_drawdown)
        inflow = (1 if strategy == "whale" else 0) if inflow is None else inflow
        ex = self.executor(strategy) if strategy in control.STRATEGIES else self.exs.get("paper")
        fill = ex.buy(mint, size, snap)
        if not fill.ok:
            store.event(self.conn, "error", f"Kauf {snap.symbol} fehlgeschlagen: {fill.error}", "error")
            return None
        ex.adjust_balance(self.conn, -fill.sol)
        safe = (meta or {}).get("safety") or {}
        meta = {**(meta or {}), "fill_source": fill.source, "impact": fill.price_impact,
                "liq_usd": snap.liquidity_usd, "fdv": snap.fdv, "dex": snap.dex_id}
        if ex.mode == "paper":
            from .execution import paper_adverse

            meta["adverse"] = paper_adverse()  # lets learn.extra_cost compare fills made under different settings
        cur = self.conn.execute(
            """INSERT INTO ct_positions(mode, strategy, managed, wallet, mint, symbol, whale_sig, whale_ts,
                 whale_price_sol, signal_id, opened_at, entry_price_sol, entry_price_usd, entry_mcap_usd, size_sol,
                 tokens, token_decimals, entry_cost_sol, take_profit, stop_loss, trail_activate, trail_drawdown,
                 max_hold_min, high_price_sol, last_price_sol, last_buys_m5, peak_buys_m5, entry_tx, meta,
                 param_version, use_inflow_exit, safety_level, safety_score, safety_reasons)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (ex.mode, strategy, managed, wallet, mint, snap.symbol, whale.signature if whale else None,
             whale.block_time if whale else None, whale.price_sol if whale else None, signal_id, int(time.time()),
             fill.price_sol, snap.price_usd, snap.fdv, size, fill.tokens, _decimals_cache.get(mint, 6), fill.sol,
             tp, sl, trail[0], trail[1], hold, snap.price_sol, snap.price_sol,
             snap.buys_m5, snap.buys_m5, fill.tx, json.dumps(meta), version, inflow,
             safe.get("level"), safe.get("score"), json.dumps(safe.get("reasons") or [], ensure_ascii=False)),
        )
        self.conn.commit()
        pid = cur.lastrowid
        control.alert(self.conn, strategy, mint, snap.symbol, "bought", reason, wallet=wallet,
                      price_sol=fill.price_sol, price_usd=snap.price_usd, mcap_usd=snap.fdv, score=score,
                      data=meta, position_id=pid)
        store.event(self.conn, "buy",
                    f"KAUF [{strategy}/{ex.mode}] {snap.symbol} {fill.sol:.3f} SOL bei MC ${snap.fdv:,.0f}: {reason}"
                    f" (Ziel +{tp:.0%}, {'du verkaufst' if managed == 'semi' else 'Bot verkauft'})",
                    data={"position": pid, **meta})
        return pid

    def close(self, p, snap, reason: str) -> bool:
        # claim it first: the whale-sell path and the price loop (other threads) may try to close the same position
        if self.conn.execute("UPDATE ct_positions SET status='closing', closed_at=? WHERE id=? AND status='open'",
                             (int(time.time()), p["id"])).rowcount != 1:
            return False
        self.conn.commit()
        ex = self._ex_for_position(p)
        try:
            fill = ex.sell(p["mint"], p["tokens"], p["token_decimals"] or 6, snap, p["last_price_sol"] or 0)
        except Exception as e:
            fill = None
            err = str(e)
        if fill is None or not fill.ok:
            self.conn.execute("UPDATE ct_positions SET status='open', closed_at=NULL WHERE id=? AND status='closing'",
                              (p["id"],))
            self.conn.commit()
            store.event(self.conn, "error", f"Verkauf {p['symbol']} fehlgeschlagen: {fill.error if fill else err}",
                        "error")
            return False
        ex.adjust_balance(self.conn, fill.sol)
        pnl = fill.sol - p["entry_cost_sol"]
        pct = pnl / p["entry_cost_sol"] if p["entry_cost_sol"] else 0
        self.conn.execute(
            """UPDATE ct_positions SET status='closed', closed_at=?, exit_price_sol=?, proceeds_sol=?,
                 pnl_sol=?, pnl_pct=?, exit_reason=?, exit_tx=?, exit_mcap_usd=? WHERE id=?""",
            (int(time.time()), fill.price_sol, fill.sol, pnl, pct, reason, fill.tx, snap.fdv if snap else None,
             p["id"]),
        )
        self.conn.commit()
        store.event(self.conn, "sell", f"VERKAUF [{p['strategy']}/{p['mode']}] {p['symbol']} {pct:+.1%} "
                                       f"({pnl:+.3f} SOL), Grund: {EXIT_NAMES.get(reason, reason)}",
                    data={"position": p["id"], "reason": reason})
        return True

    # ------------------------------------------------------------------ exits
    def on_whale_sell(self, swap: Swap) -> None:
        if not self.s.exit_on_whale_sell:
            return
        for p in self.open_positions("whale"):
            if p["mint"] == swap.mint and p["wallet"] == swap.wallet:
                self._exit(p, market.snapshot(p["mint"], self.s), "whale")

    def _exit(self, p, snap, reason: str) -> None:
        """Close in auto mode; in semi mode only flag the hint (you sell)."""
        if p["managed"] == "semi":
            if p["exit_hint"] != reason:
                self.conn.execute("UPDATE ct_positions SET exit_hint=?, exit_hint_at=? WHERE id=?",
                                  (reason, int(time.time()), p["id"]))
                self.conn.commit()
                store.event(self.conn, "hint", f"VERKAUFS-HINWEIS {p['symbol']}: {EXIT_NAMES.get(reason, reason)}",
                            data={"position": p["id"], "reason": reason})
            return
        self.close(p, snap, reason)

    def _smart_exit(self, p) -> bool:
        try:
            wallets = json.loads(p["meta"] or "{}").get("wallets") or []
        except ValueError:
            return False
        if not wallets:
            return False
        q = ",".join("?" * len(wallets))
        try:
            r = self.conn.execute(
                f"""SELECT SUM(CASE WHEN side='buy' THEN token_amount ELSE 0 END),
                           SUM(CASE WHEN side='sell' AND ts>=? THEN token_amount ELSE 0 END)
                    FROM trades WHERE mint=? AND wallet IN ({q})""",
                (p["opened_at"], p["mint"], *wallets),
            ).fetchone()
        except Exception:
            return False
        bought, sold = (r[0] or 0), (r[1] or 0)
        return bought > 0 and sold >= 0.5 * bought

    def update(self) -> None:
        pos = self.open_positions()
        if not pos:
            return
        snaps, failed = market.snapshots_checked([p["mint"] for p in pos], self.s)
        now = time.time()
        for p in pos:
            snap = snaps.get(p["mint"])
            if p["mint"] in failed:
                continue  # DexScreener down: no news about this coin, so no 'no market' count either
            if not snap or snap.price_sol <= 0:
                self._missing[p["id"]] = self._missing.get(p["id"], 0) + 1
                if self._missing[p["id"]] >= 12:
                    self._exit(p, None, "no_market")
                continue
            self._missing.pop(p["id"], None)
            price = snap.price_sol
            high = max(p["high_price_sol"] or price, price)
            peak_buys = max(p["peak_buys_m5"] or 0, snap.buys_m5)
            self.conn.execute(
                """UPDATE ct_positions SET high_price_sol=?, last_price_sol=?, last_price_usd=?, last_mcap_usd=?,
                     last_buys_m5=?, peak_buys_m5=? WHERE id=?""",
                (high, price, snap.price_usd, snap.fdv, snap.buys_m5, peak_buys, p["id"]),
            )
            entry = p["entry_price_sol"]
            gain, high_gain = price / entry - 1, high / entry - 1
            trail_act = p["trail_activate"] if p["trail_activate"] is not None else self.s.trail_activate
            trail_dd = p["trail_drawdown"] if p["trail_drawdown"] is not None else self.s.trail_drawdown
            inflow = p["use_inflow_exit"] if p["use_inflow_exit"] is not None else p["strategy"] == "whale"
            reason = None
            if gain <= -p["stop_loss"]:
                reason = "stop"
            elif gain >= p["take_profit"]:
                reason = "target"
            elif (inflow and gain > self.s.tp_min / 2 and peak_buys >= 20
                  and snap.buys_m5 < 0.6 * peak_buys):
                reason = "inflow_peak"
            elif trail_act > 0 and high_gain >= trail_act and price <= high * (1 - trail_dd):
                reason = "trail"
            elif p["strategy"] == "signal" and gain > 0 and self._smart_exit(p):
                reason = "smart_exit"
            elif (now - p["opened_at"]) / 60 >= p["max_hold_min"]:
                reason = "time"
            if reason:
                self._exit(p, snap, reason)
        self.conn.commit()

    # ------------------------------------------------------------------ commands from dashboard / terminal
    def process_commands(self) -> int:
        n = 0
        for c in control.pending_commands(self.conn):
            n += 1
            try:
                if c["action"] == "sell":
                    p = self.conn.execute("SELECT * FROM ct_positions WHERE id=? AND status='open'",
                                          (c["position_id"],)).fetchone()
                    if not p:
                        control.finish_command(self.conn, c["id"], "failed", "Position nicht offen")
                        continue
                    ok = self.close(p, market.snapshot(p["mint"], self.s), "manual")
                    control.finish_command(self.conn, c["id"], "done" if ok else "failed")
                elif c["action"] == "buy":
                    snap = market.snapshot(c["mint"], self.s)
                    if not snap or snap.price_sol <= 0:
                        control.finish_command(self.conn, c["id"], "failed", "kein Marktpreis")
                        continue
                    strat = c["strategy"] if c["strategy"] in control.STRATEGIES else "signal"
                    p = control.preset(self.conn, strat)
                    size = c["size_sol"] or p.size_sol
                    safe, block = self._safety(strat, c["mint"])
                    block = block or self._risk_block(strat, p, size, c["mint"])
                    if block:
                        control.finish_command(self.conn, c["id"], "failed", block)
                        continue
                    pid = self.open(strat, "semi", c["mint"], size, snap, p, tp=p.take_profit,
                                    hold=p.max_hold_min, reason="von dir gekauft",
                                    meta={"manual_buy": True, "safety": safe})
                    control.finish_command(self.conn, c["id"], "done" if pid else "failed", str(pid or ""))
            except Exception as e:  # never let one command kill the loop
                control.finish_command(self.conn, c["id"], "failed", str(e)[:200])
        return n

    def equity_snapshot(self) -> None:
        now = int(time.time())
        for mode, ex in self.exs.items():
            if ex is None:
                continue
            if mode == "live" and not self.conn.execute(
                    "SELECT 1 FROM ct_positions WHERE mode='live' LIMIT 1").fetchone():
                continue
            bal = ex.balance(self.conn)
            open_val = sum((p["last_price_sol"] or 0) * p["tokens"] for p in self.open_positions(mode=mode))
            self.conn.execute("INSERT OR REPLACE INTO ct_equity VALUES (?,?,?,?,?)",
                              (now, mode, bal, open_val, bal + open_val))
        self.conn.commit()


# ---------------------------------------------------------------------- sniper exits for mode 3
def sniper_exits() -> dict:
    """The sniper's exit settings as copy-trader fractions (defaults if sniper/ is missing)."""
    try:
        from sniper.config import load as sniper_settings  # type: ignore

        c = sniper_settings()
        tp1, trail, stop, hold = c.tp1_pct, c.trail_pct, c.stop_pct, c.max_hold_min
    except Exception:
        tp1, trail, stop, hold = 60.0, 25.0, 30.0, 60.0
    # no partial sale here: the trailing stop arms at TP1, the hard target sits far above it
    return {"take_profit": 3 * tp1 / 100, "trail_activate": tp1 / 100, "trail_drawdown": trail / 100,
            "stop_loss": stop / 100, "max_hold_min": hold}


# ---------------------------------------------------------------------- wash trading (safety/ check)
LAUNCH_BOT_S, LAUNCH_BOT_SHARE, LAUNCH_BOT_MIN_N = 3, 0.5, 3  # same rule as model.tiers.uncopyable
_lb_cache: dict[str, tuple[float, tuple[int, int]]] = {}
_CREATED_Q = ("SELECT pair_created_at FROM tokens WHERE mint=?", "SELECT pair_created_at FROM safety_tokens WHERE mint=?",
              "SELECT created_at FROM research_tokens WHERE mint=?", "SELECT created_ts FROM sniper_launches WHERE mint=?")


def _created(conn, mint: str) -> float | None:
    """Earliest known pool / coin creation time (unix s) from any package that recorded it."""
    best = None
    for q in _CREATED_Q:
        try:
            r = conn.execute(q, (mint,)).fetchone()
        except Exception:
            continue
        t = r[0] if r else None
        if t:
            t = t / 1000 if t > 1e11 else t  # DexScreener gives milliseconds
            best = t if best is None else min(best, t)
    return best


def launch_bot_share(conn, wallet: str) -> tuple[int, int]:
    """(buys with a known coin creation time, of those within LAUNCH_BOT_S of it). Cached 30 min.
    10/2026: one 'ultra' wallet bought 20 of 20 coins in the creation second (dev or launch bot); every other
    tracked wallet was at 17 % or less."""
    hit = _lb_cache.get(wallet)
    if hit and hit[0] > time.time() - 1800:
        return hit[1]
    n = k = 0
    try:
        for mint, ts in conn.execute("SELECT mint, MIN(ts) FROM ct_swaps WHERE wallet=? AND side='buy' "
                                     "GROUP BY mint ORDER BY MIN(ts) DESC LIMIT 60", (wallet,)).fetchall():
            c = _created(conn, mint)
            if c:
                n += 1
                k += ts - c <= LAUNCH_BOT_S
    except Exception:
        pass
    _lb_cache[wallet] = (time.time(), (n, k))
    return n, k


def wash_share(conn, mint: str) -> float | None:
    """Share of recent volume that the same wallets bought AND sold (safety/ check "wash"), None = unknown."""
    try:
        import safety  # type: ignore

        a = safety.get(conn, mint)
    except Exception:
        return None
    if not a:
        return None
    try:
        v = ((a.checks or {}).get("wash") or {}).get("data", {}).get("roundtrip_share")
        return float(v) if v is not None else None
    except (AttributeError, TypeError, ValueError):
        return None


# ---------------------------------------------------------------------- research model hook
GRADE_SIZE = {"A": 1.5, "B": 1.2, "C": 1.0, "D": 0.6, "E": 0.4}


def model_view(sig, conn=None) -> dict | None:
    """Prediction of the wallet-research model (model.score_token, see model/README.md) for a signal.

    Returns {"score": 0-100 (= P(pump 1h) x 100), "grade", "p_pump_1h", "size_factor", "tiers"} or None
    when the model is not installed or fails; then the signal engine's score is used.
    """
    try:
        from model import score_token  # type: ignore
    except Exception:
        return None
    try:
        out = score_token(sig["mint"], conn=conn)
    except Exception as e:  # includes model.predict.ModelNotTrained before the first retrain
        log.debug("model prediction unavailable: %s", e)
        return None
    if not out or out.get("score") is None or not out.get("version"):
        return None  # untrained model: fall back to the signal score
    grade = out.get("grade")
    return {"score": float(out["score"]), "grade": grade, "version": out.get("version"),
            "p_pump_1h": ((out.get("horizons") or {}).get("1h") or {}).get("p_pump"),
            "size_factor": GRADE_SIZE.get(grade, 1.0), "tiers": out.get("tiers")}
