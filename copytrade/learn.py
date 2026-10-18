"""Self-learning strategy layer.

1. Evaluate: every closed trade, every evaluated whale buy and every past smart-money signal is
   replayed on 1-minute candles against the whole parameter grid (copytrade.sim): entry delay,
   target, stop, trailing stop, hold time, sell-at-inflow-peak. One row per entry in ct_evals.
2. Tune: per mode (whale / signal) and per whale with enough history, pick the variant with the
   best net result on the OLDER 70 % of entries and accept it only if it also beats the current
   settings on the NEWER 30 % (out of sample). Accepted parameters become a new version in
   ct_param_versions; the trading loop uses the active version.
3. Learned filters: feature buckets (model tier, model grade, market cap, liquidity, signal score,
   whale size) that lost money in both the train and the test part are blocked.

Hard limits that learning never touches: position size, max open positions, daily loss limit,
live switch. The learned stop loss is never wider than your preset's stop, the learned hold time
never longer than your preset's max hold. Turn learning off per mode with control
`autotune:<strategy>` = 0.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Sequence

from . import control, market, sim
from .config import Settings

log = logging.getLogger("copytrade.learn")

SCHEMA = """
CREATE TABLE IF NOT EXISTS ct_candles (
    mint TEXT, ts INTEGER, o REAL, h REAL, l REAL, c REAL, v REAL, PRIMARY KEY (mint, ts)
);
CREATE TABLE IF NOT EXISTS ct_evals (
    kind TEXT, ref TEXT, strategy TEXT, wallet TEXT, mint TEXT, ts INTEGER, grid_version INTEGER,
    gains TEXT, actual_gain REAL, sim_actual REAL, features TEXT, created_at INTEGER,
    PRIMARY KEY (kind, ref)
);
CREATE INDEX IF NOT EXISTS idx_ct_evals_strategy ON ct_evals(strategy, ts);
CREATE INDEX IF NOT EXISTS idx_ct_evals_wallet ON ct_evals(wallet, ts);
CREATE TABLE IF NOT EXISTS ct_param_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, strategy TEXT, scope TEXT, params TEXT,
    n_train INTEGER, n_test INTEGER, train_gain REAL, test_gain REAL, baseline_test_gain REAL,
    accepted INTEGER, active INTEGER DEFAULT 0, notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_ct_param_versions ON ct_param_versions(strategy, scope, active);
