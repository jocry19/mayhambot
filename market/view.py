"""Everything the dashboard's market page shows, in one read-only call (web/ calls market.view)."""
import json
import time

from market import dynamics, evaluate, narratives, store
from market.analysis import LABELS, PHASE_WHY
from market.config import settings as default_cfg

SERVICE_STALE_S = 900


def _j(s, d=None):
    try:
        return json.loads(s) if s else d
    except (TypeError, ValueError):
        return d


def view(conn, hours=24, now=None, cfg=default_cfg):
    now = int(now or time.time())
    if not store.has(conn, "market_snapshots"):
        return {"available": False, "note": "Die Marktanalyse hat noch keine Daten (Dienst market läuft noch nicht)."}
    hb = store.get_state(conn, "service_status") or {}
    hb = hb if isinstance(hb, dict) else {}
    last = conn.execute("SELECT * FROM market_snapshots ORDER BY ts DESC LIMIT 1").fetchone()
    if not last:
        return {"available": False, "note": "Noch kein Markt-Snapshot.", "service": hb}
    last = dict(last)
    comps = _j(last.pop("components_json"), {})
    since = now - int(hours * 3600)
    history = [dict(r) for r in conn.execute(
        "SELECT ts, score, label, coverage, sol_usd, breadth, launches_1h, launch_hit2x, launch_rug, smart_net_usd, "
        "volume_h1_usd, backfilled FROM market_snapshots WHERE ts>=? ORDER BY ts", (since,))]
    secs = [dict(r) for r in conn.execute("SELECT * FROM market_sectors WHERE ts=? ORDER BY heat DESC", (last["ts"],))]
    for s in secs:
        s["label"] = narratives.SECTOR_LABEL.get(s["sector"], s["sector"])
        s["top"] = _j(s.pop("top_json"), [])
    # sector heat and volume share over time (for the rotation chart)
    sec_hist = {}
    for r in conn.execute("SELECT ts, sector, heat, vol_share, smart_net_usd FROM market_sectors WHERE ts>=? ORDER BY ts", (since,)):
        sec_hist.setdefault(r["sector"], []).append({"ts": r["ts"], "heat": r["heat"], "vol_share": r["vol_share"],
                                                     "smart_net_usd": r["smart_net_usd"]})
    terms = [dict(r) for r in conn.execute("SELECT * FROM market_terms WHERE ts=? ORDER BY heat DESC", (last["ts"],))]
    for t in terms:
        t["mints"] = _j(t.pop("mints_json"), [])
    first_seen = {r[0]: r[1] for r in conn.execute(
        "SELECT term, MIN(ts) FROM market_terms WHERE ts>=? GROUP BY term", (now - 3 * 86400,))}
    for t in terms:
        t["since"] = first_seen.get(t["term"])
    bks = [dict(r) for r in conn.execute(
        "SELECT * FROM market_buckets WHERE ts=(SELECT MAX(ts) FROM market_buckets) ORDER BY kind, rowid")]
    streaks = dynamics.rotation_streaks(conn, last["ts"])
    for s in secs:
        s["streak"] = streaks.get(s["sector"])
    for t in terms:
        t["stage_de"] = dynamics.STAGES.get(t.get("stage"), None)
    dyn = store.get_state(conn, "dynamics") or {}
    dyn = dyn if isinstance(dyn, dict) else {}
    lw = store.get_state(conn, "learned_weights") or None
    rotation = rotation_text(secs)
    return {
        "available": True, "now": now,
        "service": dict(hb, up=bool(hb.get("ts")) and now - hb["ts"] <= SERVICE_STALE_S),
        "phase": dict(last, label_de=LABELS.get(last["label"], "–"), age_s=now - last["ts"],
                      components=[dict(v, key=k, why=v.get("why") or PHASE_WHY.get(k, k)) for k, v in
                                  sorted(comps.items(), key=lambda kv: -kv[1]["weight"])]),
        "thresholds": {"risk_off": cfg.riskoff_below, "risk_on": cfg.riskon_above, "hot": cfg.hot_above},
        "history": history, "sectors": secs, "sector_history": sec_hist, "terms": terms, "buckets": bks,
        "rotation": rotation, "evaluation": evaluate.evaluate(conn, days=14, now=now, cfg=cfg),
        "dynamics": dyn, "learned_weights": lw, "learned_active": bool(cfg.learned_weights and isinstance(lw, dict) and lw.get("adopt")),
        "switches": {"signal_weight": cfg.signal_weight, "sniper_gate": cfg.sniper_gate,
                     "signal_max_adj": cfg.signal_max_adj, "sniper_riskoff_bump": cfg.sniper_riskoff_bump,
                     "sniper_hot_term_bonus": cfg.sniper_hot_term_bonus, "learned_weights": cfg.learned_weights},
    }


def rotation_text(secs):
    """Plain sentences: which sectors gain volume share and which lose it (vs about 1 h earlier)."""
    moved = [s for s in secs if s.get("vol_share_chg") is not None and s["sector"] != narratives.OTHER]
    if not moved:
        return []
    up = sorted((s for s in moved if s["vol_share_chg"] > 0.005), key=lambda s: -s["vol_share_chg"])[:3]
    down = sorted((s for s in moved if s["vol_share_chg"] < -0.005), key=lambda s: s["vol_share_chg"])[:3]
    out = []
    if up:
        out.append("Mehr Volumen-Anteil: " + ", ".join(f"{s['label']} +{100 * s['vol_share_chg']:.1f} Pkt" for s in up))
    if down:
        out.append("Weniger: " + ", ".join(f"{s['label']} {100 * s['vol_share_chg']:.1f} Pkt" for s in down))
    smart = sorted((s for s in secs if (s.get("smart_net_usd") or 0) > 0), key=lambda s: -s["smart_net_usd"])[:2]
    if smart:
        out.append("Smart Money kauft netto vor allem: " + ", ".join(f"{s['label']} ${s['smart_net_usd']:,.0f}" for s in smart))
    return out
