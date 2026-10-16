"""Read side for other packages: market phase, sector heat and narrative context of one coin, as of a point in
time. Read-only and cheap (a few indexed queries, no network)."""
import json
import math
import time

from market import narratives, store
from market.analysis import LABELS, net_flow
from market.config import settings as default_cfg


def _sector_rows(conn, snap_ts):
    if not store.has(conn, "market_sectors"):
        return {}
    return {r["sector"]: dict(r) for r in conn.execute("SELECT * FROM market_sectors WHERE ts=?", (snap_ts,))}


def _term_rows(conn, snap_ts):
    if not store.has(conn, "market_terms"):
        return {}
    return {r["term"]: dict(r) for r in conn.execute("SELECT * FROM market_terms WHERE ts=?", (snap_ts,))}


def _coin_row(conn, mint):
    r = None
    if store.has(conn, "market_tokens"):
        r = conn.execute("SELECT * FROM market_tokens WHERE mint=?", (mint,)).fetchone()
    if r is None and store.has(conn, "tokens"):
        r = conn.execute("SELECT mint, symbol, name, mcap_usd, liquidity_usd, pair_created_at, price_change_h1, "
                         "price_change_h24, volume_h1, last_updated AS updated_at FROM tokens WHERE mint=?", (mint,)).fetchone()
    if r is None and store.has(conn, "sniper_launches"):
        r = conn.execute("SELECT mint, symbol, name, created_ts AS pair_created_at FROM sniper_launches WHERE mint=?",
                         (mint,)).fetchone()
    return dict(r) if r else None


def phase(conn, ts=None, max_age=None):
    """Market phase as of ts: {ts, age_s, score 0-100, label, label_de, coverage, components, sol_*, breadth, ...}
    or None (no snapshot yet, or older than max_age seconds)."""
    ts = int(ts or time.time())
    s = store.snapshot_at(conn, ts, max_age)
    if not s:
        return None
    s["age_s"] = ts - s["ts"]
    s["label_de"] = LABELS.get(s.get("label"), "–")
    return s


def coin_context(conn, mint, ts=None, name=None, symbol=None, cfg=default_cfg):
    """Market context of one coin as of ts:
    {mint, sector, sector_label, sector_hits, sector_heat, sector_rank, sector_n, sector_median_ret_h1,
     rs_sector_h1 (coin 1 h change minus the sector median, points), terms [{term, heat, lift, hit2x}],
     term_heat (hottest matching narrative), phase {score, label, label_de, age_s}, age_h, liq_mcap,
     net_flow_usd, fresh (coin row written at or before ts, at most 30 min old)}"""
    ts = int(ts or time.time())
    row = _coin_row(conn, mint) or {"mint": mint}
    name = name if name is not None else row.get("name")
    symbol = symbol if symbol is not None else row.get("symbol")
    primary, hits = narratives.classify(name, symbol, row.get("description"))
    snap = store.snapshot_at(conn, ts, cfg.max_age_sec * 4)
    out = {"mint": mint, "symbol": symbol, "name": name, "sector": primary,
           "sector_label": narratives.SECTOR_LABEL.get(primary, primary), "sector_hits": hits, "terms": [],
           "phase": None, "sector_heat": None}
    # point in time: only a coin row written at or before ts and at most 30 min old counts; a row written after ts
    # is the future for this ts and must not leak into measurement, walk-forward or model features
    upd = row.get("updated_at")
    fresh = upd is not None and 0 <= ts - upd <= 1800
    out["fresh"] = fresh
    pc = row.get("pair_created_at")
    out["age_h"] = round((ts - pc) / 3600, 2) if pc and pc <= ts else None
    mc, liq = row.get("mcap_usd"), row.get("liquidity_usd")
    out["liq_mcap"] = round(liq / mc, 4) if fresh and mc and liq else None
    out["net_flow_usd"] = net_flow(row) if fresh else None
    if not snap:
        return out
    out["phase"] = {"ts": snap["ts"], "score": snap["score"], "label": snap["label"],
                    "label_de": LABELS.get(snap["label"], "–"), "age_s": ts - snap["ts"],
                    "trend": snap.get("trend"), "slope_1h": snap.get("slope_1h")}
    secs = _sector_rows(conn, snap["ts"])
    ranked = sorted((s for s in secs.values() if s.get("heat") is not None), key=lambda s: -s["heat"])
    sec = secs.get(primary)
    if sec:
        out.update(sector_heat=sec["heat"], sector_n=sec["n_tokens"], sector_median_ret_h1=sec["median_ret_h1"],
                   sector_vol_share=sec["vol_share"], sector_vol_share_chg=sec["vol_share_chg"],
                   sector_smart_net_usd=sec["smart_net_usd"],
                   sector_rank=next((i + 1 for i, s in enumerate(ranked) if s["sector"] == primary), None),
                   sectors_ranked=len(ranked))
        if fresh and row.get("price_change_h1") is not None and sec["median_ret_h1"] is not None:
            out["rs_sector_h1"] = round(row["price_change_h1"] - sec["median_ret_h1"], 2)
    terms = _term_rows(conn, snap["ts"])
    mine = [terms[t] for t in narratives.terms_of(name, symbol) if t in terms]
    mine.sort(key=lambda t: -(t["heat"] or 0))
    out["terms"] = [{"term": t["term"], "heat": t["heat"], "lift": t["lift"], "hit2x": t["hit2x"],
                     "n_recent": t["n_recent"], "stage": t.get("stage"), "age_h": t.get("age_h")} for t in mine[:5]]
    out["term_heat"] = mine[0]["heat"] if mine else None
    return out