"""

HORIZON_S = (max(sim.HOLDS_MIN) + 10) * 60 + max(sim.DELAYS_S)  # an entry is evaluable after this
MIN_MODE_N, MIN_MODE_TEST = 40, 12
MIN_WALLET_N, MIN_WALLET_TEST = 20, 6
MIN_IMPROVEMENT = 0.01  # +1 %-point net per trade out of sample
TRAIN_SHARE = 0.7
# no floor above a total loss: a -0.6 floor turned every rug into -60 % and lifted wallet edges (10/2026 audit);
# the cap keeps one moonshot from carrying a whole wallet
GAIN_CLIP = (-1.0, 2.0)
RETRY_S = 2 * 86400  # entries without candles are retried this long, then recorded as unevaluable
TRADE_WEIGHT = 2  # our own trades count double vs. hypothetical whale buys / signals
HALF_LIFE_H = 72.0  # older entries count less: weight halves every 3 days (markets and whales change)
ROLLBACK_MIN_TRADES = 10  # own trades under a learned version before it can be rolled back
ROLLBACK_MARGIN = 0.05  # roll back when real trades are this far below the version's test result and negative
EDGE_SIZE_UNIT = 0.03  # size factor = lower edge bound / this, clamped to 0.5..1.5
BOOTSTRAP_RUNS = 500  # Monte-Carlo resamples of the test part before a variant is accepted
BOOTSTRAP_MIN_WIN = 0.8  # share of resamples in which the variant must be in the black and beat the current one
CALIB_MIN_TRADES = 10  # own trades needed before the paper/sim cost gap is applied
CALIB_MAX_EXTRA = 0.10
CALIB_DEFAULT_EXTRA = 0.015  # before CALIB_MIN_TRADES own trades: measured paper round trip ~4.5 % vs 3 % in the grid
CALIB_TRIM = 0.1  # never add more than 10 %-points of extra cost per round trip


def init(conn) -> None:
    conn.executescript(SCHEMA)


def enabled(conn, strategy: str) -> bool:
    return control.get(conn, f"autotune:{strategy}") != "0"


# ---------------------------------------------------------------------- candles
def candles(conn, s: Settings, mint: str, lo: int, hi: int, pool: str | None = None) -> list[tuple]:
    """1-minute candles: research's cache, then ours, then GeckoTerminal/Birdeye (stored in ct_candles)."""
    need = max(1, (hi - lo) // 60)
    for q in ("SELECT ts,o,h,l,c,v FROM research_candles WHERE mint=? AND tf_sec=60 AND ts BETWEEN ? AND ? ORDER BY ts",
              "SELECT ts,o,h,l,c,v FROM ct_candles WHERE mint=? AND ts BETWEEN ? AND ? ORDER BY ts"):
        try:
            rows = [tuple(r) for r in conn.execute(q, (mint, lo, hi)).fetchall()]
        except Exception:
            rows = []
        if len(rows) >= need * 0.4 and rows and rows[-1][0] >= hi - 30 * 60:
            return rows
    pool = pool or _pool(conn, mint)
    try:
        got = market.minute_candles(pool, lo, hi, s, mint=mint) if (pool or s.birdeye_api_key) else []
    except Exception as e:
        log.debug("candles %s failed: %s", mint[:6], e)
        return []
    if got:
        conn.executemany("INSERT OR REPLACE INTO ct_candles(mint, ts, o, h, l, c, v) VALUES (?,?,?,?,?,?,?)", [(mint, *c) for c in got])
    return got


def _pool(conn, mint: str) -> str:
    for q in ("SELECT pool FROM ct_buy_outcomes WHERE mint=? AND pool!='' LIMIT 1",
              "SELECT pool_address FROM tokens WHERE mint=? AND pool_address!=''"):
        try:
            r = conn.execute(q, (mint,)).fetchone()
            if r and r[0]:
                return r[0]
        except Exception:
            pass
    return market.best_pool(mint)


# ---------------------------------------------------------------------- features (for attribution + filters)
def bucket_mcap(x) -> str:
    if not x:
        return "?"
    return "<50k" if x < 5e4 else "50k-250k" if x < 2.5e5 else "250k-1M" if x < 1e6 else "1M-10M" if x < 1e7 else ">10M"


def bucket_liq(x) -> str:
    if not x:
        return "?"
    return "<20k" if x < 2e4 else "20k-50k" if x < 5e4 else "50k-200k" if x < 2e5 else ">200k"


def bucket_size(sol) -> str:
    sol = sol or 0
    return "<5 SOL" if sol < 5 else "5-20 SOL" if sol < 20 else ">20 SOL"


def signal_features(sig) -> dict:
    """Feature buckets of a core `signals` row (same buckets for learning and for live filtering)."""
    keys = sig.keys()
    buyers = sig["smart_buyers"] or 0
    return {"mcap": bucket_mcap(sig["mcap_usd"]), "score": f"{int((sig['score'] or 0) // 10 * 10)}+",
            "buyers": str(min(buyers, 6)) + ("+" if buyers >= 6 else ""),
            "ultra": "ja" if "n_ultra" in keys and (sig["n_ultra"] or 0) > 0 else "nein"}


def bucket_capture(x) -> str:
    """Model exit_capture (whale's sell price / peak since entry): does following the whale's sell pay?"""
    if x is None:
        return "?"
    return "<50%" if x < 0.5 else "50-80%" if x < 0.8 else ">80%"


def wallet_tier(conn, wallet: str) -> str:
    """Tier from core's `wallets` table (synced from the research model), see store.wallet_info."""
    from . import store

    return store.wallet_info(conn, wallet).get("tier") or "none" if wallet else "-"


def wallet_features(conn, wallet: str, ts: float | None = None) -> dict:
    """Tier and exit-capture bucket as known at ts (None = now). Replays of old buys must not see today's tier or
    a capture computed from later trades: they get the tier of the model version active then (model_tier_history)
    and '?' for capture, unless the buy is newer than the active model."""
    from . import store

    info = store.wallet_info(conn, wallet)
    now = {"tier": info.get("tier") or "none", "capture": bucket_capture(info.get("exit_capture"))}
    if ts is None:
        return now
    try:
        act = conn.execute("SELECT MAX(ts) FROM model_versions WHERE active=1").fetchone()[0]
        if act is not None and ts >= act:
            return now
        r = conn.execute("""SELECT h.tier FROM model_tier_history h JOIN model_versions v ON v.id=h.version
                            WHERE h.address=? AND v.id=(SELECT MAX(id) FROM model_versions WHERE ts<=?)""",
                         (wallet, int(ts))).fetchone()
        had = conn.execute("SELECT 1 FROM model_versions WHERE ts<=? LIMIT 1", (int(ts),)).fetchone()
    except Exception:
        return {"tier": "?", "capture": "?"}
    return {"tier": (r[0] or "none") if r else ("none" if had else "?"), "capture": "?"}


# intel/ (Zentrale): point-in-time features; the buckets below become learnable filters, the raw values go to
# the journal. Walk-forward on the first 410 whale buys (10/2026) no single intel feature helped after costs; on
# 106 coin-hours with intel snapshots (10/16-17) only px_ret_5m held in train and test (strategy.RUN5_HALF sizes on
# it). Nothing else here gates by itself: only a filter the tuner learns in train AND test does.
INTEL_KEYS = {"coin": ("vol_roundtrip_60m", "vol_entities_60m", "flow_smart_exit_share", "liq_chg_60m",
                       "exec_rt_0_25", "hold_top10", "px_ret_5m", "px_ret_15m", "mkt_phase"),
              "wallet": ("w_hold_median_min", "w_scalp_share", "w_concentration", "w_latency_median_s")}


def _b(x, edges, labels):
    if x is None:
        return "?"
    for e, lab in zip(edges, labels):
        if x < e:
            return lab
    return labels[-1]


def intel_context(conn, mint: str, wallet: str, ts) -> tuple[dict, dict]:
    """(bucket features for learning, raw values for the journal) from intel.features_at at ts; ({}, {}) if
    intel/ is missing or fails."""
    try:
        import intel  # type: ignore

        c = intel.features_at("coin", mint, int(ts), conn)["features"]
        w = intel.features_at("wallet", wallet, int(ts), conn)["features"] if wallet else {}
    except Exception as e:
        log.debug("intel features %s failed: %s", mint[:6], e)
        return {}, {}
    raw = {k: c.get(k) for k in INTEL_KEYS["coin"]} | {k: w.get(k) for k in INTEL_KEYS["wallet"]}
    buckets = {
        "wash": _b(raw["vol_roundtrip_60m"], (0.1, 0.3), ("<10%", "10-30%", ">30%")),
        "persons": _b(raw["vol_entities_60m"], (20, 80), ("<20", "20-80", ">80")),
        "liq60": _b(raw["liq_chg_60m"], (-0.2, 0), ("<-20%", "-20-0%", ">0%")),
        "scalper": _b(raw["w_scalp_share"], (0.3, 0.7), ("<30%", "30-70%", ">70%")),
        "focus": _b(raw["w_concentration"], (0.3, 0.6), ("<30%", "30-60%", ">60%")),
        "run5": _b(raw["px_ret_5m"], (0, 0.05), ("<0%", "0-5%", ">5%")),
    }
    return buckets, {k: (round(v, 4) if isinstance(v, float) else v) for k, v in raw.items()}


def thesis(raw: dict) -> str:
    """One readable line on the coin and the wallet, as a trader would note it in the journal."""
    parts = []
    def pct(k):
        v = raw.get(k)
        return None if v is None else f"{v:.0%}"
    if raw.get("vol_entities_60m") is not None:
        parts.append(f"{raw['vol_entities_60m']:.0f} Personen/h")
    if pct("vol_roundtrip_60m"):
        parts.append(f"Wash {pct('vol_roundtrip_60m')}")
    if pct("liq_chg_60m"):
        parts.append(f"Liquidität 1 h {raw['liq_chg_60m']:+.0%}")
    if raw.get("px_ret_5m") is not None:
        parts.append(f"Kurs 5 min {raw['px_ret_5m']:+.0%}")
    if pct("exec_rt_0_25"):
        parts.append(f"Kosten {pct('exec_rt_0_25')}")
    if raw.get("w_hold_median_min") is not None:
        parts.append(f"Wal hält im Mittel {raw['w_hold_median_min']:.0f} min")
    if pct("w_scalp_share"):
        parts.append(f"Scalps {pct('w_scalp_share')}")
    return ", ".join(parts)


# ---------------------------------------------------------------------- 1. evaluate
def _sells_after(conn, wallet: str, mint: str, ts: int) -> list[int]:
    if not wallet:
        return []
    return [r[0] for r in conn.execute(
        "SELECT ts FROM ct_swaps WHERE wallet=? AND mint=? AND side='sell' AND ts>? ORDER BY ts", (wallet, mint, ts))]


TRACKER_GAP_S = 45 * 60  # no swap seen for this long = tracker was offline (Mac asleep, outage), not a quiet market


def tracker_gaps(conn, t0: int, t1: int, min_gap_s: int = TRACKER_GAP_S) -> list[tuple[int, int]]:
    """Windows in [t0, t1] where the tracker recorded nothing (by seen_at). Whale sells there are unknown."""
    seen = [r[0] for r in conn.execute(
        "SELECT seen_at FROM ct_swaps WHERE seen_at BETWEEN ? AND ? ORDER BY seen_at", (t0, t1))]
    return [(a, b) for a, b in zip(seen, seen[1:]) if b - a > min_gap_s]  # only gaps the tracker came back from


def _sell_unknown(gaps, ts: int, sells: list[int]) -> bool:
    """True if the whale may have sold during an outage within the replay horizon: no sell is on record before the
    outage ended, so the replay would hold past an exit we never saw. A sell recorded earlier settles every variant."""
    first = sells[0] if sells else None
    return any(a < ts + HORIZON_S and b > ts and (first is None or first > b) for a, b in gaps)


def _store_eval(conn, kind, ref, strategy, wallet, mint, ts, gains, features, actual=None, sim_actual=None) -> None:
    conn.execute("INSERT OR REPLACE INTO ct_evals(kind, ref, strategy, wallet, mint, ts, grid_version, gains, actual_gain, "
                 "sim_actual, features, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 (kind, str(ref), strategy, wallet or "", mint, int(ts), sim.GRID_VERSION, json.dumps(gains),
                  actual, sim_actual, json.dumps(features), int(time.time())))


