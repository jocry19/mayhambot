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
    dd = (eq - eq.cummax()).min()
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
    """One row per strategy (whale / signal / manual) with summary() columns."""
    df = positions(conn, mode)
    rows = []
    for strat in ("whale", "signal", "manual"):
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
            "SELECT * FROM signals WHERE max_ret_1h IS NOT NULL AND score>=? AND smart_buyers>=? ORDER BY ts",
            conn, params=(preset.min_score, preset.min_smart_buyers))
    except Exception:
        return pd.DataFrame()
    if df.empty:
        return df
    size = size_sol or preset.size_sol
    hit = df["max_ret_1h"] >= preset.take_profit
    df["gain"] = (hit * preset.take_profit + (~hit) * df["ret_1h"].fillna(0).clip(lower=-preset.stop_loss)) - ROUNDTRIP_COST
    df["pnl_sol"] = df["gain"] * size
    df["equity_sol"] = df["pnl_sol"].cumsum()
    df["dt"] = pd.to_datetime(df["ts"], unit="s", utc=True)
    return df


def equity(conn, mode: str) -> pd.DataFrame:
    df = pd.read_sql_query("SELECT * FROM ct_equity WHERE mode=? ORDER BY ts", conn, params=(mode,))
    df["dt"] = pd.to_datetime(df["ts"], unit="s", utc=True)
    return df


def wallet_table(conn) -> pd.DataFrame:
    df = pd.read_sql_query(
        """SELECT w.address, w.label, w.source, w.active, r.samples, r.hit_rate, r.median_first_pump,
                  r.median_pump_min, r.median_lead_min, r.median_achievable, r.avg_loss, r.expected_value,
                  r.score, r.take_profit, r.max_hold_min,
                  (SELECT COUNT(*) FROM ct_positions p WHERE p.wallet=w.address AND p.status='closed') AS copied,
                  (SELECT SUM(pnl_sol) FROM ct_positions p WHERE p.wallet=w.address AND p.status='closed') AS copy_pnl_sol,
                  (SELECT MAX(ts) FROM ct_swaps s WHERE s.wallet=w.address) AS last_swap
           FROM ct_wallets w LEFT JOIN ct_wallet_rank r ON r.address=w.address
           ORDER BY COALESCE(r.score, -1) DESC""",
        conn,
    )
    df["last_swap_dt"] = pd.to_datetime(df["last_swap"], unit="s", utc=True)
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
    for r in df.itertuples():
        prev = hist.setdefault(r.wallet, [])
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
            gain = tp - ROUNDTRIP_COST if our_first >= tp else r.achievable_gain
            rows.append({"ts": r.ts, "dt": r.dt, "wallet": r.wallet, "mint": r.mint, "tp": tp,
                         "gain": gain, "pnl_sol": size * gain})
        prev.append(r)
    bt = pd.DataFrame(rows)
    if not bt.empty:
        bt["equity_sol"] = bt["pnl_sol"].cumsum()
    return bt
