"""Statistics for the dashboard and the CLI (pandas DataFrames)."""
from __future__ import annotations

import statistics
import time

import pandas as pd

from .config import Settings
from .ranking import ROUNDTRIP_COST


def positions(conn, mode: str | None = None, strategy: str | None = None) -> pd.DataFrame:
    """All copy/signal positions. mode: paper|live, strategy: whale|signal|manual (None = all)."""
    where, args = [], []
    if mode:
        where.append("mode=?")
        args.append(mode)
    if strategy:
        where.append("strategy=?")
        args.append(strategy)
    q = "SELECT * FROM ct_positions" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY opened_at"
    df = pd.read_sql_query(q, conn, params=args)
    for c in ("opened_at", "closed_at", "whale_ts"):
        if c in df:
            df[c + "_dt"] = pd.to_datetime(df[c], unit="s", utc=True)
    if not df.empty:
        df["hold_min"] = (df["closed_at"].fillna(time.time()) - df["opened_at"]) / 60
        df["entry_delay_s"] = df["opened_at"] - df["whale_ts"]
        df["current_pct"] = df["last_price_sol"] / df["entry_price_sol"] - 1
    return df


def summary(df: pd.DataFrame) -> dict:
    closed = df[df["status"] == "closed"] if not df.empty else df
    if closed.empty:
        return {"trades": 0, "open": int((df["status"] == "open").sum()) if not df.empty else 0}
    wins = closed[closed["pnl_sol"] > 0]
    losses = closed[closed["pnl_sol"] <= 0]
    gross_win = wins["pnl_sol"].sum()
    gross_loss = -losses["pnl_sol"].sum()
    eq = closed.sort_values("closed_at")["pnl_sol"].cumsum()
    dd = (eq - eq.cummax().clip(lower=0)).min()  # the peak starts at 0: losses from the first trade on count
    return {
        "trades": len(closed),
        "open": int((df["status"] == "open").sum()),
        "win_rate": len(wins) / len(closed),
        "pnl_sol": closed["pnl_sol"].sum(),
        "avg_pnl_pct": closed["pnl_pct"].mean(),
        "avg_win_pct": wins["pnl_pct"].mean() if len(wins) else 0.0,
        "avg_loss_pct": losses["pnl_pct"].mean() if len(losses) else 0.0,
        "profit_factor": gross_win / gross_loss if gross_loss > 0 else float("inf"),
        "max_drawdown_sol": float(dd) if pd.notna(dd) else 0.0,
        "avg_hold_min": closed["hold_min"].mean(),
        "avg_entry_delay_s": closed["entry_delay_s"].mean(),
        "best_pct": closed["pnl_pct"].max(),
        "worst_pct": closed["pnl_pct"].min(),
    }


def by_strategy(conn, mode: str | None = None) -> pd.DataFrame:
    """One row per strategy (whale / signal / launch / manual) with summary() columns."""
    from .control import STRATEGIES

    df = positions(conn, mode)
    rows = []
    for strat in (*STRATEGIES, "manual"):
        sub = df[df["strategy"] == strat] if len(df) else df
        rows.append({"strategy": strat, **summary(sub)})
    return pd.DataFrame(rows)


def alerts(conn, since_ts: int = 0, strategy: str | None = None, limit: int = 500) -> pd.DataFrame:
    """Entries the bot bought or (manual mode) only signalled: ct_alerts."""
    q = "SELECT * FROM ct_alerts WHERE ts>=?" + (" AND strategy=?" if strategy else "") + " ORDER BY ts DESC LIMIT ?"
    args = (since_ts, strategy, limit) if strategy else (since_ts, limit)
    df = pd.read_sql_query(q, conn, params=args)
    df["dt"] = pd.to_datetime(df["ts"], unit="s", utc=True)
    return df


def coin_history(conn, mint: str) -> dict:
    """Everything copytrade knows about one coin: our positions, alerts, whale swaps."""
    return {
        "positions": pd.read_sql_query("SELECT * FROM ct_positions WHERE mint=? ORDER BY opened_at", conn, params=(mint,)),
        "alerts": pd.read_sql_query("SELECT * FROM ct_alerts WHERE mint=? ORDER BY ts", conn, params=(mint,)),
        "whale_swaps": pd.read_sql_query(
            """SELECT s.*, r.score AS wallet_score FROM ct_swaps s LEFT JOIN ct_wallet_rank r ON r.address=s.wallet
               WHERE s.mint=? ORDER BY s.ts""", conn, params=(mint,)),
    }


