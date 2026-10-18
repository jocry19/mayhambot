"""Every recognised buy of a tracked wallet as an opportunity with its features (plan 18.10., "Richtung neu").

The feed knows things about a whale buy nobody else does: how late we saw it, through which path, the size, the
wallet's tier / model score / own net history, its group (manual, block-0, cluster) and the venue. `features()`
collects them in one flat dict; the shared opportunity table (schema by the decider thread) receives them through
`emit()` (brain.opportunities.report, one row per buy, ref = signature:wallet).
"""
from __future__ import annotations

import logging
import time

from . import store

log = logging.getLogger("copytrade.opportunity")


def features(conn, sw, source: str, lag_s: float | None, notified_at: float | None = None,
             sol_usd: float = 0.0) -> dict:
    """Flat feature dict of one recognised swap (only what the feed knows without extra RPC calls)."""
    from . import tracker

    info = store.wallet_info(conn, sw.wallet) or {}
    try:
        rank = conn.execute("SELECT samples, hit_rate, expected_value, score, avg_loss FROM ct_wallet_rank "
                            "WHERE address=?", (sw.wallet,)).fetchone()
    except Exception:
        rank = None
    try:
        src = (conn.execute("SELECT source FROM ct_wallets WHERE address=?", (sw.wallet,)).fetchone() or [None])[0]
    except Exception:
        src = None
    group = "block0" if sw.wallet in tracker.BLOCK0 else ("manual" if src == "manual" else
                                                           (info.get("tier") or "none"))
    now = time.time()
    return {
        "source": "copytrade", "kind": f"whale_{sw.side}", "mint": sw.mint, "wallet": sw.wallet,
        "sig": sw.signature, "ts": int(sw.block_time or now), "seen_at": now,
        "detect_path": source,  # wslog | ws | poll | gap
        "lag_s": None if lag_s is None or lag_s != lag_s else round(lag_s, 2),
        "notify_lag_s": round(now - notified_at, 2) if notified_at else None,
        "sol": sw.sol_amount, "usd": sw.sol_amount * sol_usd if sol_usd else None,
        "price_sol": sw.price_sol, "mcap_usd": (getattr(sw, "mcap_sol", 0.0) or 0.0) * sol_usd or None,
        "dex": getattr(sw, "dex", "") or None,
        "group": group, "tier": info.get("tier") or "none", "model_score": info.get("model_score"),
        "cluster_id": info.get("cluster_id"), "exit_capture": info.get("exit_capture"),
        "rank_samples": rank["samples"] if rank else None, "rank_hit_rate": rank["hit_rate"] if rank else None,
        "rank_ev": rank["expected_value"] if rank else None, "rank_score": rank["score"] if rank else None,
        "rank_avg_loss": rank["avg_loss"] if rank else None,
    }


DEX_NAMES = {"pump": "pumpfun"}  # shared `dex` names of the opportunity table


def brain_report(conn, f: dict) -> None:
    """The shared opportunity table (brain/opportunities.py, schema plan/gelegenheiten_schema.md)."""
    from brain.opportunities import report  # type: ignore

    shared = {"delay_s": f["lag_s"], "wallet_tier": f["tier"], "wallet_score": f["model_score"],
              "mc_usd": f["mcap_usd"], "dex": DEX_NAMES.get(f["dex"], f["dex"])}
    own = {f"copytrade.{k}": f[k] for k in ("group", "detect_path", "notify_lag_s", "usd", "cluster_id",
                                            "exit_capture", "rank_samples", "rank_hit_rate", "rank_ev",
                                            "rank_score", "rank_avg_loss")}
    own["copytrade.size_sol"] = f["sol"]
    own["copytrade.block0"] = f["group"] == "block0"
    report(conn, "copytrade", f"{f['sig']}:{f['wallet']}", f["mint"], f["seen_at"], {**shared, **own},
           src_ts=f["ts"], price_sol=f["price_sol"] or None, mcap_usd=f["mcap_usd"])


_SINK = {"fn": "auto"}  # "auto": brain_report when the brain package is there


def set_sink(fn) -> None:
    """fn(conn, features_dict): the writer into the shared opportunity table (None: nothing is written)."""
    _SINK["fn"] = fn


def emit(conn, sw, source: str, lag_s: float | None, notified_at: float | None = None, sol_usd: float = 0.0) -> None:
    fn = _SINK["fn"]
    if fn == "auto":
        try:
            import brain.opportunities  # type: ignore # noqa: F401

            fn = _SINK["fn"] = brain_report
        except Exception:
            fn = _SINK["fn"] = None
    if fn is None or sw.side != "buy":
        return
    try:
        fn(conn, features(conn, sw, source, lag_s, notified_at, sol_usd))
    except Exception as e:  # never let a reporting problem hold up detection
        log.debug("opportunity %s not written: %s", sw.signature[:8], e)
