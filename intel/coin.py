"""Coin features at a point in time t, computed only from data with a timestamp <= t (walk-forward safe).

Groups (coverage = share of the group's features that are known):
  vol    real volume: trades, distinct wallets and entities (clusters count once), wash/round-trip, top-3 share
  flow   order flow: buy pressure, net USD, buyer acceleration, whale share, smart money by entity, insider/dev sells
  liq    liquidity: now, 7-day peak, distance from it, change over 15 / 60 min, liquidity / market cap
  exec   round-trip cost for 0.1 / 0.25 / 0.5 SOL (pool model until stage 3 measures it with Jupiter quotes)
  hold   holder structure from the rug check snapshots: top-10, top-1, insiders, dev, launch bundle, 1 h change
  px     price regime: returns and realized volatility 5 / 15 / 60 min, distance from the 24 h high, age
  micro  microstructure on AMM swaps: order flow imbalance, Kyle's lambda (price impact per $1k net flow), VPIN over
         volume buckets, absorption (buy pressure without price reaction), buy momentum (exhaustion when < 1)
  tape   holder structure rebuilt from the swap tape between rug checks (stage 2, intel/structure.py)
  net    dev / funder / launch-wallet record and bot volume (stage 5, intel/network.py)
  mkt    market phase, sector and narrative heat of the coin (market/)
"""
import math
from collections import defaultdict

from intel import base
from intel.config import settings as default_cfg
from intel.store import has

# name -> (group, German label, unit) ; unit: usd, pct (0..1 fraction), n, x (ratio), min, h, sol, pts
FEATURES = {
    "vol_trades_60m": ("vol", "Trades 60 min", "n"),
    "vol_usd_60m": ("vol", "Volumen 60 min", "usd"),
    "vol_wallets_60m": ("vol", "Wallets 60 min", "n"),
    "vol_entities_60m": ("vol", "Käufer (Personen) 60 min", "n"),
    "vol_roundtrip_60m": ("vol", "Wash/Hin-und-her-Anteil", "pct"),
    "vol_top3_60m": ("vol", "Top-3-Anteil am Volumen", "pct"),
    "flow_buy_ratio_15m": ("flow", "Kaufdruck 15 min", "pct"),
    "flow_net_usd_15m": ("flow", "Netto-Kauf 15 min", "usd"),
    "flow_net_usd_60m": ("flow", "Netto-Kauf 60 min", "usd"),
    "flow_buyers_15m": ("flow", "Käufer 15 min", "n"),
    "flow_buyers_accel": ("flow", "Käufer-Beschleunigung", "x"),
    "flow_whale_share_60m": ("flow", "Wal-Anteil an Käufen", "pct"),
    "flow_retail_buyers_60m": ("flow", "Kleinanleger-Käufer (< $100)", "n"),
    "flow_smart_net_usd_60m": ("flow", "Smart Money netto 60 min", "usd"),
    "flow_smart_entities_60m": ("flow", "Smart-Personen netto kaufend", "n"),
    "flow_smart_exit_share": ("flow", "Smart-Käufer, die wieder verkaufen", "pct"),
    "flow_insider_sell_usd_60m": ("flow", "Verkäufe Dev/Bundle/Insider 60 min", "usd"),
    "liq_usd": ("liq", "Liquidität", "usd"),
    "liq_peak_7d": ("liq", "Liquidität 7-Tage-Hoch", "usd"),
    "liq_off_peak": ("liq", "Liquidität unter dem Hoch", "pct"),
    "liq_chg_15m": ("liq", "Liquidität 15 min", "pct"),
    "liq_chg_60m": ("liq", "Liquidität 60 min", "pct"),
    "liq_mcap": ("liq", "Liquidität / Market Cap", "pct"),
    "liq_flash_60m": ("liq", "Liquidität kam und ging (60 min)", "pct"),
    "att_boost": ("att", "DexScreener-Boost jetzt", "n"),
    "att_boost_chg_1h": ("att", "Boost-Änderung 1 h", "n"),
    "att_promoted_h": ("att", "Seit erster Bewerbung", "h"),
    "exec_rt_0_1": ("exec", "Kosten hin und zurück 0,1 SOL", "pct"),
    "exec_rt_0_25": ("exec", "Kosten hin und zurück 0,25 SOL", "pct"),
    "exec_rt_0_5": ("exec", "Kosten hin und zurück 0,5 SOL", "pct"),
    "exec_q_0_1": ("exec", "Gemessen (Jupiter) 0,1 SOL", "pct"),
    "exec_q_0_25": ("exec", "Gemessen (Jupiter) 0,25 SOL", "pct"),
    "exec_q_0_5": ("exec", "Gemessen (Jupiter) 0,5 SOL", "pct"),
    "exec_q_noroute": ("exec", "Kein Verkaufsweg (1 = ja)", "n"),
    "hold_top10": ("hold", "Top-10-Holder", "pct"),
    "hold_top1": ("hold", "Größter Holder", "pct"),
    "hold_insider": ("hold", "Insider-Anteil", "pct"),
    "hold_dev": ("hold", "Dev-Anteil", "pct"),
    "hold_bundle": ("hold", "Launch-Bundle hält noch", "pct"),
    "hold_top10_chg_1h": ("hold", "Top-10 Änderung 1 h", "pts"),
    "hold_insider_chg_1h": ("hold", "Insider Änderung 1 h", "pts"),
    "hold_safety": ("hold", "Rug-Risiko", "n"),
    "px_ret_5m": ("px", "Kurs 5 min", "pct"),
    "px_ret_15m": ("px", "Kurs 15 min", "pct"),
    "px_ret_60m": ("px", "Kurs 60 min", "pct"),
    "px_vol_5m": ("px", "Volatilität 5 min", "pct"),
    "px_vol_15m": ("px", "Volatilität 15 min", "pct"),
    "px_vol_60m": ("px", "Volatilität 60 min", "pct"),
    "px_off_high_24h": ("px", "Abstand zum 24-h-Hoch", "pct"),
    "px_age_h": ("px", "Alter", "h"),
    "px_mcap": ("px", "Market Cap", "usd"),
    "micro_ofi_5m": ("micro", "Order-Flow-Imbalance 5 min", "n"),
    "micro_ofi_15m": ("micro", "Order-Flow-Imbalance 15 min", "n"),
    "micro_lambda": ("micro", "Preisimpact je $1.000 Netto (Kyle's Lambda)", "pct"),
    "micro_vpin": ("micro", "Toxizität (VPIN, Volumen-Buckets)", "pct"),
    "micro_absorption": ("micro", "Absorption: Kaufdruck ohne Kursreaktion", "n"),
    "micro_buy_momentum": ("micro", "Kauftempo letzte 5 min ggü. davor", "x"),
    "mkt_phase": ("mkt", "Marktphase", "n"),
    "mkt_sector_heat": ("mkt", "Sektor-Hitze", "n"),
    "mkt_term_heat": ("mkt", "Narrativ-Hitze", "n"),
    "mkt_rs_sector_h1": ("mkt", "Stärke ggü. Sektor 1 h", "pts"),
    "mkt_sector_share_chg": ("mkt", "Kapitalzufluss Sektor", "pts"),
}
from intel import network as _network  # noqa: E402
from intel import structure as _structure  # noqa: E402

