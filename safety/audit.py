"""Did the protection actually protect? (read-only, no network)

For every buy the bot made (copy trader `ct_positions`, sniper `sniper_positions`) and every signal (`signals`):
the safety level at that moment (the row's own safety_level when the buyer stored it, else the last
safety_history entry before the buy) and the result. Three numbers matter:
  * leaks  - bought although the coin was blocked at that moment (level critical or manual block)
  * blind  - bought without any safety result (never checked or 'unchecked')
  * result per level - average P&L and share of heavy losses (<= -50 %), so the penalty curve can be checked
    against money instead of rugs.
"""
from safety.config import settings as default_settings

HEAVY_LOSS = -0.5
SOURCES = {
    # table: (time column, P&L fraction column, where)
    "copytrade": ("ct_positions", "opened_at", "pnl_pct", "status != 'open'"),
    "sniper": ("sniper_positions", "opened_ts", "pnl_pct", "status != 'open'"),
    "signals": ("signals", "ts", "ret_1h", "1=1"),
}


def _cols(conn, table):
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    except Exception:  # noqa: BLE001
        return set()


def _level_at(conn, mint, ts):
    r = conn.execute("SELECT level FROM safety_history WHERE mint=? AND ts<=? ORDER BY ts DESC LIMIT 1",
                     (mint, ts)).fetchone()
    return r[0] if r else None


def _blocked_override(conn, mint, ts):
    r = conn.execute("SELECT action, ts FROM safety_overrides WHERE mint=?", (mint,)).fetchone()
    return bool(r and r[0] == "block" and (r[1] or 0) <= ts)


def audit(conn, since=None, cfg=None):
    """{source: {n, leaks, leak_mints, blind, levels: {level: {n, avg_pnl, heavy_loss_share, sum_pnl}}}}"""
    cfg = cfg or default_settings
    has_hist = bool(_cols(conn, "safety_history"))
    has_ov = bool(_cols(conn, "safety_overrides"))
    out = {}
    for name, (table, tcol, pcol, where) in SOURCES.items():
        cols = _cols(conn, table)
        if not cols or not {"mint", tcol, pcol} <= cols:
            continue
        lvl_col = "safety_level" if "safety_level" in cols else "NULL"
        q = f"SELECT mint, {tcol}, {pcol}, {lvl_col} FROM {table} WHERE {where}"
        args = ()
        if since:
            q += f" AND {tcol} >= ?"
            args = (since,)
        rows = conn.execute(q, args).fetchall()
        if not rows:
            continue
        levels, leaks, blind = {}, [], 0
        for mint, ts, pnl, lvl in rows:
            ts = int(ts or 0)
            if not lvl and has_hist:
                lvl = _level_at(conn, mint, ts)
            lvl = lvl or "unchecked"
            blocked = lvl == "critical" or (has_ov and _blocked_override(conn, mint, ts))
            if blocked:
                leaks.append(mint)
            if lvl == "unchecked":
                blind += 1
            d = levels.setdefault(lvl, {"n": 0, "with_result": 0, "sum_pnl": 0.0, "heavy": 0})
            d["n"] += 1
            if pnl is not None:
                d["with_result"] += 1
                d["sum_pnl"] += pnl
                d["heavy"] += 1 if pnl <= HEAVY_LOSS else 0
        for d in levels.values():
            k = d.pop("with_result")
            d["avg_pnl"] = round(d["sum_pnl"] / k, 4) if k else None
            d["heavy_loss_share"] = round(d.pop("heavy") / k, 3) if k else None
            d["sum_pnl"] = round(d["sum_pnl"], 4)
            d["n_result"] = k
        out[name] = {"n": len(rows), "leaks": len(leaks), "leak_mints": sorted(set(leaks))[:20], "blind": blind,
                     "pnl_field": pcol, "levels": levels}
    return out


def report_text(a):
    if not a:
        return "Noch keine Käufe oder Signale zum Prüfen."
    de = {"low": "niedrig", "caution": "erhöht", "high": "hoch", "critical": "kritisch", "unchecked": "ungeprüft"}
    names = {"copytrade": "Copy-Trader", "sniper": "Sniper", "signals": "Signale"}
    out = []
    for name, v in a.items():
        what = "Kurs nach 1 h" if name == "signals" else "Ergebnis"
        out.append(f"{names.get(name, name)}: {v['n']} Käufe, {v['leaks']} trotz Sperre gekauft, {v['blind']} ohne Prüfung.")
        for lv in ("low", "caution", "high", "critical", "unchecked"):
            d = v["levels"].get(lv)
            if not d:
                continue
            avg = f"{100 * d['avg_pnl']:+.0f} %" if d["avg_pnl"] is not None else "–"
            heavy = f"{100 * d['heavy_loss_share']:.0f} %" if d["heavy_loss_share"] is not None else "–"
            out.append(f"  {de[lv]:<9} {d['n']:>4}x, {what} im Schnitt {avg}, Verlust ≥50 % bei {heavy}")
    return "\n".join(out)