FEATURES = ("mkt_phase", "mkt_sol_ret_24h", "mkt_breadth", "mkt_launch_hit2x", "mkt_smart_net",
            "mkt_sector_heat", "mkt_sector_share_chg", "mkt_rs_sector_h1", "mkt_term_heat", "mkt_hot_term",
            "mkt_phase_slope_1h", "mkt_regime_age_h", "mkt_term_early")


def features(conn, mint, ts=None, name=None, symbol=None):
    """Flat numeric model inputs as of ts (None = unknown). Point in time: only snapshots at or before ts.
    Names are stable (FEATURES); the model thread decides whether to use them."""
    c = coin_context(conn, mint, ts, name=name, symbol=symbol)
    p = store.snapshot_at(conn, ts or int(time.time()), default_cfg.max_age_sec * 4) or {}
    sn = p.get("smart_net_usd")
    return {
        "mkt_phase": p.get("score"), "mkt_sol_ret_24h": p.get("sol_ret_24h"), "mkt_breadth": p.get("breadth"),
        "mkt_launch_hit2x": p.get("launch_hit2x"),
        "mkt_smart_net": None if sn is None else math.copysign(math.log10(1 + abs(sn)), sn),
        "mkt_sector_heat": c.get("sector_heat"), "mkt_sector_share_chg": c.get("sector_vol_share_chg"),
        "mkt_rs_sector_h1": c.get("rs_sector_h1"), "mkt_term_heat": c.get("term_heat"),
        "mkt_hot_term": None if c.get("phase") is None else int((c.get("term_heat") or 0) >= 60),
        "mkt_phase_slope_1h": p.get("slope_1h"),
        "mkt_regime_age_h": None if not p.get("regime_since") else round((p["ts"] - p["regime_since"]) / 3600, 2),
        "mkt_term_early": None if c.get("phase") is None else int(bool(c.get("terms")) and
                                                                   c["terms"][0].get("stage") in ("new", "growing")),
    }