def evaluate_trades(conn, s: Settings, limit: int = 20) -> int:
    """Closed positions old enough to see every alternative's outcome."""
    rows = conn.execute(
        """SELECT p.* FROM ct_positions p WHERE p.status='closed' AND p.opened_at < ?
           AND NOT EXISTS (SELECT 1 FROM ct_evals e WHERE e.kind='trade' AND e.ref=CAST(p.id AS TEXT))
           ORDER BY p.opened_at LIMIT ?""", (int(time.time()) - HORIZON_S, limit)).fetchall()
    n = 0
    for p in rows:
        whale = p["strategy"] == "whale" and p["whale_ts"]
        t0 = p["whale_ts"] if whale else p["opened_at"] - sim.DELAYS_S[0]
        cs = candles(conn, s, p["mint"], int(t0) - 20 * 60, int(t0) + HORIZON_S)
        variants = sim.GRID if whale else sim.EXIT_GRID
        meta = json.loads(p["meta"] or "{}")
        feats = {"strategy": p["strategy"], "mcap": bucket_mcap(p["entry_mcap_usd"] or meta.get("fdv")),
                 "liq": bucket_liq(meta.get("liq_usd")), "tier": meta.get("model_tier") or "-",
                 "grade": (meta.get("model") or {}).get("grade") or "-", "explore": bool(meta.get("explore")),
                 "safety": (meta.get("safety") or {}).get("level") or "-",
                 "capture": meta.get("capture") or "-"}
        if not cs:
            if p["opened_at"] > time.time() - RETRY_S:
                continue
            _store_eval(conn, "trade", p["id"], p["strategy"], p["wallet"], p["mint"], t0, [], feats, p["pnl_pct"])
            continue
        events = _sells_after(conn, p["wallet"], p["mint"], int(t0)) if whale else []
        gains = sim.grid_gains(cs, t0, variants, events)
        actual_v = sim.Variant(int(max(0, p["opened_at"] - t0)) if whale else sim.DELAYS_S[0], p["take_profit"],
                               p["stop_loss"], p["trail_activate"] or 0, p["trail_drawdown"] or 0,
                               p["max_hold_min"], int(p["use_inflow_exit"] if "use_inflow_exit" in p.keys() and
                                                      p["use_inflow_exit"] is not None else whale))
        sa = sim.simulate(cs, t0, actual_v, events)
        _store_eval(conn, "trade", p["id"], p["strategy"], p["wallet"], p["mint"], t0, gains, feats,
                    p["pnl_pct"], sa[0] if sa else None)
        n += 1
    return n


def _pit_migrate(conn) -> None:
    """Once: replays stored before 10/2026 carried today's tier and capture; rewrite them as known at the time."""
    from . import store

    if store.get_state(conn, "evals_pit_v1"):
        return
    for ref, w, ts, f in conn.execute("SELECT ref, wallet, ts, features FROM ct_evals WHERE kind='whale_buy'").fetchall():
        try:
            feats = {**json.loads(f or "{}"), **wallet_features(conn, w, ts)}
        except ValueError:
            continue
        conn.execute("UPDATE ct_evals SET features=? WHERE kind='whale_buy' AND ref=?", (json.dumps(feats), ref))
    store.set_state(conn, "evals_pit_v1", "1")
    conn.commit()


def evaluate_whale_buys(conn, s: Settings, limit: int = 40) -> int:
    """Every whale buy already measured by the ranking (ct_buy_outcomes) -> full grid."""
    rows = conn.execute(
        """SELECT o.* FROM ct_buy_outcomes o WHERE o.pumped IS NOT NULL AND o.ts < ?
           AND NOT EXISTS (SELECT 1 FROM ct_evals e WHERE e.kind='whale_buy' AND e.ref=o.sig)
           ORDER BY o.ts DESC LIMIT ?""", (int(time.time()) - HORIZON_S, limit)).fetchall()
    _pit_migrate(conn)
    now = int(time.time())
    gaps = tracker_gaps(conn, now - 3 * 86400, now)
    for a, b in gaps:  # replays stored before the outage was known
        for e in conn.execute("SELECT ref, wallet, mint, ts FROM ct_evals WHERE kind='whale_buy' AND ts<? AND ts+?>? "
                              "AND gains!='[]'", (b, HORIZON_S, a)).fetchall():
            if _sell_unknown(gaps, e[3], _sells_after(conn, e[1], e[2], e[3])):
                conn.execute("UPDATE ct_evals SET gains='[]', features=? WHERE kind='whale_buy' AND ref=?",
                             (json.dumps({"gap": 1}), e[0]))
    n = 0
    for b in rows:
        w = b["wallet"]
        sells = _sells_after(conn, w, b["mint"], b["ts"])
        if _sell_unknown(gaps, b["ts"], sells):  # counts as no data rather than a too-long hold
            _store_eval(conn, "whale_buy", b["sig"], "whale", w, b["mint"], b["ts"], [], {"gap": 1})
            n += 1
            continue
        cs = candles(conn, s, b["mint"], b["ts"] - 20 * 60, b["ts"] + HORIZON_S, b["pool"])
        if not cs and b["ts"] > time.time() - RETRY_S:
            continue  # candles not available right now: try again next round
        feats = {"strategy": "whale", **wallet_features(conn, w, b["ts"]), "size": bucket_size(b["sol_amount"]),
                 **intel_context(conn, b["mint"], w, b["ts"])[0]}
        gains = sim.grid_gains(cs, b["ts"], sim.GRID, sells) if cs else []
        _store_eval(conn, "whale_buy", b["sig"], "whale", w, b["mint"], b["ts"], gains, feats)
        n += 1
    return n


