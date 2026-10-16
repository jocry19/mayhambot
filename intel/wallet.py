"""Wallet features at a point in time t (only trades with ts <= t, last 30 days):
hold time and scalp share, concentration, entity (cluster) and co-buys within 60 s, exit capture per market phase,
and how late the copy trader sees this wallet's swaps (ct_swaps.seen_at - ts)."""
import statistics
from collections import Counter, defaultdict

from intel import base
from intel.store import has

FEATURES = {
    "w_n_coins_30d": ("wallet", "Coins in 30 Tagen", "n"),
    "w_n_closed": ("wallet", "Abgeschlossene Positionen", "n"),
    "w_hold_median_min": ("wallet", "Mittlere Haltedauer", "min"),
    "w_scalp_share": ("wallet", "Anteil Scalps unter 5 min", "pct"),
    "w_concentration": ("wallet", "Anteil der Käufe im Lieblings-Coin", "pct"),
    "w_cluster_size": ("wallet", "Wallets in derselben Gruppe", "n"),
    "w_cobuy_share": ("wallet", "Käufe mit Mitkäufern binnen 60 s", "pct"),
    "w_cobuy_partners": ("wallet", "Feste Mitkäufer (≥ 2 gemeinsame Käufe)", "n"),
    "w_exit_capture": ("wallet", "Mitgenommener Anteil vom Hoch", "pct"),
    "w_exit_capture_risk_off": ("wallet", "Mitgenommen bei Risk-off", "pct"),
    "w_exit_capture_neutral": ("wallet", "Mitgenommen bei Neutral", "pct"),
    "w_exit_capture_risk_on": ("wallet", "Mitgenommen bei Risk-on", "pct"),
    "w_exit_capture_hot": ("wallet", "Mitgenommen bei Heiß", "pct"),
    "w_latency_median_s": ("wallet", "Verspätung unserer Erkennung (Median)", "s"),
    "w_latency_p90_s": ("wallet", "Verspätung unserer Erkennung (90 %)", "s"),
}
PHASES = ("risk_off", "neutral", "risk_on", "hot")


def _phase_at(conn, ts, cache):
    b = ts // 900
    if b not in cache:
        r = conn.execute("SELECT label FROM market_snapshots WHERE ts<=? AND ts>=? ORDER BY ts DESC LIMIT 1",
                         (ts, ts - 3600)).fetchone() if has(conn, "market_snapshots") else None
        cache[b] = r[0] if r else None
    return cache[b]


def wallet_features(conn, wallet, ts, days=30):
    ts = int(ts)
    out = {k: None for k in FEATURES}
    rows = conn.execute("SELECT mint, side, ts, usd_amount, token_amount, price_usd FROM trades WHERE wallet=? AND ts>? AND ts<=? "
                        "ORDER BY ts", (wallet, ts - days * 86400, ts)).fetchall()
    cl = base.clusters(conn, ts)
    cid = cl.get(wallet)
    if cid:
        out["w_cluster_size"] = sum(1 for c in cl.values() if c == cid)
    if rows:
        by = defaultdict(list)
        for r in rows:
            by[r[0]].append(r)
        out["w_n_coins_30d"] = len(by)
        buys = {m: sum(float(r[3] or 0) for r in rs if r[1] == "buy") for m, rs in by.items()}
        tot = sum(buys.values())
        out["w_concentration"] = max(buys.values()) / tot if tot else None
        holds, caps = [], defaultdict(list)
        pcache = {}
        for m, rs in by.items():
            fb = next((r for r in rs if r[1] == "buy"), None)
            if not fb:
                continue
            sells = [r for r in rs if r[1] == "sell" and r[2] >= fb[2]]
            if not sells:
                continue
            holds.append((sells[0][2] - fb[2]) / 60)
            tok = sum(float(r[4] or 0) for r in sells)
            usd = sum(float(r[3] or 0) for r in sells)
            avg = usd / tok if tok else None
            peak = conn.execute("SELECT MAX(price_usd) FROM token_prices WHERE mint=? AND ts>=? AND ts<=?",
                                (m, fb[2], sells[-1][2])).fetchone()[0]
            peak = max(filter(None, [peak, max((float(r[5] or 0) for r in rs if fb[2] <= r[2] <= sells[-1][2]), default=0)]),
                       default=None)
            if avg and peak:
                cap = min(1.0, avg / peak)
                caps["all"].append(cap)
                ph = _phase_at(conn, fb[2], pcache)
                if ph:
                    caps[ph].append(cap)
        out["w_n_closed"] = len(holds)
        if holds:
            out["w_hold_median_min"] = statistics.median(holds)
            out["w_scalp_share"] = sum(1 for h in holds if h < 5) / len(holds)
        if caps["all"]:
            out["w_exit_capture"] = statistics.median(caps["all"])
        for ph in PHASES:
            if len(caps[ph]) >= 2:
                out[f"w_exit_capture_{ph}"] = statistics.median(caps[ph])
        out.update(cobuys(conn, wallet, [r for r in rows if r[1] == "buy"][-150:], ts))
    out.update(latency(conn, wallet, ts))
    known = sum(v is not None for v in out.values())
    return {"features": out, "coverage": {"wallet": round(known / len(out), 3), "all": round(known / len(out), 3)},
            "meta": {"cluster_id": cid, "n_trades": len(rows)}}


def cobuys(conn, wallet, buys, ts):
    """Other wallets that bought the same coin within 60 s of this wallet's buys (never after ts)."""
    if not buys:
        return {}
    partners = Counter()
    with_any = 0
    for m, _, t, *_ in buys:
        ws = {r[0] for r in conn.execute("SELECT DISTINCT wallet FROM trades WHERE mint=? AND side='buy' AND ts>=? AND ts<=? "
                                         "AND wallet<>?", (m, t - 60, min(t + 60, ts), wallet))}
        with_any += bool(ws)
        partners.update(ws)
    return {"w_cobuy_share": with_any / len(buys), "w_cobuy_partners": sum(1 for c in partners.values() if c >= 2)}


def latency(conn, wallet, ts):
    if not has(conn, "ct_swaps"):
        return {}
    d = sorted(r[0] for r in conn.execute("SELECT seen_at-ts FROM ct_swaps WHERE wallet=? AND ts>? AND ts<=? AND seen_at>=ts "
                                          "AND seen_at-ts<3600", (wallet, ts - 7 * 86400, ts)))
    if not d:
        return {}
    return {"w_latency_median_s": statistics.median(d), "w_latency_p90_s": d[min(len(d) - 1, int(0.9 * len(d)))]}


def universe(conn, ts, limit=400):
    """Wallets worth profiling: tiered ones (most recent activity first) and the copy trader's list."""
    tiers, _ = base.tiers_at(conn, ts)
    out = list(tiers)
    if has(conn, "ct_wallets"):
        out += [r[0] for r in conn.execute("SELECT address FROM ct_wallets WHERE active=1")]
    out = list(dict.fromkeys(out))
    last = {}
    for i in range(0, len(out), 500):
        part = out[i:i + 500]
        last.update({r[0]: r[1] for r in conn.execute(
            f"SELECT wallet, MAX(ts) FROM trades WHERE ts>? AND ts<=? AND wallet IN ({','.join('?' * len(part))}) "
            "GROUP BY wallet", (ts - 7 * 86400, ts, *part))})
    out.sort(key=lambda w: -(last.get(w) or 0))
    return [w for w in out if w in last][:limit]