def exit_reasons(df: pd.DataFrame) -> pd.DataFrame:
    closed = df[df["status"] == "closed"] if len(df) else df
    if closed.empty:
        return pd.DataFrame(columns=["exit_reason", "trades", "avg_pct", "pnl_sol"])
    return closed.groupby("exit_reason").agg(trades=("id", "count"), avg_pct=("pnl_pct", "mean"),
                                             pnl_sol=("pnl_sol", "sum")).reset_index()


def signal_backtest(conn, preset, size_sol: float | None = None) -> pd.DataFrame:
    """What mode 2 with this preset would have made on past signals (uses the signal engine's
    measured ret_1h / max_ret_1h; an estimate: target if the 1 h high reached it, else the 1 h
    result capped at the stop)."""
    try:
        df = pd.read_sql_query(
            "SELECT * FROM signals WHERE max_ret_1h IS NOT NULL AND COALESCE(kind, 'v1')='v1' AND score>=? "
            "AND smart_buyers>=? ORDER BY ts",
            conn, params=(preset.min_score, preset.min_smart_buyers))
    except Exception:
        return pd.DataFrame()
    if df.empty:
        return df
    size = size_sol or preset.size_sol
    sl = preset.stop_loss
    dead = df["outcome_flag"].eq("dead") if "outcome_flag" in df else pd.Series(False, index=df.index)
    # stopped out first when the dip before the 1 h high went through the stop (no target booked on that path)
    stopped = df["dd_before_peak_1h"].le(-sl) if "dd_before_peak_1h" in df else pd.Series(False, index=df.index)
    hit = (df["max_ret_1h"] >= preset.take_profit) & ~stopped & ~dead
    df = df[hit | stopped | dead | df["ret_1h"].notna()].copy()  # a signal without a 1 h price is not a 0 % trade
    hit, stopped, dead = hit[df.index], stopped[df.index], dead[df.index]
    rest = df["ret_1h"].fillna(-1.0).clip(lower=-sl)
    df["gain"] = (hit * preset.take_profit + (~hit & stopped & ~dead) * -sl
                  + dead * df["ret_1h"].fillna(-1.0).clip(lower=-1.0)  # a dead coin is not saved by the stop
                  + (~hit & ~stopped & ~dead) * rest) - ROUNDTRIP_COST
    df["pnl_sol"] = df["gain"] * size
    df["equity_sol"] = df["pnl_sol"].cumsum()
    df["dt"] = pd.to_datetime(df["ts"], unit="s", utc=True)
    return df


def equity(conn, mode: str) -> pd.DataFrame:
    df = pd.read_sql_query("SELECT * FROM ct_equity WHERE mode=? ORDER BY ts", conn, params=(mode,))
    df["dt"] = pd.to_datetime(df["ts"], unit="s", utc=True)
    return df


def wallet_table(conn, mode: str = "paper") -> pd.DataFrame:
    """Whale ranking + tier/model_score/cluster_id/exit_capture from core's `wallets` (synced from the model).
    `entity` = cluster_id or address: wallets of one cluster are one whale (count once)."""
    try:
        conn.execute("SELECT tier, model_score, cluster_id, exit_capture, tier_source FROM wallets LIMIT 0")
        extra = ("cw.tier, cw.tier_source, cw.model_score, cw.cluster_id, cw.exit_capture",
                 "LEFT JOIN wallets cw ON cw.address=w.address")
    except Exception:
        extra = ("NULL AS tier, NULL AS tier_source, NULL AS model_score, NULL AS cluster_id, NULL AS exit_capture", "")
    df = pd.read_sql_query(
        f"""SELECT w.address, w.label, w.source, w.active, {extra[0]}, r.samples, r.hit_rate, r.median_first_pump,
                  r.median_pump_min, r.median_lead_min, r.median_achievable, r.avg_loss, r.expected_value,
                  r.score, r.take_profit, r.max_hold_min,
                  (SELECT COUNT(*) FROM ct_positions p WHERE p.wallet=w.address AND p.status='closed'
                     AND p.mode=:mode) AS copied,
                  (SELECT SUM(pnl_sol) FROM ct_positions p WHERE p.wallet=w.address AND p.status='closed'
                     AND p.mode=:mode) AS copy_pnl_sol,
                  (SELECT MAX(ts) FROM ct_swaps s WHERE s.wallet=w.address) AS last_swap
           FROM ct_wallets w LEFT JOIN ct_wallet_rank r ON r.address=w.address {extra[1]}
           ORDER BY COALESCE(r.score, -1) DESC""",
        conn, params={"mode": mode},
    )
    df["last_swap_dt"] = pd.to_datetime(df["last_swap"], unit="s", utc=True)
    df["entity"] = [f"cluster:{c}" if c not in (None, "") and c == c else a for a, c in zip(df.address, df.cluster_id)]
    return df