def evaluate_signals(conn, s: Settings, limit: int = 30) -> int:
    """Past smart-money signals of the signal engine -> exit grid (entry after our latency)."""
    try:
        rows = conn.execute(
            """SELECT g.* FROM signals g WHERE g.ts < ? AND COALESCE(g.kind, 'v1')='v1'
               AND NOT EXISTS (SELECT 1 FROM ct_evals e WHERE e.kind='signal' AND e.ref=CAST(g.id AS TEXT))
               ORDER BY g.ts DESC LIMIT ?""", (int(time.time()) - HORIZON_S, limit)).fetchall()
    except Exception:
        return 0
    n = 0
    for g in rows:
        cs = candles(conn, s, g["mint"], g["ts"] - 20 * 60, g["ts"] + HORIZON_S)
        feats = {"strategy": "signal", **signal_features(g)}
        if not cs and g["ts"] > time.time() - RETRY_S:
            continue
        gains = sim.grid_gains(cs, g["ts"], sim.EXIT_GRID) if cs else []
        _store_eval(conn, "signal", g["id"], "signal", "", g["mint"], g["ts"], gains, feats)
        n += 1
    return n


# ---------------------------------------------------------------------- 2. tune
def _rows(conn, strategy: str, wallet: str | list[str] | None = None,
          min_signal_score: float | None = None) -> list[dict]:
    q = "SELECT * FROM ct_evals WHERE strategy=? AND grid_version=? AND gains!='[]'"
    args: list = [strategy, sim.GRID_VERSION]
    if wallet:
        ws = [wallet] if isinstance(wallet, str) else list(wallet)
        q += f" AND wallet IN ({','.join('?' * len(ws))})"
        args += ws
    out = []
    for r in conn.execute(q + " ORDER BY ts", args).fetchall():
        f = json.loads(r["features"] or "{}")
        if r["kind"] == "signal" and min_signal_score is not None:
            try:
                if int(f.get("score", "0+")[:-1]) + 10 <= min_signal_score:
                    continue
            except ValueError:
                pass
        out.append({"kind": r["kind"], "ts": r["ts"], "gains": json.loads(r["gains"]), "features": f,
                    "w": (TRADE_WEIGHT if r["kind"] == "trade" else 1) * decay(r["ts"]), "actual": r["actual_gain"],
                    "sim_actual": r["sim_actual"], "key": (r["wallet"], r["mint"], int(r["ts"]))})
    # one row per whale buy: our trade row (it carries the real fill) replaces the whale_buy replay of the same buy,
    # and a buy copied by both paper accounts counts once (10/2026 audit: the same buy counted about 3x)
    traded = {o["key"] for o in out if o["kind"] == "trade" and o["key"][0]}
    seen: set = set()
    keep = []
    for o in out:
        k = o.pop("key")
        if k[0] and o["kind"] in ("whale_buy", "trade"):
            if (o["kind"] == "whale_buy" and k in traded) or (o["kind"] == "trade" and k in seen):
                continue
            seen.add(k)
        keep.append(o)
    return keep


def _means(rows: Sequence[dict], n_var: int, extra_cost: float = 0.0) -> list[float]:
    # per variant over the rows it could simulate: a missing simulation (no candles after a long delay) counted as a
    # 0 % trade, so when the average was negative the variants with the most gaps looked best
    tot, wsum = [0.0] * n_var, [0.0] * n_var
    for r in rows:
        g = r["gains"]
        if len(g) != n_var:
            continue
        for i, x in enumerate(g):
            if x is not None:
                tot[i] += r["w"] * max(GAIN_CLIP[0], min(GAIN_CLIP[1], x))
                wsum[i] += r["w"]
    return [t / (ws or 1) - extra_cost for t, ws in zip(tot, wsum)]


def extra_cost(conn, strategy: str) -> float:
    """Cost the 1-minute simulation misses, measured on our own closed trades: trimmed mean(simulated - actual).

    The grid assumes sim.ROUNDTRIP_COST (3 %); real paper fills (Jupiter quote, adverse slippage both ways,
    priority fees) cost more (10/2026: 6.8 % for a buy sold one second later). CALIB_DEFAULT_EXTRA until
    CALIB_MIN_TRADES.
    Paper fills made with another adverse slippage than today's (see landing_drift) are put on today's footing.
    """
    from .execution import paper_adverse

    now_adv = paper_adverse()
    try:
        rows = conn.execute("SELECT e.sim_actual - e.actual_gain, p.meta FROM ct_evals e LEFT JOIN ct_positions p "
                            "ON e.kind='trade' AND p.id=CAST(e.ref AS INTEGER) WHERE e.kind='trade' AND e.strategy=? "
                            "AND e.actual_gain IS NOT NULL AND e.sim_actual IS NOT NULL", (strategy,)).fetchall()
    except Exception:
        return 0.0

    def used(meta):
        try:
            return float(json.loads(meta or "{}").get("adverse", LAND_MAX))
        except (TypeError, ValueError):
            return LAND_MAX

    gaps = sorted(r[0] - 2 * (used(r[1]) - now_adv) for r in rows)
    if len(gaps) < CALIB_MIN_TRADES:
        return CALIB_DEFAULT_EXTRA
    k = int(len(gaps) * CALIB_TRIM)  # trimmed mean: a few rugs move it (a median would not), single freaks do not
    core = gaps[k:len(gaps) - k] or gaps
    return max(0.0, min(CALIB_MAX_EXTRA, sum(core) / len(core)))


def decay(ts, now: float | None = None, half_life_h: float = HALF_LIFE_H) -> float:
    """Weight of an entry from ts: 1 now, 0.5 after half_life_h, 0.25 after twice that."""
    age_h = max(0.0, ((now or time.time()) - (ts or 0)) / 3600)
    return 0.5 ** (age_h / half_life_h) if half_life_h > 0 else 1.0


def wallet_edge(conn, wallet: str) -> dict:
    """What copying this whale (or its cluster) with our current exits made on its past buys, after costs.

    Uses every replayed buy in ct_evals (whale buys and our own trades), the variant closest to the
    parameters the bot would use now, the calibrated extra cost and time decay (HALF_LIFE_H). Only the past
    is used, so gating on it is a walk-forward decision. `lower` = mean minus one standard error: a whale
    counts as proven only when even that is in the black, so a few lucky buys are not enough.
    -> {"n", "n_eff", "mean", "lower", "size_factor", "variant", "extra_cost"}"""
    from . import store

    ws = [wallet]
    ent = store.entity(conn, wallet)
    if ent.startswith("cluster:"):
        try:
            ws = [r[0] for r in conn.execute("SELECT address FROM wallets WHERE cluster_id=?", (ent[8:],))] or ws
        except Exception:
            pass
    variants = sim.GRID
    idx = sim.nearest(current_params(conn, "whale", wallet), variants)
    q = ",".join("?" * len(ws))
    vals, wts = [], []
    now = time.time()
    seen: set = set()
    # one row per whale buy: a copied buy has its whale_buy replay plus a 'trade' row per account that copied it, all
    # starting at the whale's time with the same gains, and counting them 2-4 times shrank the error bar too much
    for r in conn.execute(f"SELECT gains, ts, wallet, mint FROM ct_evals WHERE strategy='whale' AND grid_version=? "
                          f"AND gains!='[]' AND kind IN ('whale_buy','trade') AND wallet IN ({q}) "
                          f"ORDER BY kind='trade'", (sim.GRID_VERSION, *ws)):
        key = (r[2], r[3], int(r[1]))
        if key in seen:
            continue
        seen.add(key)
        g = json.loads(r[0])
        if len(g) == len(variants) and g[idx] is not None:
            vals.append(max(GAIN_CLIP[0], min(GAIN_CLIP[1], g[idx])))
            wts.append(decay(r[1], now))
    cost = extra_cost(conn, "whale")
    out = {"n": len(vals), "n_eff": 0.0, "mean": 0.0, "lower": 0.0, "size_factor": 0.0,
           "variant": variants[idx].key(), "extra_cost": cost}
    sw = sum(wts)
    if not vals or sw <= 0:
        return out
    mean = sum(w * v for w, v in zip(wts, vals)) / sw
    var = sum(w * (v - mean) ** 2 for w, v in zip(wts, vals)) / sw
    n_eff = sw * sw / sum(w * w for w in wts)
    lower = mean - cost - (var / n_eff) ** 0.5 if n_eff > 1 else mean - cost - abs(mean)
    out.update(n_eff=round(n_eff, 1), mean=mean - cost, lower=lower,
               size_factor=max(0.5, min(1.5, lower / EDGE_SIZE_UNIT)) if lower > 0 else 0.0)
    return out


