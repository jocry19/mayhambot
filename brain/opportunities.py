"""One opportunity table for every source (plan "Richtung neu", 18.10.).

Sniper, copytrade, signals, the market watch and the wallet model each report every coin they could have bought,
taken or not, with its features. The source is a feature, never a gate. The brain predicts a distribution of the
net result for each row, decides by drawing from it, and writes back its prediction, its decision and, once the
candles are in, the honest result after costs (a rug is -100 %).

Writers import only `report` (stdlib only, creates the table if it is missing). The schema is in
plan/gelegenheiten_schema.md; columns are only ever added, never renamed or removed.
"""
from __future__ import annotations

import json
import math
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS opportunities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL, ref TEXT NOT NULL, mint TEXT NOT NULL, symbol TEXT,
    ts INTEGER NOT NULL, src_ts INTEGER, reported_at INTEGER,
    price_sol REAL, price_usd REAL, mcap_usd REAL, liq_usd REAL,
    features TEXT, source_score REAL, source_action TEXT, source_reason TEXT, source_result REAL,
    source_horizon_min REAL,
    model TEXT, pred_mean REAL, pred_sd REAL, pred_q10 REAL, draw REAL,
    decision TEXT, account TEXT, size_sol REAL, decided_at INTEGER, position_id INTEGER,
    evaluated_at INTEGER, exit_plan TEXT, outcome_net REAL, outcome_gross REAL, cost REAL, rug INTEGER,
    outcomes TEXT, supplemented_at INTEGER,
    UNIQUE (source, ref)
);
CREATE INDEX IF NOT EXISTS idx_opportunities_ts ON opportunities(ts);
CREATE INDEX IF NOT EXISTS idx_opportunities_mint ON opportunities(mint, ts);
CREATE INDEX IF NOT EXISTS idx_opportunities_eval ON opportunities(evaluated_at, ts);
"""

SOURCES = ("sniper", "copytrade", "signals", "signals_v2", "market", "wallet_model", "whale", "launch", "strategy")
# shared feature names: every source that knows one of these uses exactly this name and unit, so the model learns
# them across sources; anything else goes under "<source>.<name>"
SHARED = {
    "mc_usd": "market cap in USD at ts",
    "liq_usd": "pool liquidity in USD (curve coins: curve depth)",
    "buyers": "distinct buyers so far",
    "buys_5m": "buy transactions in the last 5 min",
    "trades_5m": "all transactions in the last 5 min",
    "vol_5m_usd": "volume in the last 5 min, USD",
    "pc_5m": "price change over 5 min, percent",
    "pc_1h": "price change over 1 h, percent",
    "top10_pct": "share of supply held by the top 10 holders, percent",
    "curve_pct": "bonding curve progress, percent (pump.fun / LaunchLab)",
    "bundle": "bought in a bundle at launch (bool)",
    "dev_pct": "share the creator holds, percent",
    "delay_s": "seconds between the on-chain event and ts",
    "age_s": "seconds since the pair or the launch",
    "wallet_score": "score of the wallet that triggered it, 0..100",
    "wallet_tier": "ultra / smart / good / none",
    "wallets_n": "smart wallets in the coin",
    "inflow_usd": "net smart inflow, USD",
    "inflow_sol": "net smart inflow, SOL",
    "dex": "pumpfun / pumpswap / raydium / meteora / launchlab / ...",
    "risk": "rug-check risk 0..100",
    "staged": "confirmed staged pump pattern (bool)",
}


def ensure(conn) -> None:
    # statement by statement: executescript would commit the caller's open transaction
    for stmt in SCHEMA.split(";"):
        if stmt.strip():
            conn.execute(stmt)


def _clean(features: dict | None) -> dict:
    out = {}
    for k, v in (features or {}).items():
        if v is None:
            continue
        if isinstance(v, float) and not math.isfinite(v):
            continue
        if isinstance(v, (bool, int, float, str)):
            out[str(k)] = v
    return out


def report(conn, source: str, ref: str, mint: str, ts: float, features: dict | None = None, *, symbol: str = "",
           src_ts: float | None = None, price_sol: float | None = None, price_usd: float | None = None,
           mcap_usd: float | None = None, liq_usd: float | None = None, source_score: float | None = None,
           source_action: str | None = None, source_reason: str | None = None) -> bool:
    """Report one opportunity. ts = the moment a trader could have bought (unix s); ref = the source's own id, unique
    per source (a repeat report is ignored). features: flat {name: number | str | bool}, raw values (the brain
    buckets them itself), shared names from SHARED, everything else as "<source>.<name>". Returns True if new."""
    ensure(conn)
    cur = conn.execute(
        "INSERT OR IGNORE INTO opportunities(source, ref, mint, symbol, ts, src_ts, reported_at, price_sol, price_usd, "
        "mcap_usd, liq_usd, features, source_score, source_action, source_reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (source, str(ref), mint, symbol or "", int(ts), int(src_ts) if src_ts else None, int(time.time()),
         price_sol, price_usd, mcap_usd, liq_usd, json.dumps(_clean(features)), source_score, source_action, source_reason))
    return cur.rowcount > 0


def _supplement_column(conn) -> None:
    if "supplemented_at" not in {r[1] for r in conn.execute("PRAGMA table_info(opportunities)")}:
        try:
            conn.execute("ALTER TABLE opportunities ADD COLUMN supplemented_at INTEGER")
        except Exception:  # another process added it a moment ago
            pass


def supplement(conn, source: str, ref: str, features: dict | None, ts: float | None = None) -> bool:
    """Later features for an opportunity already reported (same source and ref), e.g. a deep check that finishes
    minutes after the first look: merged into its features (same naming rules as report), supplemented_at = ts (unix
    s, default now). The brain decides such a row once more with what it knows then; labels and replays keep the
    first report's moment. Returns True if the row exists."""
    ensure(conn)
    _supplement_column(conn)
    row = conn.execute("SELECT id, features FROM opportunities WHERE source=? AND ref=?", (source, str(ref))).fetchone()
    if row is None:
        return False
    try:
        merged = json.loads(row[1] or "{}")
    except (TypeError, ValueError):
        merged = {}
    merged.update(_clean(features))
    conn.execute("UPDATE opportunities SET features=?, supplemented_at=? WHERE id=?",
                 (json.dumps(merged), int(ts or time.time()), row[0]))
    return True


def source_result(conn, source: str, ref: str, net: float, horizon_min: float | None = None) -> None:
    """Optional: a source that traded or followed the opportunity itself writes its own net result after costs
    (fraction, rug -1.0) and the horizon it measured over."""
    ensure(conn)
    conn.execute("UPDATE opportunities SET source_result=?, source_horizon_min=? WHERE source=? AND ref=?",
                 (net, horizon_min, source, str(ref)))
