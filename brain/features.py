"""Context of one opportunity, as the brain sees it at the moment of the decision (point in time, DB only, plus the
DexScreener snapshot the caller already fetched).

Features are buckets (strings). Names shared with copytrade's replays (tier, capture, size, wash, persons, liq60,
scalper, focus, mcap, liq, score, buyers, ultra) use copytrade.learn's bucket functions, so ct_evals rows are prior
knowledge in the same language. Everything else is the brain's own view: timing, crowd, momentum, rug verdict, market
phase, the Zentrale's walk-forward features, the signal's EV and re-calls, the sniper's launch checks, own form.
"""
from __future__ import annotations

import json
import logging
import time

log = logging.getLogger("brain.features")

MISSING = (None, "", "?", "-")

LABELS = {
    "kind": "Art", "tier": "Wal-Stufe", "capture": "Wal nimmt vom Pump mit", "size": "Wal-Kauf", "wash": "Wash-Anteil",
    "persons": "Personen/h", "liq60": "Liquidität 1 h", "scalper": "Scalp-Anteil des Wals", "focus": "Fokus des Wals",
    "mcap": "Market Cap", "liq": "Liquidität", "score": "Signal-Score", "buyers": "Smart Wallets",
    "ultra": "Ultra-Wallet dabei", "grade": "Modell-Note", "safety": "Rug-Urteil", "hour": "Tageszeit (UTC)",
    "delay": "erkannt nach", "run": "Preis seit Auslöser", "crowd": "weitere Smart Wallets im Coin (30 min)",
    "sigcall": "Smart-Money-Signal auf dem Coin (6 h)", "edge": "Wal-Bilanz nach Kosten", "launchbot": "Launch-Bot",
    "age": "Coin-Alter", "mom": "Kurs 5 min", "pressure": "Kaufdruck 5 min", "phase": "Marktphase",
    "ev": "Signal-EV", "call": "Call Nr.", "lscore": "Sniper-Score", "lrisk": "Sniper-Risiko",
    "bundle": "Bundle", "insider": "Insider", "devl": "Dev-Launches", "top10": "Top-10-Anteil",
    "socials": "Socials", "lsmart": "Smart Wallets im Launch", "modelp": "Modell-Chance", "form": "eigene Form",
    "flip": "Wal verkauft binnen 2 min", "run5": "Kurs 5 min vor dem Kauf",
}


def b(x, edges, labels) -> str:
    if x is None:
        return "?"
    for e, lab in zip(edges, labels):
        if x < e:
            return lab
    return labels[-1]


# features every kind has: these get a global token (what generalizes) plus a per-kind token (where the kinds differ);
# kind-specific features (wallet tier, signal EV, sniper score ...) get one token, a duplicate would only split it
SHARED = {"hour", "age", "mom", "pressure", "safety", "phase", "form", "delay", "run", "mcap", "liq"}


def tokens(kind: str, feats: dict) -> list[str]:
    """Sparse context tokens of one opportunity."""
    out = [f"kind={kind}"]
    for k, v in sorted(feats.items()):
        if k in ("kind", "strategy", "explore", "gap") or v in MISSING or isinstance(v, bool):
            continue
        out.append(f"{k}={v}")
        if k in SHARED or k.startswith("t_"):
            out.append(f"{kind}:{k}={v}")
    return out


def describe(token: str) -> str:
    """'whale:tier=ultra' -> 'Wal-Stufe ultra'."""
    t = token.split(":", 1)[-1]
    k, _, v = t.partition("=")
    if k.startswith("t_"):
        return f"Zentrale {k[2:]} {v}"
    if k == "kind":
        return {"whale": "Wal-Kopie", "signal": "Smart-Money-Signal", "launch": "Launch"}.get(v, v)
    return f"{LABELS.get(k, k)} {v}"


# ---------------------------------------------------------------------- shared context
_trusted_seen: dict[str, list[float]] = {}