THOMPSON_PRIOR_N = 6.0  # the population's average counts like this many own buys of a wallet (until measured)
THOMPSON_SD_MIN = 0.05
# the prior weight is measured instead (empirical Bayes): k = within-wallet variance / between-wallet variance of the
# true edge, from wallets with this many replays. 10/18 backup: 66 wallets, within sd 0.169, between sd 0.05 -> k 11.5
# (split-half persistence of wallet means 0.11): the fixed 6 drew unknown wallets about twice as often as the data
# supports. Walk-forward on 1179 replays (4 splits): the wallet effect alone ranks the newer part slightly better than
# the population (rho +0.02..+0.13), adding the stored context buckets made it worse (rho -0.03..-0.14)
THOMPSON_K_MIN_WALLET_N, THOMPSON_K_MIN_WALLETS = 5, 20
THOMPSON_K_RANGE = (3.0, 50.0)
_POP_CACHE: dict = {}
POP_CACHE_S = 600


def population_edge(conn) -> dict:
    """Mean and spread of what copying any tracked whale made after costs (same replays and variant logic as
    wallet_edge, all wallets together, time-decayed): the prior every single wallet starts from. Cached 10 min."""
    hit = _POP_CACHE.get(id(conn))
    if hit and time.time() - hit[0] < POP_CACHE_S:
        return hit[1]
    variants = sim.GRID
    idx = sim.nearest(current_params(conn, "whale", None), variants)
    vals, wts, now, seen, by_wallet = [], [], time.time(), set(), {}
    for r in conn.execute("SELECT gains, ts, wallet, mint FROM ct_evals WHERE strategy='whale' AND grid_version=? "
                          "AND gains!='[]' AND kind IN ('whale_buy','trade') ORDER BY kind='trade'",
                          (sim.GRID_VERSION,)):
        key = (r[2], r[3], int(r[1]))
        if key in seen:
            continue
        seen.add(key)
        g = json.loads(r[0])
        if len(g) == len(variants) and g[idx] is not None:
            vals.append(max(GAIN_CLIP[0], min(GAIN_CLIP[1], g[idx])))
            wts.append(decay(r[1], now))
            by_wallet.setdefault(r[2], []).append(vals[-1])
    cost = extra_cost(conn, "whale")
    sw = sum(wts)
    if not vals or sw <= 0:
        out = {"n": 0, "mean": -cost, "sd": 0.3}  # nothing measured yet: costs only, a wide spread
    else:
        mean = sum(w * v for w, v in zip(wts, vals)) / sw
        var = sum(w * (v - mean) ** 2 for w, v in zip(wts, vals)) / sw
        out = {"n": len(vals), "mean": mean - cost, "sd": max(THOMPSON_SD_MIN, var ** 0.5)}
        out.update(_wallet_spread(by_wallet))
    _POP_CACHE[id(conn)] = (time.time(), out)
    return out


def _wallet_spread(by_wallet: dict) -> dict:
    """Between- and within-wallet spread of replayed copy gains (method of moments over wallets with
    THOMPSON_K_MIN_WALLET_N+ replays) -> {"k", "sd_within", "sd_between", "k_wallets"}, or {} with too few wallets."""
    groups = [v for v in by_wallet.values() if len(v) >= THOMPSON_K_MIN_WALLET_N]
    if len(groups) < THOMPSON_K_MIN_WALLETS:
        return {}

    def var(v, ddof=0):
        m = sum(v) / len(v)
        return sum((x - m) ** 2 for x in v) / (len(v) - ddof)

    within = sum(var(v, 1) for v in groups) / len(groups)
    means = [sum(v) / len(v) for v in groups]
    tau2 = var(means) - sum(within / len(v) for v in groups) / len(groups)
    k = within / tau2 if tau2 > 0 else THOMPSON_K_RANGE[1]
    return {"k": max(THOMPSON_K_RANGE[0], min(THOMPSON_K_RANGE[1], k)), "sd_within": max(THOMPSON_SD_MIN, within ** 0.5),
            "sd_between": max(tau2, 0.0) ** 0.5, "k_wallets": len(groups)}


def thompson(conn, edge: dict, seed: str) -> dict:
    """Thompson sampling instead of a yes/no gate: the wallet's edge per copy is a belief (population prior plus
    its own decayed replays, normal), one draw from it decides. Unknown wallets get drawn positive often enough to
    be tried, proven losers rarely, proven winners nearly always, and every closed copy narrows the belief.
    Size follows the draw (EDGE_SIZE_UNIT = 1x, 0.25x..1.5x). The seed (the whale's signature) makes the draw the
    same in every process that sees this buy. -> {"draw", "mean", "sd", "p_pos", "size_factor", "prior"}"""
    import math
    import random

    pop = population_edge(conn)
    n = float(edge.get("n_eff") or 0.0)
    k = pop.get("k") or THOMPSON_PRIOR_N  # measured from the data once enough wallets have replays
    mean = (k * pop["mean"] + n * edge.get("mean", 0.0)) / (k + n)
    sd = (pop.get("sd_within") or pop["sd"]) / math.sqrt(k + n)
    draw = random.Random(seed).gauss(mean, sd)
    p_pos = 0.5 * (1 + math.erf(mean / (sd * math.sqrt(2))))
    return {"draw": draw, "mean": mean, "sd": sd, "p_pos": p_pos, "prior": pop["mean"], "k": round(k, 1),
            "size_factor": max(0.25, min(1.5, draw / EDGE_SIZE_UNIT)) if draw > 0 else 0.0}