FEATURES.update(_structure.FEATURES)
FEATURES.update(_network.FEATURES)
GROUPS = {"vol": "Echtes Volumen", "flow": "Orderfluss", "micro": "Mikrostruktur", "tape": "Halter laut Swaps",
          "net": "Netzwerk: Dev, Geldgeber, Bots", "att": "Aufmerksamkeit", "liq": "Liquidität", "exec": "Ausführungskosten",
          "hold": "Holder-Struktur", "px": "Kursregime", "mkt": "Markt und Narrativ"}
SMART = ("ultra", "smart")
INSIDER_ROLES = ("bundle", "insider", "dev")
PUMP_CURVE_K = 30 * 1_073_000_191 / 1e9  # virtual SOL x virtual tokens / supply of a pump.fun curve (SOL units)


def _trades(conn, mint, since, until):
    return conn.execute("SELECT sig, wallet, side, ts, usd_amount, sol_amount FROM trades WHERE mint=? AND ts>? AND ts<=?",
                        (mint, since, until)).fetchall()


def _usd(t, sol_usd):
    if t[4]:
        return float(t[4])
    if t[5] and sol_usd:
        return float(t[5]) * sol_usd
    return 0.0


def volume_flow(conn, mint, ts, cfg=default_cfg):
    rows = _trades(conn, mint, ts - 3600, ts)
    out = {k: None for k in FEATURES if FEATURES[k][0] in ("vol", "flow")}
    if not rows:
        return out, {"n_trades": 0}
    sol_usd = base.sol_usd_at(conn, ts)
    tiers, pit = base.tiers_at(conn, ts)
    cl = base.clusters(conn)
    buy, sell = defaultdict(float), defaultdict(float)
    ent_buy = set()
    whale = retail_buyers = 0.0
    retail = set()
    b15 = s15 = 0.0
    buyers15, buyers_prev = set(), set()
    smart_net = defaultdict(float)
    for r in rows:
        u = _usd(r, sol_usd)
        w = r[1]
        e = base.entity(cl, w)
        if r[2] == "buy":
            buy[w] += u
            ent_buy.add(e)
            if u >= cfg.whale_usd:
                whale += u
            if 0 < u < 100:
                retail.add(e)
            if r[3] > ts - 900:
                b15 += u
                buyers15.add(e)
            elif r[3] > ts - 1800:
                buyers_prev.add(e)
        else:
            sell[w] += u
            if r[3] > ts - 900:
                s15 += u
        if tiers.get(w) in SMART:
            smart_net[e] += u if r[2] == "buy" else -u
    vol = {w: buy[w] + sell[w] for w in set(buy) | set(sell)}
    total = sum(vol.values())
    tb, ts_ = sum(buy.values()), sum(sell.values())
    out.update(
        vol_trades_60m=len(rows), vol_usd_60m=total, vol_wallets_60m=len(vol), vol_entities_60m=len(ent_buy),
        vol_roundtrip_60m=(sum(2 * min(buy[w], sell[w]) for w in vol) / total) if total and len(rows) >= 10 else None,
        vol_top3_60m=(sum(sorted(vol.values(), reverse=True)[:3]) / total) if total and len(rows) >= 10 else None,
        flow_buy_ratio_15m=(b15 / (b15 + s15)) if b15 + s15 > 0 else None,
        flow_net_usd_15m=b15 - s15, flow_net_usd_60m=tb - ts_, flow_buyers_15m=len(buyers15),
        flow_buyers_accel=(len(buyers15) + 1) / (len(buyers_prev) + 1) if buyers15 or buyers_prev else None,
        flow_whale_share_60m=(whale / tb) if tb else None, flow_retail_buyers_60m=len(retail),
        flow_smart_net_usd_60m=sum(smart_net.values()) if tiers else None,
        flow_smart_entities_60m=sum(1 for v in smart_net.values() if v > 0) if tiers else None,
    )
    # smart buyers of the last 6 h who already sold at least half of what they bought
    if tiers:
        hist = conn.execute("SELECT wallet, side, usd_amount, sol_amount FROM trades WHERE mint=? AND ts>? AND ts<=?",
                            (mint, ts - 6 * 3600, ts)).fetchall()
        b6, s6 = defaultdict(float), defaultdict(float)
        for w, side, u, s in hist:
            if tiers.get(w) not in SMART:
                continue
            v = float(u or 0) or float(s or 0) * (sol_usd or 0)
            (b6 if side == "buy" else s6)[base.entity(cl, w)] += v
        bought = [e for e in b6 if b6[e] > 0]
        out["flow_smart_exit_share"] = (sum(1 for e in bought if s6[e] >= 0.5 * b6[e]) / len(bought)) if bought else None
    if has(conn, "sniper_launch_wallets"):
        roles = {r[0] for r in conn.execute(
            f"SELECT wallet FROM sniper_launch_wallets WHERE mint=? AND (ts IS NULL OR ts<=?) "
            f"AND role IN ({','.join('?' * len(INSIDER_ROLES))})", (mint, ts, *INSIDER_ROLES))}
        if roles:
            out["flow_insider_sell_usd_60m"] = sum(sell[w] for w in roles)
    return out, {"n_trades": len(rows), "tier_pit": pit}


