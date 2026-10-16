"""Live ticker of the Zentrale: what just happened that a professional trader would want to know.
Each detector reads the shared DB as of `now` and returns events with a dedup key, so a re-run never repeats one.

  ultra_buy     an Ultra wallet bought
  smart_entry   two or more smart entities (clusters count once) net-bought the same coin within 15 min
  smart_exit    most smart entities that bought a coin in the last 6 h are selling it
  insider_sell  dev, bundle or insider wallets of a launch are selling
  rotation      a smart wallet sold coin A and bought coin B within 30 min (where the money moves)
  liq_drop      liquidity fell sharply within 15 min
  wash          most of a coin's volume is the same wallets buying and selling
  holder_shift  top-10 or insider share changed by 10 points or more within an hour
  risk_jump     the rug check score rose by 25 or more, or the coin got blocked
"""
from collections import defaultdict

from intel import base
from intel.config import settings as default_cfg
from intel.store import has

KINDS = {
    "ultra_buy": ("Ultra-Kauf", "good"), "smart_entry": ("Smart Money steigt ein", "good"),
    "smart_exit": ("Smart Money steigt aus", "bad"), "insider_sell": ("Insider verkaufen", "bad"),
    "rotation": ("Rotation", ""), "liq_drop": ("Liquidität bricht ein", "bad"), "wash": ("Wash-Trading", "warn"),
    "holder_shift": ("Holder-Struktur kippt", "warn"), "risk_jump": ("Rug-Risiko steigt", "bad"),
}


def _sym(conn, mint, cache):
    if mint not in cache:
        r = conn.execute("SELECT symbol FROM tokens WHERE mint=?", (mint,)).fetchone()
        s = r[0] if r and r[0] else None
        if not s and has(conn, "market_tokens"):
            r = conn.execute("SELECT symbol FROM market_tokens WHERE mint=?", (mint,)).fetchone()
            s = r[0] if r and r[0] else None
        cache[mint] = s or mint[:6] + "…"
    return cache[mint]


def _usd(u, s, sol):
    return float(u or 0) or float(s or 0) * (sol or 0)


def _fmt(v):
    return f"${v:,.0f}".replace(",", ".")


def detect(conn, now, cfg=default_cfg, lookback=900):
    """All events of the last `lookback` seconds (the service runs every minute, overlap is deduplicated)."""
    now = int(now)
    sym = {}
    tiers, _ = base.tiers_at(conn, now)
    cl = base.clusters(conn)
    sol = base.sol_usd_at(conn, now)
    ev = []
    smart = {w for w, t in tiers.items() if t in ("ultra", "smart")}
    recent = conn.execute("SELECT sig, wallet, mint, side, ts, usd_amount, sol_amount FROM trades WHERE ts>? AND ts<=?",
                          (now - 3600, now)).fetchall()
    # ultra buys
    for sig, w, m, side, t, u, s in recent:
        if side == "buy" and t > now - lookback and tiers.get(w) == "ultra" and _usd(u, s, sol) >= cfg.ultra_buy_usd:
            v = _usd(u, s, sol)
            ev.append(dict(ts=t, kind="ultra_buy", mint=m, wallet=w, severity=2, title=f"Ultra kauft {_sym(conn, m, sym)}",
                           text=f"Ultra-Wallet {w[:4]}…{w[-4:]} kauft für {_fmt(v)}.", data={"usd": v}, dedup=f"ub:{sig}:{w}"))
    # smart entries: >= 2 entities net buying in 15 min
    by = defaultdict(lambda: defaultdict(float))
    for sig, w, m, side, t, u, s in recent:
        if w in smart and t > now - 900:
            by[m][base.entity(cl, w)] += _usd(u, s, sol) * (1 if side == "buy" else -1)
    for m, ents in by.items():
        buyers = [e for e, v in ents.items() if v > 0]
        if len(buyers) >= 2:
            net = sum(v for v in ents.values())
            ev.append(dict(ts=now, kind="smart_entry", mint=m, severity=3 if len(buyers) >= 3 else 2,
                           title=f"{len(buyers)} Smart-Personen kaufen {_sym(conn, m, sym)}",
                           text=f"{len(buyers)} unabhängige Smart-Money-Personen kaufen netto {_fmt(net)} in 15 min.",
                           data={"entities": len(buyers), "net_usd": net}, dedup=f"se:{m}:{now // 7200}"))
    # smart exits and rotation from the last 6 h of smart trades
    rows6 = conn.execute("SELECT wallet, mint, side, ts, usd_amount, sol_amount FROM trades WHERE ts>? AND ts<=? ORDER BY ts",
                         (now - 6 * 3600, now)).fetchall()
    b6, s6, last_sell = defaultdict(lambda: defaultdict(float)), defaultdict(lambda: defaultdict(float)), defaultdict(int)
    for w, m, side, t, u, s in rows6:
        if w not in smart:
            continue
        e = base.entity(cl, w)
        (b6 if side == "buy" else s6)[m][e] += _usd(u, s, sol)
        if side == "sell":
            last_sell[m] = max(last_sell[m], t)
    for m in b6:
        bought = [e for e, v in b6[m].items() if v > 0]
        out = [e for e in bought if s6[m][e] >= 0.5 * b6[m][e]]
        if len(out) >= 2 and len(out) / len(bought) >= 0.5 and last_sell[m] > now - lookback:
            ev.append(dict(ts=last_sell[m], kind="smart_exit", mint=m, severity=3,
                           title=f"Smart Money verlässt {_sym(conn, m, sym)}",
                           text=f"{len(out)} von {len(bought)} Smart-Käufern der letzten 6 h haben mindestens die Hälfte verkauft.",
                           data={"out": len(out), "bought": len(bought)}, dedup=f"sx:{m}:{now // 7200}"))
    ev += rotations(conn, now, smart, sol, sym, cfg, lookback, rows6)
    ev += insider_sells(conn, now, sol, sym, cfg, recent)
    ev += liquidity_drops(conn, now, sym, cfg)
    ev += holder_events(conn, now, sym)
    return ev