def _trusted(conn, mint: str) -> dict:
    """Zentrale features that passed walk-forward (intel.trusted_at), as 'gut'/'schlecht' against the median of
    the values the brain saw so far (the list itself carries direction, not thresholds)."""
    try:
        import intel  # type: ignore

        cur = intel.trusted_at(conn, mint)
    except Exception as e:
        log.debug("trusted %s: %s", mint[:6], e)
        return {}
    out = {}
    for name, d in (cur or {}).items():
        v, sign = d.get("value"), d.get("direction") or 0
        if v is None or not sign:
            continue
        seen = _trusted_seen.setdefault(name, [])
        seen.append(float(v))
        del seen[:-500]
        if len(seen) < 10:
            continue
        med = sorted(seen)[len(seen) // 2]
        out[f"t_{name}"] = "gut" if (v > med) == (sign > 0) else "schlecht"
    return out


def _phase(conn, now: float) -> str | None:
    try:
        from market import context  # type: ignore

        ph = context.phase(conn, now, max_age=3600)
        return (ph or {}).get("label") or None
    except Exception:
        return None


def _safety(conn, mint: str) -> str | None:
    try:
        import safety  # type: ignore

        a = safety.get(conn, mint)
        return a.level if a else None
    except Exception:
        return None


def form(conn, kind: str) -> str | None:
    """The brain's own recent results in this kind (last 10 closed trades, from 5)."""
    rows = [r[0] for r in conn.execute("SELECT pnl_pct FROM brain_positions WHERE kind=? AND status='closed' "
                                       "ORDER BY closed_at DESC LIMIT 10", (kind,)).fetchall() if r[0] is not None]
    if len(rows) < 5:
        return None
    m = sum(rows) / len(rows)
    return "gut" if m > 0.02 else "schlecht" if m < -0.02 else "neutral"


def market_feats(conn, mint: str, snap, now: float, kind: str) -> dict:
    f = {"hour": b(time.gmtime(now).tm_hour, (6, 12, 18), ("0-6", "6-12", "12-18", "18-24"))}
    if snap is not None:
        age = snap.age_hours if snap.pair_created_ms else None
        f["age"] = b(age, (1, 6, 24), ("<1h", "1-6h", "6-24h", ">24h"))
        f["mom"] = b(snap.change_m5, (-10, 0, 10, 30), ("<-10%", "-10-0%", "0-10%", "10-30%", ">30%"))
        tx = (snap.buys_m5 or 0) + (snap.sells_m5 or 0)
        f["pressure"] = b(snap.buys_m5 / tx if tx >= 5 else None, (0.45, 0.6), ("<45%", "45-60%", ">60%"))
    f["safety"] = _safety(conn, mint) or "?"
    f["phase"] = _phase(conn, now) or "?"
    f["form"] = form(conn, kind) or "?"
    f.update(_trusted(conn, mint))
    return f


def _crowd(conn, mint: str, wallet: str, now: float) -> tuple[int, bool]:
    """(other smart entities buying this coin in the last 30 min, smart-money signal on it within 6 h)."""
    from copytrade import store as ct_store

    me = ct_store.entity(conn, wallet)
    ents = set()
    try:
        for (w,) in conn.execute("SELECT DISTINCT wallet FROM ct_swaps WHERE mint=? AND side='buy' AND ts>=? AND wallet!=?",
                                 (mint, int(now) - 1800, wallet)).fetchall():
            if (ct_store.wallet_info(conn, w).get("tier") or "none") in ("ultra", "smart", "good"):
                e = ct_store.entity(conn, w)
                if e != me:
                    ents.add(e)
    except Exception:
        pass
    try:
        sig = conn.execute("SELECT 1 FROM signals WHERE mint=? AND ts>=?", (mint, int(now) - 6 * 3600)).fetchone()
    except Exception:
        sig = None
    return len(ents), bool(sig)


# ---------------------------------------------------------------------- per kind
def on_curve(snap) -> bool:
    """Still on the pump.fun bonding curve: DexScreener shows no pool liquidity, the curve is the market."""
    return snap is not None and snap.dex_id == "pumpfun" and not snap.liquidity_usd


def liq_bucket(snap) -> str:
    from copytrade import learn

    return "curve" if on_curve(snap) else learn.bucket_liq(snap.liquidity_usd)


FLIP_S = 120
FLIP_LOOKBACK_S = 7 * 86400


def flip(conn, wallet: str, now: float, limit: int = 40) -> dict:
    """How often this wallet sold a coin within FLIP_S of buying it (its recent buys, 7 days): a copy enters seconds
    after the whale, so a wallet that is out again within two minutes leaves the copy holding the dip."""
    rows = conn.execute(
        """SELECT b.mint, b.ts,
                  (SELECT MIN(s.ts) FROM ct_swaps s WHERE s.wallet=b.wallet AND s.mint=b.mint AND s.side='sell'
                     AND s.ts>=b.ts) AS sold
           FROM ct_swaps b WHERE b.wallet=? AND b.side='buy' AND b.ts>=? AND b.ts<?
           ORDER BY b.ts DESC LIMIT ?""", (wallet, int(now - FLIP_LOOKBACK_S), int(now - FLIP_S), limit)).fetchall()
    seen, n, fast = set(), 0, 0
    for r in rows:  # first buy per coin only: adds to a position are no new round trip
        if r["mint"] in seen:
            continue
        seen.add(r["mint"])
        n += 1
        fast += r["sold"] is not None and r["sold"] - r["ts"] <= FLIP_S
    return {"n": n, "share": fast / n if n else None}


def whale(conn, sw, snap, now: float) -> tuple[dict, dict]:
    """sw: a ct_swaps row (buy). -> (features, raw facts for the note)."""
    from copytrade import learn, store as ct_store
    from copytrade.strategy import launch_bot_share

    w, mint = sw["wallet"], sw["mint"]
    info = ct_store.wallet_info(conn, w)
    f = {**learn.wallet_features(conn, w), "size": learn.bucket_size(sw["sol_amount"])}
    ib, iraw = learn.intel_context(conn, mint, w, now)
    f.update(ib)
    delay = now - sw["ts"]
    f["delay"] = b(delay, (5, 15, 30), ("<5s", "5-15s", "15-30s", ">30s"))
    run = (snap.price_sol / sw["price_sol"] - 1) if snap and snap.price_sol and sw["price_sol"] else None
    f["run"] = b(run, (0, 0.05, 0.15), ("<0%", "0-5%", "5-15%", ">15%"))
    crowd, sigcall = _crowd(conn, mint, w, now)
    f["crowd"] = "2+" if crowd >= 2 else str(crowd)
    f["sigcall"] = "ja" if sigcall else "nein"
    try:
        e = learn.wallet_edge(conn, w)
        f["edge"] = ("?" if e["n"] < 8 else "belegt" if e["lower"] > 0 else "plus" if e["mean"] > 0 else "minus")
    except Exception:
        e = {}
    fl = flip(conn, w, sw["ts"])
    if fl["n"] >= 5:
        f["flip"] = b(fl["share"], (0.2, 0.5), ("<20%", "20-50%", ">50%"))
    nb, kb = launch_bot_share(conn, w)
    if nb >= 8:
        f["launchbot"] = "ja" if kb / nb >= 0.6 else "nein"
    if snap is not None:
        f["mcap"] = learn.bucket_mcap(snap.fdv)
        f["liq"] = liq_bucket(snap)
    f.update(market_feats(conn, mint, snap, now, "whale"))
    ent = ct_store.entity(conn, w)
    raw = {"wallet": w, "tier": info.get("tier") or "none", "cluster": ent if ent != w else None,
           "sol": sw["sol_amount"], "delay_s": round(delay, 1), "run": run, "crowd": crowd,
           "edge_n": e.get("n"), "edge_mean": e.get("mean"), "flip": fl, "thesis": learn.thesis(iraw), "intel": iraw}
    return f, raw


def signal(conn, sig, snap, now: float) -> tuple[dict, dict]:
    from copytrade import learn

    keys = sig.keys()
    f = dict(learn.signal_features(sig))
    ev = sig["ev"] if "ev" in keys else None
    f["ev"] = b(ev, (-0.05, 0, 0.05), ("<-5%", "-5-0%", "0-5%", ">5%"))
    call = sig["call_no"] if "call_no" in keys else None
    f["call"] = "?" if call is None else ("1" if call <= 1 else "2" if call == 2 else "3+")
    delay = now - sig["ts"]
    f["delay"] = b(delay, (15, 60, 120), ("<15s", "15-60s", "1-2min", ">2min"))
    run = (snap.price_usd / sig["price_usd"] - 1) if snap and snap.price_usd and sig["price_usd"] else None
    f["run"] = b(run, (0, 0.05, 0.15), ("<0%", "0-5%", "5-15%", ">15%"))
    if snap is not None:
        f["liq"] = liq_bucket(snap)
    f.update(market_feats(conn, sig["mint"], snap, now, "signal"))
    raw = {"score": sig["score"], "buyers": sig["smart_buyers"], "net_usd": sig["net_usd"], "ev": ev, "call": call,
           "delay_s": round(delay, 1), "run": run,
           "n_ultra": sig["n_ultra"] if "n_ultra" in keys else None}
    return f, raw


def launch(conn, row, snap, now: float) -> tuple[dict, dict]:
    keys = row.keys()

    def g(k):
        return row[k] if k in keys else None

    f = {"lscore": b(g("score"), (40, 60, 80), ("<40", "40-60", "60-80", "80+")),
         "lrisk": b(g("risk"), (20, 40, 60), ("<20", "20-40", "40-60", "60+")),
         "bundle": "ja" if g("bundle_flag") else "nein", "insider": "ja" if g("insider_flag") else "nein",
         "devl": b(g("dev_launches"), (1, 3, 10), ("0", "1-2", "3-9", "10+")),
         "top10": b(g("top10_pct"), (20, 35, 50), ("<20%", "20-35%", "35-50%", ">50%")),
         "socials": "ja" if g("has_socials") else "nein",
         "lsmart": "ja" if (g("n_ultra") or 0) + (g("n_smart") or 0) > 0 else "nein",
         "modelp": b(g("model_p"), (0.15, 0.3), ("<15%", "15-30%", ">30%"))}
    dec = g("entered_ts") or g("decided_ts") or now
    delay = now - dec
    f["delay"] = b(delay, (5, 15, 30), ("<5s", "5-15s", "15-30s", ">30s"))
    if snap is not None:
        f["mcap"] = b(snap.fdv, (1e4, 3e4, 1e5), ("<10k", "10-30k", "30-100k", ">100k"))
        f["liq"] = b(snap.liquidity_usd, (5e3, 2e4), ("<5k", "5-20k", ">20k"))
    f.update(market_feats(conn, row["mint"], snap, now, "launch"))
    try:
        reasons = json.loads(g("reasons_json") or "[]")[:2]
    except (TypeError, ValueError):
        reasons = []
    raw = {"score": g("score"), "risk": g("risk"), "delay_s": round(delay, 1), "reasons": reasons}
    return f, raw
