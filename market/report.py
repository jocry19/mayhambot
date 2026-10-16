"""Hourly self-report reports/market_report.json next to the DB (data/ on the Mac), for threads that may read
files and logs but not the DB: does the market analysis predict anything yet (walk-forward per lens, component
power, sector lead), what the switches are and what the sniper gate would have blocked. Small, no secrets."""
import json
import time
from pathlib import Path

from market import evaluate, store

LENSES = ("by_phase", "by_sector_heat", "by_term", "by_term_stage", "new_term")


def report_path(db_path=None):
    from core.config import settings
    return Path(db_path or settings.db_path).parent / "reports" / "market_report.json"


def _holds(wf, lens, best=None, worst=None):
    """A lens carries a switch only when the older half itself was clear (z >= 2), the newer half confirmed it
    (z >= 2 on unseen data) and the direction is the one the switch acts on."""
    v = wf.get(lens) or {}
    return bool(v.get("ok") and ((v.get("train") or {}).get("z") or 0) >= 2
                and (best is None or v.get("best") == best) and (worst is None or v.get("worst") == worst))


def verdict(ev, dyn):
    """Plain verdict per switch: on only where the walk-forward confirmed exactly the rule that switch applies."""
    wf = ev.get("walk_forward") or {}
    learned = (((dyn or {}).get("components") or {}).get("learned") or {})
    return {
        "signal_weight": _holds(wf, "by_phase", best="Risk-on") and _holds(wf, "by_sector_heat", best="heiß ≥60"),
        "sniper_gate": _holds(wf, "by_phase", worst="Risk-off"),
        "sniper_hot_term": _holds(wf, "by_term", best="mit heißem Narrativ"),
        "sniper_new_term": _holds(wf, "new_term", worst="neues Narrativ"),
        "learned_weights": bool(learned.get("adopt")),
    }


def build(conn, now=None, cfg=None, days=14):
    from market.config import settings
    cfg = cfg or settings
    now = int(now or time.time())
    ev = evaluate.evaluate(conn, days=days, now=now, cfg=cfg)
    dyn = store.get_state(conn, "dynamics") or {}
    la = ev.get("launches") or {}
    comp = dyn.get("components") or {}
    snap = store.snapshot_at(conn, now, cfg.max_age_sec) or {}
    return {
        "ts": now, "days": days, "snapshots": ev.get("snapshots"),
        "phase": {k: snap.get(k) for k in ("ts", "score", "label", "regime", "trend", "slope_1h", "coverage")},
        "launches": {"n": la.get("n"), "base_hit_rate": la.get("base_hit_rate"),
                     **{lens: la.get(lens) for lens in LENSES}, "gate": la.get("gate")},
        "signals": ev.get("signals"),
        "significant": ev.get("significant"),
        "walk_forward": ev.get("walk_forward"),
        "components": {"n_hours": comp.get("n_hours"),
                       "rows": {k: {x: v.get(x) for x in ("ic", "ic_old", "ic_new", "confirmed", "n_hours")}
                                for k, v in (comp.get("rows") or {}).items()},
                       "learned": comp.get("learned")},
        "sector_lead": dyn.get("sector_lead"),
        "switches": {"signal_weight": cfg.signal_weight, "sniper_gate": cfg.sniper_gate,
                     "sniper_new_term": getattr(cfg, "sniper_new_term", False),
                     "learned_weights": getattr(cfg, "learned_weights", False)},
        "walk_forward_says": verdict(ev, dyn),
        "note": ev.get("note"),
    }


def write(conn, path, now=None, cfg=None):
    rep = build(conn, now, cfg)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(rep, ensure_ascii=False, indent=1, default=str))
    tmp.replace(path)
    return rep


def line(rep):
    """One log line: what the walk-forward says per lens."""
    wf = rep.get("walk_forward") or {}
    parts = [f"{k.replace('by_', '')}: {'ja' if v.get('ok') else 'nein'}" for k, v in wf.items()]
    says = [k for k, v in rep["walk_forward_says"].items() if v]
    return (f"Taugt die Marktanalyse? {rep['launches']['n'] or 0} Launches mit Ergebnis, "
            f"Walk-forward {', '.join(parts) or 'noch nichts'}; einschalten laut Messung: {', '.join(says) or 'nichts'}")