def rollback(conn) -> list[int]:
    """Deactivate learned versions whose own trades fall clearly short of what the test promised."""
    out = []
    for v in conn.execute("SELECT id, strategy, scope, test_gain FROM ct_param_versions WHERE active=1").fetchall():
        # strict accounts only: the exploration account trades wider on purpose and must not trigger or mask this
        r = conn.execute("SELECT COUNT(*), AVG(pnl_pct) FROM ct_positions WHERE param_version=? AND status='closed' "
                         "AND mode!='explore'", (v["id"],)).fetchone()
        n, avg = r[0] or 0, r[1]
        if n >= ROLLBACK_MIN_TRADES and avg is not None and avg < 0 and avg < (v["test_gain"] or 0) - ROLLBACK_MARGIN:
            conn.execute("UPDATE ct_param_versions SET active=0 WHERE id=?", (v["id"],))
            from . import store

            store.event(conn, "learn", f"[{v['strategy']}] Version {v['id']} zurückgerollt: {n} echte Trades "
                                       f"Ø {avg:+.1%} statt Test {(v['test_gain'] or 0):+.1%}",
                        data={"version": v["id"], "trades": n, "avg": avg})
            out.append(v["id"])
    return out


def robustness(test: Sequence[dict], best: int, base: int, variants: Sequence[sim.Variant],
               mte: Sequence[float], cost: float = 0.0, runs: int = BOOTSTRAP_RUNS) -> dict:
    """Is the winner an edge or luck? Monte Carlo over the test entries (bootstrap with replacement): share of
    runs where it is in the black AND beats the current settings; and parameter jitter: the mean test result
    of its grid neighbours (one step away in one parameter) must be in the black too (no lone spike)."""
    import random

    # only entries both variants could simulate: a missing one is no 0 % trade
    rows = [r for r in test if len(r["gains"]) == len(variants)
            and r["gains"][best] is not None and r["gains"][base] is not None]
    if not rows:
        return {"p_win": 0.0, "neighbours": None, "runs": 0}
    gb = [max(GAIN_CLIP[0], min(GAIN_CLIP[1], r["gains"][best])) for r in rows]
    g0 = [max(GAIN_CLIP[0], min(GAIN_CLIP[1], r["gains"][base])) for r in rows]
    w = [r["w"] for r in rows]
    rng = random.Random(len(rows) * 7919 + best)  # deterministic: the same data gives the same verdict
    n, wins = len(rows), 0
    for _ in range(runs):
        idx = [rng.randrange(n) for _ in range(n)]
        sw = sum(w[i] for i in idx) or 1
        mb = sum(w[i] * gb[i] for i in idx) / sw - cost
        m0 = sum(w[i] * g0[i] for i in idx) / sw - cost
        wins += mb > 0 and mb > m0
    nb = [mte[j] for j in sim.neighbors(variants, best)]
    return {"p_win": wins / runs, "neighbours": sum(nb) / len(nb) if nb else None, "runs": runs}


def _allowed(variants: Sequence[sim.Variant], cap_sl: float, cap_hold: float) -> list[int]:
    return [i for i, v in enumerate(variants) if v.stop_loss <= cap_sl + 1e-9 and v.max_hold_min <= cap_hold + 1e-9]


def current_params(conn, strategy: str, wallet: str | None = None) -> dict:
    """What the bot uses right now (active learned version, else the preset)."""
    lp = params_for(conn, strategy, wallet, apply_caps=False)
    if lp:
        return lp
    p = control.preset(conn, strategy)
    return {"delay_s": sim.DELAYS_S[0], "take_profit": p.take_profit, "stop_loss": p.stop_loss,
            "trail_activate": p.trail_activate, "trail_drawdown": p.trail_drawdown,
            "max_hold_min": p.max_hold_min, "inflow_exit": 1 if strategy == "whale" else 0}


def _learn_filters(rows: Sequence[dict], best: int, split: int) -> dict[str, list[str]]:
    """Buckets that lost money with the chosen variant in train AND test."""
    block: dict[str, list[str]] = {}
    keys = {k for r in rows for k in r["features"] if k not in ("strategy",)}
    for k in keys:
        vals = {str(r["features"].get(k)) for r in rows}
        for val in vals:
            if val in ("?", "-", "None"):
                continue
            tr = [r["gains"][best] for r in rows[:split] if str(r["features"].get(k)) == val and r["gains"][best] is not None]
            te = [r["gains"][best] for r in rows[split:] if str(r["features"].get(k)) == val and r["gains"][best] is not None]
            if len(tr) >= 15 and len(te) >= 8 and sum(tr) / len(tr) < -0.02 and sum(te) / len(te) < -0.02:
                block.setdefault(k, []).append(val)
    return block


