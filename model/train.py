"""Retraining: re-tier all wallets, rebuild point-in-time events, walk-forward backtest, fit, save a new
model version with its report. Run on a schedule so the model evolves with new outcomes."""
import json
import logging
import time
from pathlib import Path

from model import learn
from model.config import msettings
from model.tiers import tier_all
from research import backtest, data, report, wallet_stats
from research.store import connect, ensure_schema, retry, save_report

log = logging.getLogger("model.train")
REPORT_DIR = Path(__file__).resolve().parent.parent / "data" / "reports"


def _previous(conn):
    v = conn.execute("SELECT * FROM model_versions WHERE active=1 ORDER BY id DESC LIMIT 1").fetchone()
    if not v:
        return None
    m = json.loads(v["metrics_json"] or "{}")
    return dict(version=v["id"], tier_counts=json.loads(v["tier_counts_json"] or "{}"),
                auc=(m.get("1h") or {}).get("auc"))


def run_once(conn=None, now=None, write_files=True):
    conn = ensure_schema(conn) if conn is not None else connect()
    now = int(now or time.time())
    t0 = time.time()
    ds = data.load(conn)
    if not ds.trades:
        log.warning("no trades in the database yet")
        return None
    bt = backtest.run(ds, msettings)
    # production weights: all wallet buys whose outcome is known by now
    known = [(we["comps"], we["y"]) for we in bt["wallet_events"] if we["ts"] + we["sec"] <= now]
    weights, winfo = learn.learn_wallet_weights(known)
    st, _ = wallet_stats.compute(ds, now + 1)
    tiers = tier_all(st, weights)
    champ = {}
    v = conn.execute("SELECT token_model_json FROM model_versions WHERE active=1 ORDER BY id DESC LIMIT 1").fetchone()
    if v:
        champ = {h: m.get("cfg") for h, m in json.loads(v[0] or "{}").items() if m.get("cfg")}
    models, selection = learn.select_models(bt["events"], champion=champ, now=now)
    bt.setdefault("metrics", {})["selection"] = selection
    for line in selection_log(selection):
        log.info(line)
    tiered = [w for w, (t, _, _) in tiers.items() if t]
    cl = wallet_stats.clusters(ds, tiered, now + 1)
    counts = {t: sum(1 for v in tiers.values() if v[0] == t) for t in ("ultra", "smart", "good")}
    prev = _previous(conn)
    cur = conn.execute(
        """INSERT INTO model_versions(ts, active, n_events, n_wallets, tier_counts_json, wallet_weights_json,
               token_model_json, tier_rules_json, metrics_json, notes) VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (now, 0, len(bt["events"]), len(st), json.dumps(counts), json.dumps(dict(weights=weights, info=winfo)),
         json.dumps(models), json.dumps(msettings.rules_dict()), json.dumps(bt.get("metrics", {}), default=float),
         f"{len(bt['checkpoints'])} checkpoints, {time.time() - t0:.0f}s"))
    vid = cur.lastrowid
    rows = []
    for w, (tier, sc, s) in tiers.items():
        rows.append((w, vid, now, tier, sc, s["n_tokens"], s["win_rate"], s["early_rate"], s["roi"],
                     s["realized_pnl_usd"], s["total_pnl_usd"], s["median_entry_mcap"], s["median_min_to_2x"],
                     s["influence_followers"], s["influence_usd"], s["inflow_lift_usd"], s["consistency"],
                     s["rug_rate"], json.dumps(s["components"]), cl.get(w), s.get("exit_capture"),
                     s.get("median_hold_min")))
    conn.commit()

    def swap():  # one short write transaction, retried if the live app holds the lock
        conn.execute("DELETE FROM model_wallet_stats")
        conn.executemany("""INSERT INTO model_wallet_stats(address, version, ts, tier, score, n_tokens, win_rate, early_rate,
               roi, realized_pnl_usd, total_pnl_usd, median_entry_mcap, median_min_to_2x, influence_followers,
               influence_usd, inflow_lift_usd, consistency, rug_rate, components_json, cluster_id, exit_capture,
               median_hold_min) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", rows)
        conn.executemany("INSERT OR REPLACE INTO model_tier_history VALUES (?,?,?,?)",
                         [(vid, w, t, sc) for w, (t, sc, _) in tiers.items() if t])
        conn.execute("UPDATE model_versions SET active = (id = ?)", (vid,))
        conn.commit()
    retry(swap, conn=conn)
    md, rep = report.build(conn, vid, bt, tiers, weights, winfo, models, prev)
    save_report(conn, f"Modell v{vid}", md, rep, vid)
    if write_files:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        (REPORT_DIR / f"research_v{vid}.md").write_text(md)
        (REPORT_DIR / "latest.md").write_text(md)
        (REPORT_DIR / "latest.json").write_text(json.dumps(rep, default=float, indent=1))
    log.info("model v%d: %d wallets scored, tiers %s, %d events, %.0fs", vid, len(st), counts, len(bt["events"]),
             time.time() - t0)
    return vid


def _cfg(c):
    return f"{c.get('algo')}" + (f"-{c['drop']}" if c.get("drop") else "")


def selection_log(selection):
    """One log line per horizon and candidate: chosen, lost in the holdout (both log-losses), not trainable,
    or why the horizon stayed on the prior."""
    out = []
    for h, r in selection.items():
        if r.get("status") == "prior":
            why = ("no outcomes for this horizon yet" if not r.get("n") else
                   f"need >= {r.get('need_n')} labelled events and >= {r.get('need_pos')} pumps")
            if r.get("n") and r.get("pos") == r.get("n"):
                why = "every event pumped, nothing to separate"
            out.append(f"selection {h}: prior (n {r.get('n')}, pumps {r.get('pos')}): {why}")
            continue
        ch = r.get("chosen") or {}
        hold = r.get("holdout") or {}
        out.append(f"selection {h}: champion {_cfg(ch)} ({r.get('why')}), n {r.get('n')} "
                   f"(train {r.get('n_train')}, holdout {r.get('n_test')}), holdout logloss "
                   f"{hold.get('logloss', float('nan')):.4f} vs base rate {r.get('base_logloss', float('nan')):.4f}")
        if r.get("skipped"):
            out.append(f"selection {h}: no candidate evaluated: {r['skipped']}")
        for c in r.get("candidates") or []:
            if c["cfg"] == ch:
                continue
            if "error" in c:
                out.append(f"selection {h}: {_cfg(c['cfg'])} rejected: not trainable ({c['error']})")
            else:
                out.append(f"selection {h}: {_cfg(c['cfg'])} rejected: lost in holdout, logloss {c['logloss']:.4f} "
                           f"vs champion {hold.get('logloss', float('nan')):.4f} (auc {c.get('auc') or 0:.3f})")
    return out


def last_success(conn):
    """Unix time of the active model version (only a fully successful retrain activates its version)."""
    try:
        r = conn.execute("SELECT MAX(ts) FROM model_versions WHERE active=1").fetchone()
    except Exception:  # noqa: BLE001  (fresh DB without the table)
        return 0.0
    return float(r[0] or 0)


def _utc(ts):
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ts))


