"""Market features at a point in time (kind 'market', key 'solana'): read from market/ snapshots (<= ts) and the
sniper's launch statistics. market/ owns the computation; this only flattens it for the brain."""
from intel.store import has

FEATURES = {
    "m_phase": ("market", "Marktphase 0–100", "n"),
    "m_hot": ("market", "Phase heiß (1) / sonst 0", "n"),
    "m_risk_off": ("market", "Phase Risk-off (1) / sonst 0", "n"),
    "m_volume_h1_usd": ("market", "Memecoin-Volumen 1 h (Top 100)", "usd"),
    "m_volume_ratio": ("market", "Volumen ggü. 24-h-Median", "x"),
    "m_sol_ret_1h": ("market", "SOL 1 h", "pct"),
    "m_sol_ret_24h": ("market", "SOL 24 h", "pct"),
    "m_sol_ret_7d": ("market", "SOL 7 Tage", "pct"),
    "m_breadth": ("market", "Breite (Coins im Plus)", "pct"),
    "m_launches_1h": ("market", "Launches pro Stunde", "n"),
    "m_launch_hit2x": ("market", "Launches mit 2x in 60 min", "pct"),
    "m_launch_rug": ("market", "Launches mit Rug", "pct"),
    "m_launch_migrated": ("market", "Launches migriert", "pct"),
    "m_smart_net_usd": ("market", "Smart Money netto 1 h", "usd"),
    "m_top_sector_heat": ("market", "Hitze des heißesten Sektors", "n"),
    "m_top_term_heat": ("market", "Hitze des heißesten Narrativs", "n"),
    "m_sector_rotation": ("market", "Kapitalrotation (größte Anteilsverschiebung)", "pts"),
}
COLS = {"m_phase": "score", "m_volume_h1_usd": "volume_h1_usd", "m_volume_ratio": "volume_ratio",
        "m_sol_ret_1h": "sol_ret_1h", "m_sol_ret_24h": "sol_ret_24h", "m_sol_ret_7d": "sol_ret_7d", "m_breadth": "breadth",
        "m_launches_1h": "launches_1h", "m_launch_hit2x": "launch_hit2x", "m_launch_rug": "launch_rug",
        "m_launch_migrated": "launch_migrated", "m_smart_net_usd": "smart_net_usd"}


def market_features(conn, ts, max_age=1800):
    ts = int(ts)
    out = {k: None for k in FEATURES}
    meta = {}
    if has(conn, "market_snapshots"):
        s = conn.execute("SELECT * FROM market_snapshots WHERE ts<=? AND ts>=? ORDER BY ts DESC LIMIT 1",
                         (ts, ts - max_age)).fetchone()
        if s:
            for k, c in COLS.items():
                out[k] = s[c]
            out["m_hot"] = int(s["label"] == "hot") if s["label"] else None
            out["m_risk_off"] = int(s["label"] == "risk_off") if s["label"] else None
            meta.update(label=s["label"], snapshot_ts=s["ts"], top_sector=s["top_sector"], top_term=s["top_term"])
            sec = conn.execute("SELECT MAX(heat), MAX(ABS(vol_share_chg)) FROM market_sectors WHERE ts=?", (s["ts"],)).fetchone() \
                if has(conn, "market_sectors") else None
            if sec:
                out["m_top_sector_heat"] = sec[0]
                out["m_sector_rotation"] = None if sec[1] is None else 100 * sec[1]
            if has(conn, "market_terms"):
                t = conn.execute("SELECT MAX(heat) FROM market_terms WHERE ts=?", (s["ts"],)).fetchone()
                out["m_top_term_heat"] = t[0] if t else None
    known = sum(v is not None for v in out.values())
    cov = round(known / len(out), 3)
    return {"features": out, "coverage": {"market": cov, "all": cov}, "meta": meta}