def rotation_edges(conn, now, hours=6, smart=None, sol=None, rows=None, window=1800, min_usd=None, cfg=default_cfg):
    """[(wallet, from_mint, to_mint, sell_ts, buy_ts, sell_usd, buy_usd)]: a smart wallet's sell followed by a buy of
    another coin within `window` seconds. The sell is the money's source, the buy its destination."""
    if smart is None:
        tiers, _ = base.tiers_at(conn, now)
        smart = {w for w, t in tiers.items() if t in ("ultra", "smart")}
    sol = sol if sol is not None else base.sol_usd_at(conn, now)
    if rows is None:
        rows = conn.execute("SELECT wallet, mint, side, ts, usd_amount, sol_amount FROM trades WHERE ts>? AND ts<=? ORDER BY ts",
                            (now - hours * 3600, now)).fetchall()
    min_usd = cfg.rotation_min_usd if min_usd is None else min_usd
    by_w = defaultdict(list)
    for w, m, side, t, u, s in rows:
        if w in smart:
            by_w[w].append((t, m, side, _usd(u, s, sol)))
    out = []
    for w, trs in by_w.items():
        sells = [x for x in trs if x[2] == "sell" and x[3] >= min_usd]
        buys = [x for x in trs if x[2] == "buy" and x[3] >= min_usd]
        for st, sm, _, su in sells:
            nxt = next((b for b in buys if st <= b[0] <= st + window and b[1] != sm), None)
            if nxt:
                out.append((w, sm, nxt[1], st, nxt[0], su, nxt[3]))
    return out


def rotations(conn, now, smart, sol, sym, cfg, lookback, rows):
    ev = []
    for w, a, b, st, bt, su, bu in rotation_edges(conn, now, smart=smart, sol=sol, rows=rows, cfg=cfg):
        if bt <= now - lookback:
            continue
        ev.append(dict(ts=bt, kind="rotation", mint=b, wallet=w, severity=1,
                       title=f"{_sym(conn, a, sym)} → {_sym(conn, b, sym)}",
                       text=f"Smart-Wallet {w[:4]}…{w[-4:]} verkauft {_sym(conn, a, sym)} ({_fmt(su)}) und kauft "
                            f"{(bt - st) // 60:.0f} min später {_sym(conn, b, sym)} ({_fmt(bu)}).",
                       data={"from": a, "to": b, "sell_usd": su, "buy_usd": bu}, dedup=f"ro:{w}:{a}:{b}:{bt // 3600}"))
    return ev


def insider_sells(conn, now, sol, sym, cfg, recent):
    if not has(conn, "sniper_launch_wallets"):
        return []
    sells = defaultdict(lambda: defaultdict(float))
    mints = {m for _, _, m, side, t, _, _ in recent if side == "sell" and t > now - 900}
    if not mints:
        return []
    roles = defaultdict(dict)
    for m in mints:
        for w, role in conn.execute("SELECT wallet, role FROM sniper_launch_wallets WHERE mint=? AND role IN ('bundle','insider','dev')",
                                    (m,)):
            roles[m][w] = role
    for sig, w, m, side, t, u, s in recent:
        if side == "sell" and t > now - 900 and w in roles.get(m, {}):
            sells[m][w] += _usd(u, s, sol)
    ev = []
    for m, ws in sells.items():
        tot = sum(ws.values())
        if tot >= cfg.insider_sell_usd or len(ws) >= 2:
            kinds = sorted({roles[m][w] for w in ws})
            label = {"dev": "Dev", "bundle": "Bundle", "insider": "Insider"}
            ev.append(dict(ts=now, kind="insider_sell", mint=m, severity=3, title=f"Insider verkaufen {_sym(conn, m, sym)}",
                           text=f"{len(ws)} {'/'.join(label[k] for k in kinds)}-Wallets verkaufen {_fmt(tot)} in 15 min.",
                           data={"usd": tot, "wallets": len(ws), "roles": kinds}, dedup=f"is:{m}:{now // 3600}"))
    return ev