def outcomes(conn) -> pd.DataFrame:
    df = pd.read_sql_query("SELECT * FROM ct_buy_outcomes WHERE pumped IS NOT NULL ORDER BY ts", conn)
    df["dt"] = pd.to_datetime(df["ts"], unit="s", utc=True)
    return df


def events(conn, limit: int = 200) -> pd.DataFrame:
    df = pd.read_sql_query("SELECT * FROM ct_events ORDER BY ts DESC LIMIT ?", conn, params=(limit,))
    df["dt"] = pd.to_datetime(df["ts"], unit="s", utc=True)
    return df


def backtest(conn, s: Settings, size_sol: float | None = None) -> pd.DataFrame:
    """Walk-forward replay of evaluated whale buys.

    A buy is copied only if that wallet's ranking built from its EARLIER buys
    qualified (no look-ahead). Exit: the wallet's learned take-profit if the
    first pump (seen from our later entry) reaches it, else the trailing/stop/
    time result. An estimate from minute candles, not tick data.
    """
    size = size_sol or s.position_size_sol
    df = outcomes(conn)
    if df.empty:
        return df
    hist: dict[str, list] = {}
    rows = []
    known_after = s.pump_window_min * 60  # an outcome is only known once its pump window is over
    for r in df.itertuples():
        prev = [p for p in hist.setdefault(r.wallet, []) if p.ts + known_after <= r.ts]
        take = False
        tp = s.take_profit
        if len(prev) >= s.min_wallet_samples:
            n = len(prev)
            ach = [max(-s.stop_loss - 0.05, min(p.achievable_gain, s.tp_max)) for p in prev]
            score = (sum(ach) / n) * n / (n + 5)
            take = score >= s.min_wallet_score
            pumps = [p.first_pump_gain for p in prev if p.pumped]
            if pumps:
                tp = min(s.tp_max, max(s.tp_min, s.tp_fraction_of_median_pump * statistics.median(pumps)))
        if take and r.our_entry_price_usd and r.entry_price_usd:
            our_first = (1 + r.first_pump_gain) * r.entry_price_usd / r.our_entry_price_usd - 1
            # a buy that stopped out may have hit the stop before the pump: book the stop (pessimistic)
            stopped = r.achievable_gain <= -s.stop_loss - ROUNDTRIP_COST + 1e-9
            gain = tp - ROUNDTRIP_COST if our_first >= tp and not stopped else r.achievable_gain
            rows.append({"ts": r.ts, "dt": r.dt, "wallet": r.wallet, "mint": r.mint, "tp": tp,
                         "gain": gain, "pnl_sol": size * gain})
        hist[r.wallet].append(r)
    bt = pd.DataFrame(rows)
    if not bt.empty:
        bt["equity_sol"] = bt["pnl_sol"].cumsum()
    return bt


def detection(conn, hours: float = 24, side: str | None = "buy") -> pd.DataFrame:
    """How late we see whale swaps: one row per recognised swap with lag_s = recognised - block time and the
    source (ws = websocket, poll = polling, gap = polling found what the websocket missed)."""
    q = "SELECT * FROM ct_detect WHERE detected_at >= ?" + (" AND side=?" if side else "") + " ORDER BY detected_at"
    args: list = [time.time() - hours * 3600] + ([side] if side else [])
    try:
        df = pd.read_sql_query(q, conn, params=args)
    except Exception:
        return pd.DataFrame()
    if df.empty:
        return df
    df["lag_s"] = df["detected_at"] - df["block_ts"]
    df["notify_lag_s"] = df["notified_at"] - df["block_ts"]  # chain -> websocket message (ws only)
    df["dt"] = pd.to_datetime(df["detected_at"], unit="s", utc=True)
    return df