def signal_factor(conn, mint, ts=None, cfg=default_cfg):
    """Multiplier for a smart-money signal score from market phase (60 %) and the coin's sector heat (40 %),
    within 1 +- MARKET_SIGNAL_MAX_ADJ. None when MARKET_SIGNAL_WEIGHT is off or the snapshot is too old."""
    if not cfg.signal_weight:
        return None
    ts = int(ts or time.time())
    c = coin_context(conn, mint, ts, cfg=cfg)
    p = c.get("phase")
    if not p or p["score"] is None or p["age_s"] > cfg.max_age_sec:
        return None
    x = 0.6 * (p["score"] - 50) / 50
    heat = c.get("sector_heat")
    if heat is not None:
        x += 0.4 * (heat - 50) / 50
    x = max(-1.0, min(1.0, x))
    f = round(1 + cfg.signal_max_adj * x, 3)
    why = f"Markt {p['label_de']} ({p['score']:.0f})"
    if heat is not None:
        why += f", Sektor {c['sector_label']} Hitze {heat:.0f}"
    return {"factor": f, "phase": p["score"], "label": p["label"], "sector": c["sector"], "sector_heat": heat, "why": why}


def sniper_adjust(conn, name, symbol, ts=None, cfg=default_cfg):
    """Change of the sniper's minimum score for one launch. With MARKET_SNIPER_GATE: + MARKET_SNIPER_RISKOFF_BUMP
    in a risk-off market, - MARKET_SNIPER_HOT_TERM_BONUS when the name carries a hot narrative whose launches did
    better than average. With MARKET_SNIPER_NEW_TERM: + MARKET_SNIPER_NEW_TERM_BUMP when the name's strongest
    narrative is brand new (stage 'new'; such launches doubled about half as often in the walk-forward).
    {"delta": 0} when both switches are off or the snapshot is too old."""
    out = {"delta": 0.0, "why": "", "label": None}
    if not (cfg.sniper_gate or getattr(cfg, "sniper_new_term", False)):
        return out
    ts = int(ts or time.time())
    p = store.snapshot_at(conn, ts, cfg.max_age_sec)
    if not p:
        return out
    out["label"] = p["label"]
    why = []
    terms = _term_rows(conn, p["ts"])
    mine = sorted((terms[t] for t in narratives.terms_of(name, symbol) if t in terms), key=lambda r: -(r["heat"] or 0))
    if cfg.sniper_gate:
        if p["label"] == "risk_off":
            out["delta"] += cfg.sniper_riskoff_bump
            why.append(f"Markt Risk-off ({p['score']:.0f}): Mindest-Score +{cfg.sniper_riskoff_bump:.0f}")
        base = p.get("launch_hit2x")
        for r in mine:
            if (r["heat"] or 0) >= 60 and r["hit2x"] is not None and base and r["hit2x"] > base:
                out["delta"] -= cfg.sniper_hot_term_bonus
                why.append(f"heißes Narrativ '{r['term']}' (Hitze {r['heat']:.0f}): Mindest-Score -{cfg.sniper_hot_term_bonus:.0f}")
                break
    if getattr(cfg, "sniper_new_term", False) and mine and mine[0].get("stage") == "new":
        out["delta"] += cfg.sniper_new_term_bump
        why.append(f"brandneues Narrativ '{mine[0]['term']}': Mindest-Score +{cfg.sniper_new_term_bump:.0f}")
    out["why"] = "; ".join(why)
    return out


def summary(conn, ts=None):
    """One line for cli/ and the overview: phase, top sector and narrative, data age."""
    p = phase(conn, ts)
    if not p:
        return None
    hb = store.get_state(conn, "service_status") or {}
    return {"ts": p["ts"], "age_s": p["age_s"], "score": p["score"], "label": p["label"], "label_de": p["label_de"],
            "coverage": p["coverage"], "top_sector": p.get("top_sector"),
            "top_sector_label": narratives.SECTOR_LABEL.get(p.get("top_sector"), p.get("top_sector")),
            "top_term": p.get("top_term"), "sol_ret_24h": p.get("sol_ret_24h"), "breadth": p.get("breadth"),
            "service_ts": hb.get("ts") if isinstance(hb, dict) else None}


def _j(s, default=None):
    try:
        return json.loads(s) if s else default
    except (TypeError, ValueError):
        return default