def liquidity_drops(conn, now, sym, cfg):
    ev = []
    rows = conn.execute("SELECT mint, MIN(ts), MAX(ts) FROM token_prices WHERE ts>? AND ts<=? AND liquidity_usd>0 GROUP BY mint",
                        (now - 1200, now)).fetchall()
    for m, t0, t1 in rows:
        if t1 < now - 600 or t0 == t1:
            continue
        a = conn.execute("SELECT MAX(liquidity_usd) FROM token_prices WHERE mint=? AND ts>? AND ts<=?", (m, now - 1200, t1 - 1)).fetchone()[0]
        b = conn.execute("SELECT liquidity_usd FROM token_prices WHERE mint=? AND ts=?", (m, t1)).fetchone()[0]
        if a and b and a >= 10_000 and b < a * (1 - cfg.liq_drop_pct / 100):
            ev.append(dict(ts=t1, kind="liq_drop", mint=m, severity=3, title=f"Liquidität {_sym(conn, m, sym)} −{100 * (1 - b / a):.0f} %",
                           text=f"Liquidität fiel binnen 20 min von {_fmt(a)} auf {_fmt(b)}.", data={"from": a, "to": b},
                           dedup=f"ld:{m}:{now // 3600}"))
    return ev


def holder_events(conn, now, sym):
    if not has(conn, "intel_holders"):
        return []
    ev = []
    rows = conn.execute("SELECT * FROM intel_holders WHERE seen_ts>? ORDER BY safety_ts", (now - 900,)).fetchall()
    for r in rows:
        old = conn.execute("SELECT * FROM intel_holders WHERE mint=? AND safety_ts<=? AND safety_ts>=? ORDER BY safety_ts LIMIT 1",
                           (r["mint"], r["safety_ts"] - 1800, r["safety_ts"] - 5400)).fetchone()
        m = r["mint"]
        if old:
            for col, label in (("top10_pct", "Top-10-Holder"), ("insider_pct", "Insider")):
                if r[col] is not None and old[col] is not None and abs(r[col] - old[col]) >= 10:
                    d = r[col] - old[col]
                    ev.append(dict(ts=r["safety_ts"], kind="holder_shift", mint=m, severity=2,
                                   title=f"{label} {_sym(conn, m, sym)} {d:+.0f} Pkt",
                                   text=f"{label} halten jetzt {r[col]:.0f} % statt {old[col]:.0f} % vor etwa einer Stunde"
                                        + (" (verteilen an Käufer)." if d < 0 else " (sammeln ein)."),
                                   data={"col": col, "from": old[col], "to": r[col]}, dedup=f"hs:{m}:{col}:{r['safety_ts'] // 3600}"))
            if r["safety_score"] is not None and old["safety_score"] is not None and r["safety_score"] - old["safety_score"] >= 25:
                ev.append(dict(ts=r["safety_ts"], kind="risk_jump", mint=m, severity=3,
                               title=f"Rug-Risiko {_sym(conn, m, sym)} {old['safety_score']:.0f} → {r['safety_score']:.0f}",
                               text="Der Rug-Schutz bewertet den Coin deutlich riskanter als vor einer Stunde.",
                               data={"from": old["safety_score"], "to": r["safety_score"]}, dedup=f"rj:{m}:{r['safety_ts'] // 3600}"))
        if r["blocked"] and not (old and old["blocked"]):
            ev.append(dict(ts=r["safety_ts"], kind="risk_jump", mint=m, severity=3, title=f"{_sym(conn, m, sym)} gesperrt",
                           text="Der Rug-Schutz sperrt den Coin (kritisches Risiko oder harter Befund).",
                           data={"blocked": 1}, dedup=f"rb:{m}"))
        if r["roundtrip_share"] is not None and r["roundtrip_share"] >= default_cfg.wash_warn:
            ev.append(dict(ts=r["safety_ts"], kind="wash", mint=m, severity=2, title=f"Wash-Trading {_sym(conn, m, sym)}",
                           text=f"{100 * r['roundtrip_share']:.0f} % des Volumens sind Hin-und-her-Trades derselben Wallets.",
                           data={"roundtrip": r["roundtrip_share"]}, dedup=f"wa:{m}:{r['safety_ts'] // 21600}"))
    return ev
