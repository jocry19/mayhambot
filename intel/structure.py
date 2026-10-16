"""Stage 2: holder and insider structure over time, rebuilt from the swap tape (no RPC call).

The rug check snapshots holders now and then (getTokenLargestAccounts is the scarcest public RPC method). Between
those snapshots the tape tells us how positions move: every buy adds tokens to the signer, every sell removes them.
So at any ts we know, for the wallets we have seen trading the coin, who still holds, how concentrated it is, how
much the launch insiders (dev, bundle, insider wallets of the sniper) and smart money still hold, and how that
changed over the last hour. Coverage is honest: it is only as complete as the trades we stored for the coin."""
from collections import defaultdict

from intel import base
from intel.store import has

FEATURES = {
    "tape_holders": ("tape", "Wallets, die noch halten (gesehen)", "n"),
    "tape_holders_chg_1h": ("tape", "Halter-Änderung 1 h", "n"),
    "tape_top10": ("tape", "Top-10 der gesehenen Halter", "pct"),
    "tape_top10_chg_1h": ("tape", "Top-10 Änderung 1 h", "pts"),
    "tape_hhi": ("tape", "Konzentration (Herfindahl)", "n"),
    "tape_new_holders_1h": ("tape", "Neue Halter in 1 h", "n"),
    "tape_insider_held": ("tape", "Insider halten noch (Anteil ihres Starts)", "pct"),
    "tape_insider_sold_1h": ("tape", "Insider-Verkauf in 1 h (Anteil ihres Starts)", "pct"),
    "tape_smart_held": ("tape", "Smart Money hält (Anteil gesehener Bestände)", "pct"),
    "tape_smart_held_chg_1h": ("tape", "Smart-Money-Bestand Änderung 1 h", "pts"),
    "tape_distribution": ("tape", "Verteilung: große Halter geben an neue ab", "n"),
}
MIN_TRADES = 20


def _balances(rows, until):
    bal = defaultdict(float)
    first = {}
    for w, side, t, tok in rows:
        if t > until:
            break
        tok = float(tok or 0)
        if side == "buy":
            bal[w] += tok
            first.setdefault(w, t)
        else:
            bal[w] -= tok
    return {w: v for w, v in bal.items() if v > 1e-9}, first


def _top10(bal):
    tot = sum(bal.values())
    return sum(sorted(bal.values(), reverse=True)[:10]) / tot if tot else None


def tape_structure(conn, mint, ts, days=7):
    out = {k: None for k in FEATURES}
    rows = conn.execute("SELECT wallet, side, ts, token_amount FROM trades WHERE mint=? AND ts>? AND ts<=? ORDER BY ts",
                        (mint, ts - days * 86400, ts)).fetchall()
    if len(rows) < MIN_TRADES:
        return out, {"tape_trades": len(rows)}
    now_b, first = _balances(rows, ts)
    old_b, _ = _balances(rows, ts - 3600)
    tot = sum(now_b.values())
    out["tape_holders"] = len(now_b)
    out["tape_top10"] = _top10(now_b)
    out["tape_hhi"] = sum((v / tot) ** 2 for v in now_b.values()) if tot else None
    out["tape_new_holders_1h"] = sum(1 for w in now_b if first.get(w, 0) > ts - 3600)
    old_ok = sum(1 for r in rows if r[2] <= ts - 3600) >= MIN_TRADES
    if old_ok:
        out["tape_holders_chg_1h"] = len(now_b) - len(old_b)
        ot = _top10(old_b)
        if ot is not None and out["tape_top10"] is not None:
            out["tape_top10_chg_1h"] = 100 * (out["tape_top10"] - ot)
        # distribution: tokens that left the 10 biggest holders of an hour ago, landing with new holders
        big = sorted(old_b, key=lambda w: -old_b[w])[:10]
        left = sum(max(0.0, old_b[w] - now_b.get(w, 0)) for w in big)
        landed = sum(v for w, v in now_b.items() if w not in old_b)
        otot = sum(old_b.values())
        if otot:
            out["tape_distribution"] = min(left, landed) / otot
    tiers, _ = base.tiers_at(conn, ts)
    if tiers and tot:
        sm = sum(v for w, v in now_b.items() if tiers.get(w) in ("ultra", "smart"))
        out["tape_smart_held"] = sm / tot
        if old_ok and sum(old_b.values()):
            so = sum(v for w, v in old_b.items() if tiers.get(w) in ("ultra", "smart")) / sum(old_b.values())
            out["tape_smart_held_chg_1h"] = 100 * (out["tape_smart_held"] - so)
    if has(conn, "sniper_launch_wallets"):
        ins = {r[0]: float(r[1] or 0) for r in conn.execute(
            "SELECT wallet, MAX(tokens) FROM sniper_launch_wallets WHERE mint=? AND role IN ('dev','bundle','insider') "
            "AND (ts IS NULL OR ts<=?) GROUP BY wallet", (mint, ts))}
        start = sum(ins.values())
        if start > 0:
            sold = defaultdict(float)
            sold_1h = 0.0
            for w, side, t, tok in rows:
                if w in ins and side == "sell":
                    sold[w] += float(tok or 0)
                    if t > ts - 3600:
                        sold_1h += float(tok or 0)
            held = sum(max(0.0, ins[w] - sold[w]) for w in ins)
            out["tape_insider_held"] = held / start
            out["tape_insider_sold_1h"] = min(1.0, sold_1h / start)
    return out, {"tape_trades": len(rows)}