def tune_scope(conn, strategy: str, rows: list[dict], scope: str, min_n: int, min_test: int,
               wallet: str | None = None) -> dict | None:
    variants = sim.GRID if strategy == "whale" else sim.EXIT_GRID
    rows = [r for r in rows if len(r["gains"]) == len(variants)]
    n = len(rows)
    if n < min_n:
        return None
    split = int(n * TRAIN_SHARE)
    train, test = rows[:split], rows[split:]
    if len(test) < min_test:
        return None
    pre = control.preset(conn, strategy)
    allowed = _allowed(variants, pre.stop_loss, max(pre.max_hold_min, 15))
    if not allowed:
        return None
    cost = extra_cost(conn, strategy)
    mtr, mte = _means(train, len(variants), cost), _means(test, len(variants), cost)
    best = max(allowed, key=lambda i: mtr[i])
    cur = current_params(conn, strategy, wallet)
    base = sim.nearest(cur, variants)
    # in the black on the older AND the newer part, and better than now out of sample; a variant that loses
    # money in training is the least bad of 1296, not an edge (10/2026: version 9 had train -0.8 %)
    accepted = int(mte[best] > mte[base] + MIN_IMPROVEMENT and mte[best] > 0 and mtr[best] > 0)
    rob = robustness(test, best, base, variants, mte, cost) if accepted else None
    if rob and (rob["p_win"] < BOOTSTRAP_MIN_WIN or rob["neighbours"] is None or rob["neighbours"] <= 0):
        accepted = 0  # lucky on this test set or a lone spike in the grid: not stable enough
    params = variants[best].as_dict()
    if scope == "mode":
        params["block"] = _learn_filters(rows, best, split)
    trades = [r for r in rows if r["kind"] == "trade" and r["actual"] is not None and r["sim_actual"] is not None]
    gap = (sum(r["actual"] - r["sim_actual"] for r in trades) / len(trades)) if trades else None
    notes = {"variants": len(variants), "baseline": variants[base].key(), "best": variants[best].key(),
             "calibration_gap": gap, "extra_cost": cost, "robustness": rob, "n_trades": len(trades),
             "kinds": {k: sum(1 for r in rows if r["kind"] == k) for k in ("trade", "whale_buy", "signal")}}
    last = conn.execute(
        "SELECT params, accepted FROM ct_param_versions WHERE strategy=? AND scope=? ORDER BY id DESC LIMIT 1",
        (strategy, scope)).fetchone()
    if last and not accepted and not last["accepted"] and json.loads(last["params"]).get("take_profit") == params["take_profit"]:
        return None  # nothing new to record
    if accepted:
        conn.execute("UPDATE ct_param_versions SET active=0 WHERE strategy=? AND scope=?", (strategy, scope))
    conn.execute(
        """INSERT INTO ct_param_versions(ts, strategy, scope, params, n_train, n_test, train_gain, test_gain,
             baseline_test_gain, accepted, active, notes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (int(time.time()), strategy, scope, json.dumps(params), len(train), len(test), mtr[best], mte[best],
         mte[base], accepted, accepted, json.dumps(notes)))
    if accepted:
        from . import store

        store.event(conn, "learn",
                    f"[{strategy}{'' if scope == 'mode' else ' ' + scope[:12] + '…'}] neue Parameter gelernt: "
                    f"{variants[best].key()} (Test {mte[best]:+.1%} statt {mte[base]:+.1%} pro Trade, "
                    f"{len(test)} Test-Einstiege)", data={"params": params, "notes": notes})
    return {"accepted": bool(accepted), "params": params, "test_gain": mte[best], "baseline": mte[base]}


def tune(conn, s: Settings) -> dict:
    out: dict = {}
    for strategy in control.STRATEGIES:
        if not enabled(conn, strategy):
            continue
        min_score = control.preset(conn, "signal").min_score if strategy == "signal" else None
        rows = _rows(conn, strategy, min_signal_score=min_score)
        out[strategy] = tune_scope(conn, strategy, rows, "mode", MIN_MODE_N, MIN_MODE_TEST)
    if enabled(conn, "whale"):
        from . import store

        groups: dict[str, list[tuple[str, int]]] = {}  # entity (cluster or wallet) -> [(wallet, n)]
        for r in conn.execute("""SELECT wallet, COUNT(*) n FROM ct_evals WHERE strategy='whale' AND wallet!=''
                                 AND gains!='[]' GROUP BY wallet""").fetchall():
            groups.setdefault(store.entity(conn, r["wallet"]), []).append((r["wallet"], r["n"]))
        for ent, ws in groups.items():
            if sum(n for _, n in ws) < MIN_WALLET_N:
                continue
            res = tune_scope(conn, "whale", _rows(conn, "whale", [w for w, _ in ws]), ent, MIN_WALLET_N,
                             MIN_WALLET_TEST, wallet=ws[0][0])
            if res:
                out[f"whale:{ent[:16]}"] = res
    return out


LAND_S = 3  # quote to on-chain landing with a priority fee
LAND_MIN_N = 30
LAND_BUFFER = 0.005  # sandwich / MEV allowance on top of the measured drift (not visible in the tape)
LAND_MIN, LAND_MAX = 0.0075, 0.02


def landing_drift(conn, land_s: int = LAND_S) -> dict:
    """How far the market moved against our paper fills in the seconds a real swap needs to land, measured on the
    coin's real trade tape (GeckoTerminal trades) around our own entries and exits. Returns per-side means and the
    adverse slippage to use for paper fills: mean adverse move + buffer, clamped to 0.75..2 %; the old fixed 2 %
    until LAND_MIN_N fills are measured."""
    def px(mint, t):
        r = conn.execute("SELECT price_usd, ts FROM trades WHERE mint=? AND ts<=? AND price_usd>0 ORDER BY ts DESC "
                         "LIMIT 1", (mint, int(t))).fetchone()
        return (r[0], t - r[1]) if r else (None, None)

    def px_after(mint, t, land):
        # the first real print inside the landing window: without one there is nothing to measure (the last print
        # before t + land was the same print as at t, a 0 that pulled the mean down to the 0.75 % floor)
        r = conn.execute("SELECT price_usd FROM trades WHERE mint=? AND ts>? AND ts<=? AND price_usd>0 ORDER BY ts "
                         "DESC LIMIT 1", (mint, int(t), int(t + land))).fetchone()
        return r[0] if r else None

    buys, sells = [], []
    try:
        rows = conn.execute("SELECT mint, opened_at, closed_at FROM ct_positions WHERE mode IN ('paper','explore') "
                            "ORDER BY opened_at DESC LIMIT 400").fetchall()
        for mint, op, cl in rows:
            for t, out, sign in ((op, buys, 1), (cl, sells, -1)):
                if not t:
                    continue
                a, age = px(mint, t)
                b = px_after(mint, t, land_s)
                if a and b and age < 60:
                    out.append(sign * (b / a - 1))  # >0 = moved against us
    except Exception as e:  # no trade tape (signals/ not running)
        log.debug("landing drift: %s", e)
    n = min(len(buys), len(sells))
    res = {"n_buys": len(buys), "n_sells": len(sells),
           "buy": round(sum(buys) / len(buys), 4) if buys else None,
           "sell": round(sum(sells) / len(sells), 4) if sells else None}
    if n >= LAND_MIN_N:
        side = max(res["buy"], res["sell"], 0.0)
        res["adverse"] = round(min(LAND_MAX, max(LAND_MIN, side + LAND_BUFFER)), 4)
    else:
        res["adverse"] = LAND_MAX
    return res


def calibrate_fills(conn) -> dict:
    from . import execution, store

    d = landing_drift(conn)
    execution.set_paper_adverse(d["adverse"])
    store.set_state(conn, "paper_adverse", json.dumps(d))
    return d


EVENTS_KEEP_D, TRADE_EVENTS_KEEP_D, EQUITY_KEEP_D = 7, 90, 90


def prune(conn, now: float | None = None) -> int:
    """Retention, at most hourly: skip/info events 7 days, buy/sell/error events and equity points 90 days."""
    from . import store

    now = now or time.time()
    if float(store.get_state(conn, "pruned_at") or 0) > now - 3600:
        return 0
    n = conn.execute("DELETE FROM ct_events WHERE ts<? AND kind NOT IN ('buy','sell','error','start','stop')",
                     (int(now - EVENTS_KEEP_D * 86400),)).rowcount
    n += conn.execute("DELETE FROM ct_events WHERE ts<?", (int(now - TRADE_EVENTS_KEEP_D * 86400),)).rowcount
    n += conn.execute("DELETE FROM ct_equity WHERE ts<?", (int(now - EQUITY_KEEP_D * 86400),)).rowcount
    store.set_state(conn, "pruned_at", str(int(now)))
    conn.commit()
    return n


def run(conn, s: Settings) -> dict:
    """One learning round (called by the maintenance loop): evaluate new entries, then tune."""
    init(conn)
    n = {"trades": evaluate_trades(conn, s), "whale_buys": evaluate_whale_buys(conn, s),
         "signals": evaluate_signals(conn, s)}
    n["rolled_back"] = rollback(conn)
    n["tuned"] = tune(conn, s)
    try:
        n["pruned"] = prune(conn)
    except Exception as e:
        log.warning("prune failed: %s", e)
    try:
        n["fills"] = calibrate_fills(conn)
    except Exception as e:
        log.warning("fill calibration failed: %s", e)
    return n


# ---------------------------------------------------------------------- 3. use
def params_for(conn, strategy: str, wallet: str | None = None, apply_caps: bool = True) -> dict | None:
    """Active learned parameters (wallet-specific first, then mode), capped by the current preset."""
    if not enabled(conn, strategy):
        return None
    try:
        r = None
        if wallet:
            from . import store

            r = conn.execute("SELECT id, params FROM ct_param_versions WHERE strategy=? AND scope=? AND active=1 "
                             "AND train_gain > 0 ORDER BY id DESC LIMIT 1", (strategy, store.entity(conn, wallet))).fetchone()
        if r is None:  # versions accepted before training had to be in the black are ignored
            r = conn.execute("SELECT id, params FROM ct_param_versions WHERE strategy=? AND scope='mode' AND active=1 "
                             "AND train_gain > 0 ORDER BY id DESC LIMIT 1", (strategy,)).fetchone()
    except Exception:
        return None
    if r is None:
        return None
    p = json.loads(r["params"])
    p["version"] = r["id"]
    if apply_caps:
        pre = control.preset(conn, strategy)
        p["stop_loss"] = min(p["stop_loss"], pre.stop_loss)
        p["max_hold_min"] = min(p["max_hold_min"], pre.max_hold_min)
    return p


def blocked(conn, strategy: str, features: dict) -> str | None:
    """Name of a learned filter that blocks these features, or None."""
    lp = params_for(conn, strategy)
    for k, vals in ((lp or {}).get("block") or {}).items():
        if str(features.get(k)) in vals:
            return f"{k}={features.get(k)}"
    return None


# ---------------------------------------------------------------------- insight (dashboard / terminal)
def versions(conn, strategy: str | None = None, scope: str | None = None):
    """Parameter history with the paper/live P&L of the trades each version actually traded."""
    import pandas as pd

    q = "SELECT * FROM ct_param_versions WHERE 1=1"
    args: list = []
    if strategy:
        q += " AND strategy=?"
        args.append(strategy)
    if scope:
        q += " AND scope=?"
        args.append(scope)
    df = pd.read_sql_query(q + " ORDER BY id", conn, params=args)
    if df.empty:
        return df
    pnl = pd.read_sql_query(
        """SELECT param_version AS id, COUNT(*) AS trades, SUM(pnl_sol) AS pnl_sol, AVG(pnl_pct) AS avg_pct,
                  AVG(CASE WHEN pnl_sol>0 THEN 1.0 ELSE 0 END) AS win_rate
           FROM ct_positions WHERE status='closed' AND param_version IS NOT NULL GROUP BY param_version""", conn)
    df = df.merge(pnl, on="id", how="left")
    df["dt"] = pd.to_datetime(df["ts"], unit="s", utc=True)
    return df


def variant_surface(conn, strategy: str, x: str = "take_profit", y: str = "stop_loss"):
    """Mean net gain per (x, y) over all evaluated entries (best over the other parameters) for a heatmap."""
    import pandas as pd

    variants = sim.GRID if strategy == "whale" else sim.EXIT_GRID
    rows = [r for r in _rows(conn, strategy) if len(r["gains"]) == len(variants)]
    if not rows:
        return pd.DataFrame()
    m = _means(rows, len(variants))
    df = pd.DataFrame([{**v.as_dict(), "gain": m[i]} for i, v in enumerate(variants)])
    return df.groupby([x, y])["gain"].max().reset_index().assign(n=len(rows))


def attribution(conn, strategy: str):
    """Mean net gain with the current parameters per feature bucket (tier, grade, mcap, liquidity, ...)."""
    import pandas as pd

    variants = sim.GRID if strategy == "whale" else sim.EXIT_GRID
    rows = [r for r in _rows(conn, strategy) if len(r["gains"]) == len(variants)]
    if not rows:
        return pd.DataFrame()
    i = sim.nearest(current_params(conn, strategy), variants)
    recs = []
    for r in rows:
        g = r["gains"][i]
        if g is None:
            continue
        for k, v in r["features"].items():
            if k != "strategy":
                recs.append({"feature": k, "bucket": str(v), "gain": g, "kind": r["kind"]})
    df = pd.DataFrame(recs)
    if df.empty:
        return df
    return (df.groupby(["feature", "bucket"])
              .agg(n=("gain", "size"), avg_gain=("gain", "mean"), win_rate=("gain", lambda x: (x > 0).mean()))
              .reset_index())


def exit_rule_comparison(conn, strategy: str):
    """For our closed trades: what each simple exit style would have returned vs. what we got."""
    import pandas as pd

    variants = sim.GRID if strategy == "whale" else sim.EXIT_GRID
    rows = [r for r in _rows(conn, strategy) if r["kind"] == "trade" and len(r["gains"]) == len(variants)]
    if not rows:
        return pd.DataFrame()
    m = _means(rows, len(variants))
    styles = {
        "nur Kursziel": [i for i, v in enumerate(variants) if v.trail_activate == 0 and not v.inflow_exit],
        "Trailing-Stop": [i for i, v in enumerate(variants) if v.trail_activate > 0 and not v.inflow_exit],
        "Copy-Ansturm-Ende": [i for i, v in enumerate(variants) if v.inflow_exit],
        "kurz halten (≤15 min)": [i for i, v in enumerate(variants) if v.max_hold_min <= 15],
        "lange halten (≥120 min)": [i for i, v in enumerate(variants) if v.max_hold_min >= 120],
    }
    actual = sum(r["actual"] for r in rows if r["actual"] is not None) / max(1, sum(r["actual"] is not None for r in rows))
    out = [{"style": "tatsächlich", "avg_gain": actual, "best_variant": "", "n": len(rows)}]
    for name, idx in styles.items():
        if idx:
            b = max(idx, key=lambda i: m[i])
            out.append({"style": name, "avg_gain": m[b], "best_variant": variants[b].key(), "n": len(rows)})
    return pd.DataFrame(out)


def status(conn) -> dict:
    """Counts for the dashboard: how much the learner has seen, what is active, what it still needs."""
    st: dict = {}
    for strategy in control.STRATEGIES:
        rows = conn.execute("SELECT kind, COUNT(*) FROM ct_evals WHERE strategy=? AND gains!='[]' GROUP BY kind",
                            (strategy,)).fetchall()
        n = {r[0]: r[1] for r in rows}
        total = sum(n.values())
        act = params_for(conn, strategy, apply_caps=False)
        st[strategy] = {"autotune": enabled(conn, strategy), "evaluated": n, "total": total,
                        "needed_for_tuning": max(0, MIN_MODE_N - total), "active_version": act,
                        "wallet_versions": conn.execute(
                            "SELECT COUNT(*) FROM ct_param_versions WHERE strategy=? AND scope!='mode' AND active=1",
                            (strategy,)).fetchone()[0]}
    return st