def microstructure(conn, mint, ts):
    """Order-book ideas translated to swaps: there is no book, only the tape of buys and sells and the pool price."""
    out = {k: None for k in FEATURES if FEATURES[k][0] == "micro"}
    rows = conn.execute("SELECT ts, side, usd_amount, sol_amount, price_usd FROM trades WHERE mint=? AND ts>? AND ts<=? ORDER BY ts",
                        (mint, ts - 3600, ts)).fetchall()
    if len(rows) < 5:
        return out
    sol = base.sol_usd_at(conn, ts) or 0
    tape = [(t, 1 if side == "buy" else -1, float(u or 0) or float(s or 0) * sol, p) for t, side, u, s, p in rows]

    def ofi(since):
        b = sum(v for t, d, v, _ in tape if t > since and d > 0)
        s_ = sum(v for t, d, v, _ in tape if t > since and d < 0)
        return (b - s_) / (b + s_) if b + s_ > 0 else None
    out["micro_ofi_5m"], out["micro_ofi_15m"] = ofi(ts - 300), ofi(ts - 900)
    # Kyle's lambda: slope of the 1-min log price change on the minute's signed USD flow (per $1,000)
    mins = defaultdict(lambda: [0.0, None])
    for t, d, v, p in tape:
        m = mins[t // 60]
        m[0] += d * v
        if p:
            m[1] = p
    keys = sorted(k for k in mins if mins[k][1])
    xs, ys = [], []
    for a, b in zip(keys, keys[1:]):
        if b - a <= 3 and mins[a][1] > 0:
            xs.append(mins[b][0] / 1000)
            ys.append(math.log(mins[b][1] / mins[a][1]))
    if len(xs) >= 8:
        mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
        vx = sum((x - mx) ** 2 for x in xs)
        if vx > 0:
            out["micro_lambda"] = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / vx
    # VPIN: split the hour's volume into 20 equal buckets in trade order, mean |buy - sell| / bucket volume
    total = sum(v for _, _, v, _ in tape)
    if len(tape) >= 30 and total > 0:
        size, cur, imb, buckets = total / 20, [0.0, 0.0], [], 0
        for _, d, v, _ in tape:
            left = v
            while left > 0:
                take = min(left, size - cur[0] - cur[1])
                cur[0 if d > 0 else 1] += take
                left -= take
                if cur[0] + cur[1] >= size - 1e-9:
                    imb.append(abs(cur[0] - cur[1]) / size)
                    cur = [0.0, 0.0]
                    buckets += 1
        if imb:
            out["micro_vpin"] = sum(imb) / len(imb)
    # absorption: buy pressure that does not move the price (hidden supply sells into it)
    px = [(t, p) for t, _, _, p in tape if p and t > ts - 900]
    o15 = out["micro_ofi_15m"]
    if o15 is not None and len(px) >= 3:
        ret = px[-1][1] / px[0][1] - 1
        out["micro_absorption"] = o15 * math.exp(-abs(ret) / 0.03)
    last5 = sum(v for t, d, v, _ in tape if d > 0 and t > ts - 300)
    prev = sum(v for t, d, v, _ in tape if d > 0 and ts - 1200 < t <= ts - 300) / 3
    if last5 or prev:
        out["micro_buy_momentum"] = (last5 + 1) / (prev + 1)
    return out


def liquidity_price(conn, mint, ts, cfg=default_cfg):
    out = {k: None for k in FEATURES if FEATURES[k][0] in ("liq", "px", "exec")}
    pts = base.series(conn, mint, ts - 7 * 86400, ts)
    meta = {}
    if not pts:
        return out, meta
    last = pts[-1]
    if ts - last[0] > 1800:  # no fresh price: everything "now" is unknown, history still counts
        meta["stale_s"] = ts - last[0]
    fresh = ts - last[0] <= 1800
    liqs = [p for p in pts if p[3]]
    if liqs and fresh:
        liq = liqs[-1][3]
        peak = max(p[3] for p in liqs)
        out.update(liq_usd=liq, liq_peak_7d=peak, liq_off_peak=1 - liq / peak if peak else None)
        for w, k in ((900, "liq_chg_15m"), (3600, "liq_chg_60m")):
            ago = base.at_or_before(liqs, ts - w, 3, max_gap=w)
            out[k] = (liq / ago - 1) if ago else None
        if last[2]:
            out["liq_mcap"] = liq / last[2]
        hour = [p[3] for p in liqs if p[0] > ts - 3600]
        if len(hour) >= 3:
            start, top = hour[0], max(hour)
            out["liq_flash_60m"] = (top / start - 1) if top >= 1.5 * start and liq <= 1.1 * start else 0.0
    if fresh and last[1]:
        p_now = last[1]
        for w, k in ((300, "px_ret_5m"), (900, "px_ret_15m"), (3600, "px_ret_60m")):
            ago = base.at_or_before(pts, ts - w, 1, max_gap=max(600, w))
            out[k] = (p_now / ago - 1) if ago else None
        for w, k in ((300, "px_vol_5m"), (900, "px_vol_15m"), (3600, "px_vol_60m")):
            out[k] = base.realized_vol([p[1] for p in pts if p[0] > ts - w])
        day = [p[1] for p in pts if p[0] > ts - 86400 and p[1]]
        if day:
            out["px_off_high_24h"] = 1 - p_now / max(day)
        out["px_mcap"] = last[2]
    created = conn.execute("SELECT pair_created_at FROM tokens WHERE mint=?", (mint,)).fetchone()
    pc = created[0] if created else None
    if not pc and has(conn, "sniper_launches"):
        r = conn.execute("SELECT created_ts FROM sniper_launches WHERE mint=?", (mint,)).fetchone()
        pc = r[0] if r else None
    if pc:
        pc = pc / 1000 if pc > 1e11 else pc
        if pc <= ts:
            out["px_age_h"] = (ts - pc) / 3600
    out.update(execution_cost(conn, mint, ts, out.get("liq_usd"), last[2] if fresh else None, cfg))
    return out, meta


def execution_cost(conn, mint, ts, liq_usd, mcap_usd, cfg=default_cfg):
    """Round-trip loss (fraction) for buying and selling right away: price impact both ways + fees.
    AMM pool: SOL side = liquidity/2, impact ~ size / (side + size). pump.fun curve (no liquidity): virtual SOL
    from the market cap (v_sol = sqrt(mcap_sol x k)). Measured with Jupiter quotes from stage 3 on."""
    out = {"exec_rt_0_1": None, "exec_rt_0_25": None, "exec_rt_0_5": None}
    sol = base.sol_usd_at(conn, ts)
    if not sol:
        return out
    if liq_usd:
        side, fee = liq_usd / 2 / sol, cfg.dex_fee
    elif mcap_usd:
        side, fee = math.sqrt(mcap_usd / sol * PUMP_CURVE_K), cfg.curve_fee
    else:
        return out
    if side <= 0:
        return out
    for size, k in ((0.1, "exec_rt_0_1"), (0.25, "exec_rt_0_25"), (0.5, "exec_rt_0_5")):
        imp = size / (side + size)
        out[k] = min(1.0, 2 * imp + 2 * fee + 2 * cfg.net_fee_sol / size)
    return out


def holders(conn, mint, ts):
    out = {k: None for k in FEATURES if FEATURES[k][0] == "hold"}
    if not has(conn, "intel_holders"):
        return out, {}
    rows = conn.execute("SELECT * FROM intel_holders WHERE mint=? AND safety_ts<=? AND safety_ts>? ORDER BY safety_ts DESC",
                        (mint, ts, ts - 6 * 3600)).fetchall()
    if not rows:
        return out, {}
    r = rows[0]
    out.update(hold_top10=_frac(r["top10_pct"]), hold_top1=_frac(r["top1_pct"]), hold_insider=_frac(r["insider_pct"]),
               hold_dev=_frac(r["dev_pct"]), hold_bundle=_frac(r["bundle_held_pct"]), hold_safety=r["safety_score"])
    old = next((x for x in rows if x["safety_ts"] <= r["safety_ts"] - 2400), None)
    if old:
        if r["top10_pct"] is not None and old["top10_pct"] is not None:
            out["hold_top10_chg_1h"] = r["top10_pct"] - old["top10_pct"]
        if r["insider_pct"] is not None and old["insider_pct"] is not None:
            out["hold_insider_chg_1h"] = r["insider_pct"] - old["insider_pct"]
    return out, {"safety_age_s": ts - r["safety_ts"], "blocked": r["blocked"]}


def _frac(v):
    return None if v is None else v / 100


def market_part(conn, mint, ts):
    out = {k: None for k in FEATURES if FEATURES[k][0] == "mkt"}
    if not has(conn, "market_snapshots"):
        return out
    try:
        import market
        f = market.features(conn, mint, ts=ts)
    except Exception:  # noqa: BLE001 - market/ is optional
        return out
    for k in out:
        out[k] = f.get(k)
    return out


def coin_features(conn, mint, ts, cfg=default_cfg):
    """{features: {name: float|None}, coverage: {group: 0..1, 'all': 0..1}, meta: {...}} as of ts."""
    ts = int(ts)
    f, meta = {}, {}
    vf, m1 = volume_flow(conn, mint, ts, cfg)
    lp, m2 = liquidity_price(conn, mint, ts, cfg)
    hd, m3 = holders(conn, mint, ts)
    f.update(vf)
    f.update(microstructure(conn, mint, ts))
    f.update(lp)
    f.update(hd)
    tp, m4 = _structure.tape_structure(conn, mint, ts)
    nw, m5 = _network.network_features(conn, mint, ts)
    f.update(tp)
    f.update(nw)
    from intel import sources as _src
    f.update(_src.measured_cost(conn, mint, ts))
    f.update(_src.attention(conn, mint, ts))
    meta.update(m4, **m5)
    f.update(market_part(conn, mint, ts))
    meta.update(m1, **m2, **m3)
    return {"features": f, "coverage": coverage(f), "meta": meta}


def coverage(f):
    by = defaultdict(lambda: [0, 0])
    for k, v in f.items():
        g = FEATURES.get(k, ("other",))[0]
        by[g][1] += 1
        by[g][0] += v is not None
    out = {g: round(a / b, 3) for g, (a, b) in by.items()}
    out["all"] = round(sum(a for a, _ in by.values()) / max(1, sum(b for _, b in by.values())), 3)
    return out
