"""Stage 5: the network behind a coin, from public on-chain traces the other services already stored (no RPC call).

  dev        how the creator's earlier launches ended (rug rate, 2x rate), counted only once their hour was over
  funder     who funded the creator (research_funding / safety_wallets): the record of every creator with that funder
  early      the coin's launch wallets (dev, bundle, insider, early) that were in earlier rugged or 2x launches
  bots       share of the last hour's volume from bot wallets and same-second sandwich patterns (the MEV / HFT of memecoins)
"""
from collections import defaultdict

from intel.store import has

FEATURES = {
    "net_dev_launches": ("net", "Frühere Launches des Devs", "n"),
    "net_dev_rug_rate": ("net", "Rug-Rate des Devs", "pct"),
    "net_dev_hit2x_rate": ("net", "2x-Rate des Devs", "pct"),
    "net_funder_launches": ("net", "Launches aus demselben Geldgeber-Netz", "n"),
    "net_funder_rug_rate": ("net", "Rug-Rate des Geldgeber-Netzes", "pct"),
    "net_early_bad": ("net", "Launch-Wallets aus früheren Rugs", "n"),
    "net_early_good": ("net", "Launch-Wallets aus früheren 2x-Launches", "n"),
    "net_bot_share_60m": ("net", "Volumen von Sandwich-/Roundtrip-Bots 60 min", "pct"),
    "net_sandwich_60m": ("net", "Sandwich-Muster 60 min", "n"),
}
DONE_AFTER = 3600  # a launch's outcome (60 min) is known one hour after creation


def _rates(rows):
    n = len(rows)
    if not n:
        return 0, None, None
    rug = sum(1 for r in rows if r[0]) / n
    hit = sum(1 for r in rows if (r[1] or 0) >= 1) / n
    return n, rug, hit


def _funder(conn, wallet, ts):
    """Funder of a wallet as known at ts (the lookup was done at checked_at)."""
    for t in ("research_funding", "safety_wallets"):
        if has(conn, t):
            col = "wallet" if t == "research_funding" else "address"
            r = conn.execute(f"SELECT funder FROM {t} WHERE {col}=? AND funder IS NOT NULL AND COALESCE(checked_at, 0)<=?",
                             (wallet, ts)).fetchone()
            if r and r[0]:
                return r[0]
    return None


def network_features(conn, mint, ts):
    out = {k: None for k in FEATURES}
    meta = {}
    if has(conn, "sniper_launches"):
        me = conn.execute("SELECT creator, created_ts FROM sniper_launches WHERE mint=?", (mint,)).fetchone()
        creator = me[0] if me else None
        if not creator and has(conn, "safety_tokens"):
            r = conn.execute("SELECT creator FROM safety_tokens WHERE mint=?", (mint,)).fetchone()
            creator = r[0] if r else None
        cut = ts - DONE_AFTER
        if creator:
            meta["creator"] = creator
            rows = conn.execute("SELECT rugged, max_ret_60m FROM sniper_launches WHERE creator=? AND mint<>? AND created_ts<=? "
                                "AND (rugged IS NOT NULL OR max_ret_60m IS NOT NULL)", (creator, mint, cut)).fetchall()
            out["net_dev_launches"], out["net_dev_rug_rate"], out["net_dev_hit2x_rate"] = _rates(rows)
            if not rows:
                out["net_dev_rug_rate"] = out["net_dev_hit2x_rate"] = None
            f = _funder(conn, creator, ts)
            if f:
                meta["funder"] = f
                sibs = set()
                for t, col in (("research_funding", "wallet"), ("safety_wallets", "address")):
                    if has(conn, t):
                        sibs.update(r[0] for r in conn.execute(
                            f"SELECT {col} FROM {t} WHERE funder=? AND COALESCE(checked_at, 0)<=? LIMIT 500", (f, ts)))
                sibs.add(creator)
                q = ",".join("?" * len(sibs))
                frows = conn.execute(f"SELECT rugged, max_ret_60m FROM sniper_launches WHERE creator IN ({q}) AND mint<>? "
                                     "AND created_ts<=? AND (rugged IS NOT NULL OR max_ret_60m IS NOT NULL)",
                                     (*sibs, mint, cut)).fetchall()
                out["net_funder_launches"], rr, _ = _rates(frows)
                out["net_funder_rug_rate"] = rr
        if has(conn, "sniper_launch_wallets"):
            ws = [r[0] for r in conn.execute(
                "SELECT DISTINCT lw.wallet FROM sniper_launch_wallets lw LEFT JOIN sniper_launches l ON l.mint=lw.mint "
                "WHERE lw.mint=? AND COALESCE(l.decided_ts, lw.ts, 0)<=?", (mint, ts))]
            if ws:
                bad, good = set(), set()
                for i in range(0, len(ws), 400):
                    part = ws[i:i + 400]
                    q = ",".join("?" * len(part))
                    for w, rug, mx in conn.execute(
                            f"SELECT lw.wallet, l.rugged, l.max_ret_60m FROM sniper_launch_wallets lw JOIN sniper_launches l "
                            f"ON l.mint=lw.mint WHERE lw.wallet IN ({q}) AND lw.mint<>? AND l.created_ts<=?", (*part, mint, cut)):
                        if rug:
                            bad.add(w)
                        elif (mx or 0) >= 1:
                            good.add(w)
                out["net_early_bad"], out["net_early_good"] = len(bad), len(good - bad)
    out.update(bots(conn, mint, ts))
    return out, meta


def bots(conn, mint, ts):
    # wallets.is_bot is today's verdict (not point in time), so only the pattern itself counts
    rows = conn.execute("SELECT wallet, side, ts, usd_amount, 0 FROM trades WHERE mint=? AND ts>? AND ts<=? ORDER BY ts",
                        (mint, ts - 3600, ts)).fetchall()
    if len(rows) < 10:
        return {}
    total = sum(float(r[3] or 0) for r in rows)
    by_sec = defaultdict(list)
    for w, side, t, u, b in rows:
        by_sec[t].append((w, side))
    sandwich = 0
    sand_w = set()
    for t, trs in by_sec.items():
        near = trs + by_sec.get(t + 1, []) + by_sec.get(t + 2, [])
        ws = {w for w, _ in near}
        for w in ws:
            sides = [s for x, s in near if x == w]
            others = [x for x, s in near if x != w and s == "buy"]
            if "buy" in sides and "sell" in sides and others:
                sandwich += 1
                sand_w.add(w)
    bot_usd = sum(float(r[3] or 0) for r in rows if r[4] or r[0] in sand_w)
    return {"net_bot_share_60m": bot_usd / total if total else None, "net_sandwich_60m": sandwich}