def detection_summary(conn, hours: float = 24) -> pd.DataFrame:
    """Per source: n, share, p50/p90/p99/max lag in seconds and the share seen within 10 s / 30 s."""
    df = detection(conn, hours)
    if df.empty:
        return df
    g = df.groupby("source")["lag_s"]
    out = g.agg(n="size", p50="median", p90=lambda x: x.quantile(0.9), p99=lambda x: x.quantile(0.99),
                max="max").reset_index()
    out["share"] = out["n"] / out["n"].sum()
    out["within_10s"] = g.apply(lambda x: (x <= 10).mean()).values
    out["within_30s"] = g.apply(lambda x: (x <= 30).mean()).values
    return out


LEDGER_EVERY_S = 3600
LEDGER_TOLERANCE_SOL = 1e-6


def ledger(conn, s: Settings) -> dict:
    """Paper balances against their ledger: start - cost of every bought position + proceeds of every sold one.
    A difference is money booked twice or lost between a balance change and its position (10/2026 audit)."""
    from . import store

    out = {}
    for mode, key, start in (("paper", "paper_balance_sol", s.paper_start_balance_sol),
                             ("explore", "explore_balance_sol", s.explore_start_balance_sol)):
        bal = store.get_state(conn, key)
        if bal is None:
            continue
        cost, proceeds, n = conn.execute(
            "SELECT COALESCE(SUM(entry_cost_sol), 0), COALESCE(SUM(CASE WHEN status='closed' THEN proceeds_sol END), 0),"
            " COUNT(*) FROM ct_positions WHERE mode=? AND status IN ('open', 'closing', 'closed')", (mode,)).fetchone()
        expected = start - cost + proceeds
        out[mode] = {"balance": float(bal), "expected": expected, "diff": float(bal) - expected, "positions": n}
    return out


def _median(xs):
    return statistics.median(xs) if xs else None


def costs(conn, days: float = 7) -> dict:
    """Measured trading costs per account and source (strategy) on closed positions: booked fill against the market
    price at that moment. entry = fill / mid at the buy - 1, exit = 1 - fill / mid at the sale (the last polled price
    for sales booked before exit_mid_sol was stored), lag = our fill / the whale's price - 1 (whale copies)."""
    import json

    rows = conn.execute(
        "SELECT mode, strategy, entry_price_sol, exit_price_sol, last_price_sol, whale_price_sol, meta FROM ct_positions "
        "WHERE status='closed' AND closed_at > ? AND entry_price_sol > 0", (int(time.time() - days * 86400),)).fetchall()
    acc: dict = {}
    for r in rows:
        try:
            m = json.loads(r["meta"] or "{}")
        except (TypeError, ValueError):
            m = {}
        d = acc.setdefault(f"{r['mode']}/{r['strategy']}", {"entry": [], "exit": [], "lag": [], "n": 0})
        d["n"] += 1
        if m.get("mid_sol"):
            d["entry"].append(r["entry_price_sol"] / m["mid_sol"] - 1)
        mid = m.get("exit_mid_sol") if "exit_mid_sol" in m else r["last_price_sol"]
        if mid and r["exit_price_sol"] and m.get("exit_source") != "no_route":  # a 0 fill is a rug, not a cost
            d["exit"].append(1 - r["exit_price_sol"] / mid)
        if r["whale_price_sol"]:
            d["lag"].append(r["entry_price_sol"] / r["whale_price_sol"] - 1)
    return {k: {"n": d["n"], "entry": _median(d["entry"]), "n_entry": len(d["entry"]), "exit": _median(d["exit"]),
                "n_exit": len(d["exit"]), "lag": _median(d["lag"])} for k, d in sorted(acc.items())}


def bookkeeping_line(conn, s: Settings) -> str:
    """One log line: ledger check per paper account and the measured costs per source."""
    def pct(x):
        return "?" if x is None else f"{x:+.1%}"

    parts = []
    for mode, d in ledger(conn, s).items():
        ok = abs(d["diff"]) <= LEDGER_TOLERANCE_SOL
        parts.append(f"{mode} balance {d['balance']:.4f} = start - costs + proceeds {d['expected']:.4f}"
                     + (" ok" if ok else f" DIFF {d['diff']:+.4f} SOL") + f" ({d['positions']} positions)")
    for k, d in costs(conn).items():
        parts.append(f"cost {k}: n {d['n']}, entry {pct(d['entry'])} (n {d['n_entry']}), exit {pct(d['exit'])}"
                     f" (n {d['n_exit']}), whale lag {pct(d['lag'])}")
    return "bookkeeping: " + "; ".join(parts or ["no paper account yet"])