def _champions(conn, vid):
    v = conn.execute("SELECT token_model_json FROM model_versions WHERE id=?", (vid,)).fetchone()
    models = json.loads(v[0] or "{}") if v else {}
    return " ".join(f"{h}={(m.get('cfg') or {}).get('algo', m.get('kind'))}" for h, m in models.items())


def retrain_if_due(conn, every, last_ok):
    """Retrain when the last successful one is older than `every` seconds (or there never was one).
    Returns the new last-success time; a failure keeps the old one so the next cycle tries again."""
    if time.time() - last_ok < every:
        return last_ok
    from core.db import retry_locked
    t0 = time.time()
    try:
        vid = retry_locked(conn, lambda: run_once(conn), attempts=2)
    except Exception:  # noqa: BLE001
        conn.rollback()
        log.exception("retrain failed after %.0f s, retrying next cycle", time.time() - t0)
        return last_ok
    if vid:
        log.info("retrain finished in %.0f s, model v%d, champion %s", time.time() - t0, vid, _champions(conn, vid))
        last_ok = time.time()
        log.info("next retrain at %s", _utc(last_ok + every))
    return last_ok


def loop(every_hours=None, discover=False):
    every = (every_hours or msettings.retrain_hours) * 3600
    last_seed = 0.0
    from research.config import rsettings
    conn = connect()
    last_ok = last_success(conn)
    log.info("service start: last successful retrain %s, next retrain at %s",
             _utc(last_ok) if last_ok else "never", _utc(max(time.time(), last_ok + every)))
    while True:
        last_ok = retrain_if_due(conn, every, last_ok)  # first, so a due retrain is not held back by discovery
        if discover and time.time() - last_seed >= rsettings.seed_every_hours * 3600:
            try:
                from research import public_seeds
                log.info("public seeds: %s", public_seeds.run(conn))
            except Exception:  # noqa: BLE001
                log.exception("public seeding failed")
            last_seed = time.time()
        if discover:
            try:
                from research import discovery
                discovery.run_once(conn)
            except Exception:  # noqa: BLE001
                log.exception("discovery failed")
        try:
            from model.predict import update_outcomes
            update_outcomes(conn)
        except Exception:  # noqa: BLE001
            log.exception("outcome update failed")
        time.sleep(rsettings.discover_every_min * 60 if discover else min(every, 1800))
