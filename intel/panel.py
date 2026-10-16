"""Everything the dashboard page "Zentrale" shows (GET /api/intel) and the per-coin dossier (GET /api/intel/coin/<mint>)."""
import json
import time
from collections import defaultdict

from intel import base, coin, events, marketctx, store, wallet

STAGES = [
    (1, "Geld- und Orderfluss", "Wer kauft und verkauft was, nach Personen; Smart-Money-Rotation; Insider-Verkäufe; "
        "Liquidität, Kursregime, Holder-Verlauf, geschätzte Ausführungskosten; Live-Ticker; Messung je Merkmal", "live"),
    (2, "Holder- und Insider-Struktur im Zeitverlauf", "Bestände aus dem Swap-Strom zwischen den Rug-Checks: Halter, Top-10, "
        "Insider- und Smart-Money-Bestand, Verteilung großer Halter an neue, jeweils mit Änderung über 1 h", "live"),
    (3, "Liquidität und Markttiefe", "Echte Jupiter-Quotes hin und zurück für 0,1 / 0,25 / 0,5 SOL bei den heißesten Coins, "
        "kein Verkaufsweg, Liquidität, die kommt und sofort wieder geht", "live"),
    (4, "Makro und Aufmerksamkeit", "BTC, ETH, SOL, Fear & Greed, Verlauf der DexScreener-Boosts je Coin", "live"),
    (5, "Netzwerk der Devs und Bundler", "Bilanz des Devs und seines Geldgeber-Netzes, Launch-Wallets aus früheren Rugs "
        "oder 2x-Launches, Bot- und Sandwich-Volumen", "live"),
]


def _j(s, default=None):
    try:
        return json.loads(s) if s else default
    except ValueError:
        return default


def _symbols(conn, mints):
    out = {}
    mints = list(mints)
    for i in range(0, len(mints), 400):
        part = mints[i:i + 400]
        q = ",".join("?" * len(part))
        out.update({r[0]: (r[1], r[2]) for r in conn.execute(f"SELECT mint, symbol, name FROM tokens WHERE mint IN ({q})", part)})
        if store.has(conn, "market_tokens"):
            for r in conn.execute(f"SELECT mint, symbol, name FROM market_tokens WHERE mint IN ({q})", part):
                if not (out.get(r[0]) or (None,))[0]:
                    out[r[0]] = (r[1], r[2])
    return out


def flags(f):
    """Short German badges a trader reads first: [(kind good|bad|warn, text)]."""
    out = []
    g = f.get
    if (g("flow_smart_entities_60m") or 0) >= 2:
        out.append(("good", f"{g('flow_smart_entities_60m'):.0f} Smart-Personen kaufen"))
    if g("flow_smart_exit_share") is not None and g("flow_smart_exit_share") >= 0.5:
        out.append(("bad", "Smart Money steigt aus"))
    if (g("flow_insider_sell_usd_60m") or 0) >= 300:
        out.append(("bad", "Insider verkaufen"))
    if g("vol_roundtrip_60m") is not None and g("vol_roundtrip_60m") >= 0.4:
        out.append(("warn", f"Wash {100 * g('vol_roundtrip_60m'):.0f} %"))
    if g("liq_off_peak") is not None and g("liq_off_peak") >= 0.5:
        out.append(("bad", f"Liquidität −{100 * g('liq_off_peak'):.0f} % vom Hoch"))
    if g("liq_chg_15m") is not None and g("liq_chg_15m") <= -0.2:
        out.append(("bad", "Liquidität fließt ab"))
    if g("exec_rt_0_25") is not None and g("exec_rt_0_25") >= 0.06:
        out.append(("warn", f"Teuer: {100 * g('exec_rt_0_25'):.0f} % hin und zurück"))
    if g("flow_buyers_accel") is not None and g("flow_buyers_accel") >= 2 and (g("flow_buyers_15m") or 0) >= 5:
        out.append(("good", "Käufer beschleunigen"))
    if g("micro_vpin") is not None and g("micro_vpin") >= 0.8 and (g("vol_trades_60m") or 0) >= 50:
        out.append(("warn", "Einseitiger Fluss (VPIN)"))
    if g("micro_absorption") is not None and g("micro_absorption") >= 0.5:
        out.append(("warn", "Käufe werden absorbiert"))
    if g("exec_q_noroute") == 1:
        out.append(("bad", "Kein Verkaufsweg"))
    if g("exec_q_0_25") is not None and g("exec_q_0_25") >= 0.06:
        out.append(("warn", f"Gemessen {100 * g('exec_q_0_25'):.0f} % hin und zurück"))
    if g("net_dev_rug_rate") is not None and (g("net_dev_launches") or 0) >= 2 and g("net_dev_rug_rate") >= 0.5:
        out.append(("bad", f"Dev rugte {100 * g('net_dev_rug_rate'):.0f} % seiner Launches"))
    if g("net_funder_rug_rate") is not None and (g("net_funder_launches") or 0) >= 3 and g("net_funder_rug_rate") >= 0.5:
        out.append(("bad", "Geldgeber-Netz rugt oft"))
    if (g("net_early_bad") or 0) >= 2:
        out.append(("bad", f"{g('net_early_bad'):.0f} bekannte Rug-Wallets dabei"))
    if (g("net_early_good") or 0) >= 2:
        out.append(("good", f"{g('net_early_good'):.0f} Wallets aus 2x-Launches"))
    if g("net_bot_share_60m") is not None and g("net_bot_share_60m") >= 0.4:
        out.append(("warn", f"Bots {100 * g('net_bot_share_60m'):.0f} % des Volumens"))
    if g("tape_insider_sold_1h") is not None and g("tape_insider_sold_1h") >= 0.25:
        out.append(("bad", "Insider bauen ab"))
    if g("tape_distribution") is not None and g("tape_distribution") >= 0.15:
        out.append(("warn", "Große Halter verteilen an Neue"))
    if g("liq_flash_60m") is not None and g("liq_flash_60m") >= 0.5:
        out.append(("warn", "Liquidität kam und ging"))
    if g("att_boost_chg_1h") is not None and g("att_boost_chg_1h") > 0:
        out.append(("", "Neu beworben"))
    if g("hold_top10") is not None and g("hold_top10") >= 0.5:
        out.append(("warn", f"Top-10 halten {100 * g('hold_top10'):.0f} %"))
    if g("hold_top10_chg_1h") is not None and g("hold_top10_chg_1h") <= -10:
        out.append(("warn", "Große Holder verteilen"))
    return out


def _latest(conn, kind, max_age, now):
    return conn.execute("SELECT key, ts, coverage, features_json, meta_json FROM intel_latest WHERE kind=? AND ts>=?",
                        (kind, now - max_age)).fetchall()


def view(conn, now=None):
    now = int(now or time.time())
    if not store.has(conn, "intel_state"):
        return {"available": False, "note": "Die Zentrale sammelt noch keine Daten (Dienst intel nicht gestartet)."}
    sv = store.get_state(conn, "service_status") or {}
    sv["up"] = bool(sv.get("ts") and now - sv["ts"] <= 3 * max(60, sv.get("every_sec") or 60))
    coins = []
    dp = store.get_state(conn, "deep") or {}
    from intel import deep as _deep
    rows = _latest(conn, "coin", 1800, now)
    sym = _symbols(conn, [r[0] for r in rows])
    for k, ts, cov, fj, mj in rows:
        f = _j(fj, {})
        meta = _j(mj, {})
        s, name = sym.get(k, (None, None))
        coins.append({"mint": k, "symbol": s, "name": name, "ts": ts, "coverage": cov, "coverage_by": meta.get("coverage"),
                      "flags": flags(f), "setups": _deep.matching(f, dp.get("playbook")), **{x: f.get(x) for x in (
                          "px_mcap", "liq_usd", "liq_off_peak", "liq_chg_15m", "flow_net_usd_15m", "flow_net_usd_60m",
                          "flow_buy_ratio_15m", "flow_buyers_15m", "flow_buyers_accel", "flow_smart_entities_60m",
                          "flow_smart_net_usd_60m", "flow_smart_exit_share", "flow_whale_share_60m", "flow_insider_sell_usd_60m",
                          "vol_usd_60m", "vol_entities_60m", "vol_roundtrip_60m", "hold_top10", "hold_insider", "hold_safety",
                          "exec_rt_0_25", "exec_q_0_25", "tape_insider_held", "tape_top10", "net_dev_rug_rate", "net_bot_share_60m", "att_boost", "micro_vpin", "micro_lambda", "micro_ofi_15m", "px_ret_15m", "px_ret_60m", "px_vol_15m", "px_off_high_24h", "px_age_h",
                          "mkt_sector_heat", "mkt_term_heat")}})
    coins.sort(key=lambda c: (-(c["flow_smart_entities_60m"] or 0), -(c["flow_net_usd_60m"] or 0)))
    mk = _latest(conn, "market", 3600, now)
    market = None
    if mk:
        market = {"ts": mk[0][1], "coverage": mk[0][2], "features": _j(mk[0][3], {}), "meta": _j(mk[0][4], {})}
    ev = [dict(r, data=_j(r["data_json"], {})) for r in map(dict, conn.execute(
        "SELECT id, ts, kind, mint, wallet, severity, title, text, data_json FROM intel_events ORDER BY ts DESC, id DESC LIMIT 120"))]
    for e in ev:
        e.pop("data_json", None)
        e["kind_label"], e["tone"] = events.KINDS.get(e["kind"], (e["kind"], ""))
    counts = defaultdict(int)
    for k, in conn.execute("SELECT kind FROM intel_events WHERE ts>?", (now - 86400,)):
        counts[k] += 1
    return {"available": True, "ts": now, "service": sv, "market": market, "market_labels": {k: v[1] for k, v in marketctx.FEATURES.items()},
            "coins": coins[:150], "events": ev, "event_counts_24h": dict(counts),
            "event_kinds": {k: {"label": v[0], "tone": v[1]} for k, v in events.KINDS.items()},
            "rotation": rotation(conn, now), "flows": flows(conn, now, sym_cache=sym), "wallets": wallets(conn, now),
            "evaluation": store.get_state(conn, "evaluation"), "deep": dp or None, "sources": sources(conn, now), "stages": [
                {"n": n, "title": t, "text": x, "status": s} for n, t, x, s in STAGES],
            "groups": coin.GROUPS, "feature_labels": {k: {"group": v[0], "label": v[1], "unit": v[2]} for k, v in coin.FEATURES.items()}}


def rotation(conn, now, hours=6):
    """Where smart money moved in the last hours: from coin -> to coin, summed."""
    edges = events.rotation_edges(conn, now, hours=hours)
    agg = defaultdict(lambda: {"usd": 0.0, "wallets": set(), "n": 0, "last": 0})
    for w, a, b, st, bt, su, bu in edges:
        x = agg[(a, b)]
        x["usd"] += min(su, bu)
        x["wallets"].add(w)
        x["n"] += 1
        x["last"] = max(x["last"], bt)
    sym = _symbols(conn, {m for k in agg for m in k})
    out = [{"from": a, "to": b, "from_symbol": (sym.get(a) or (None,))[0], "to_symbol": (sym.get(b) or (None,))[0],
            "usd": v["usd"], "wallets": len(v["wallets"]), "n": v["n"], "last": v["last"]} for (a, b), v in agg.items()]
    out.sort(key=lambda r: (-r["wallets"], -r["usd"]))
    net = defaultdict(float)
    for r in out:
        net[r["to"]] += r["usd"]
        net[r["from"]] -= r["usd"]
    return {"hours": hours, "edges": out[:40], "n_edges": len(out),
            "magnets": sorted(({"mint": m, "symbol": (sym.get(m) or (None,))[0], "net_usd": v} for m, v in net.items()),
                              key=lambda r: -r["net_usd"])[:8]}


def flows(conn, now, minutes=60, sym_cache=None):
    """Smart money (Ultra/Smart) net USD per coin in the last hour, entities counted once, plus the whole tape split by tier."""
    tiers, pit = base.tiers_at(conn, now)
    cl = base.clusters(conn, now)
    sol = base.sol_usd_at(conn, now)
    per = defaultdict(lambda: {"net": 0.0, "buy": 0.0, "sell": 0.0, "ents": set(), "ultra": 0.0})
    tape = defaultdict(lambda: {"buy": 0.0, "sell": 0.0, "n": 0})
    for w, m, side, u, s in conn.execute("SELECT wallet, mint, side, usd_amount, sol_amount FROM trades WHERE ts>? AND ts<=?",
                                         (now - minutes * 60, now)):
        v = float(u or 0) or float(s or 0) * (sol or 0)
        t = tiers.get(w) or "other"
        tape[t]["buy" if side == "buy" else "sell"] += v
        tape[t]["n"] += 1
        if t in ("ultra", "smart"):
            p = per[m]
            p["net"] += v if side == "buy" else -v
            p["buy" if side == "buy" else "sell"] += v
            p["ents"].add(base.entity(cl, w))
            if t == "ultra":
                p["ultra"] += v if side == "buy" else -v
    sym = dict(sym_cache or {})
    sym.update(_symbols(conn, [m for m in per if m not in sym]))
    rows = [{"mint": m, "symbol": (sym.get(m) or (None,))[0], "net_usd": p["net"], "buy_usd": p["buy"], "sell_usd": p["sell"],
             "entities": len(p["ents"]), "ultra_net_usd": p["ultra"]} for m, p in per.items()]
    rows.sort(key=lambda r: -r["net_usd"])
    return {"minutes": minutes, "tier_pit": pit, "inflow": rows[:12], "outflow": sorted(rows, key=lambda r: r["net_usd"])[:8],
            "tape": {k: dict(v) for k, v in tape.items()}}


def wallets(conn, now, limit=60):
    rows = _latest(conn, "wallet", 3 * 3600, now)
    tiers, _ = base.tiers_at(conn, now)
    out = []
    for k, ts, cov, fj, mj in rows:
        f = _j(fj, {})
        out.append({"address": k, "tier": tiers.get(k), "coverage": cov, **f, "cluster_id": (_j(mj, {}) or {}).get("cluster_id")})
    order = {"ultra": 0, "smart": 1, "good": 2}
    out.sort(key=lambda r: (order.get(r["tier"], 3), -(r.get("w_n_coins_30d") or 0)))
    return {"rows": out[:limit], "n": len(out), "labels": {k: v[1] for k, v in wallet.FEATURES.items()}}


def _count(conn, sql, args=()):
    try:
        return conn.execute(sql, args).fetchone()[0]
    except Exception:  # noqa: BLE001 - table of another service may not exist yet
        return None


def sources(conn, now):
    """What the Zentrale sees right now, per source: freshness and volume (Bloomberg-style source monitor)."""
    h = now - 3600
    out = [
        {"key": "trades", "label": "Swaps (GeckoTerminal, RPC, Helius)", "n_1h": _count(conn, "SELECT COUNT(*) FROM trades WHERE ts>?", (h,)),
         "last": _count(conn, "SELECT MAX(ts) FROM trades"), "cost": "schon vorhanden"},
        {"key": "prices", "label": "Kurse und Liquidität (DexScreener)", "n_1h": _count(conn, "SELECT COUNT(*) FROM token_prices WHERE ts>?", (h,)),
         "last": _count(conn, "SELECT MAX(ts) FROM token_prices"), "cost": "schon vorhanden"},
        {"key": "safety", "label": "Rug-Schutz (Holder, Insider, Bundles, Wash)", "n_1h": _count(conn, "SELECT COUNT(*) FROM safety_tokens WHERE ts>?", (h,)),
         "last": _count(conn, "SELECT MAX(ts) FROM safety_tokens"), "cost": "schon vorhanden"},
        {"key": "launches", "label": "Launches (Sniper, PumpPortal + RPC)", "n_1h": _count(conn, "SELECT COUNT(*) FROM sniper_launches WHERE created_ts>?", (h,)),
         "last": _count(conn, "SELECT MAX(created_ts) FROM sniper_launches"), "cost": "schon vorhanden"},
        {"key": "market", "label": "Marktlage (DexScreener, CoinGecko)", "n_1h": _count(conn, "SELECT COUNT(*) FROM market_snapshots WHERE ts>?", (h,)),
         "last": _count(conn, "SELECT MAX(ts) FROM market_snapshots"), "cost": "schon vorhanden"},
        {"key": "copy", "label": "Wal-Swaps des Copy-Traders", "n_1h": _count(conn, "SELECT COUNT(*) FROM ct_swaps WHERE seen_at>?", (h,)),
         "last": _count(conn, "SELECT MAX(seen_at) FROM ct_swaps"), "cost": "schon vorhanden"},
        {"key": "quotes", "label": "Jupiter-Quotes (Stufe 3)", "n_1h": _count(conn, "SELECT COUNT(*) FROM intel_quotes WHERE ts>?", (h,)),
         "last": _count(conn, "SELECT MAX(ts) FROM intel_quotes"), "cost": "Jupiter, kostenlos"},
        {"key": "macro", "label": "BTC, ETH, Fear & Greed (Stufe 4)", "n_1h": _count(conn, "SELECT COUNT(*) FROM intel_macro WHERE ts>?", (h,)),
         "last": _count(conn, "SELECT MAX(ts) FROM intel_macro"), "cost": "CoinGecko, alternative.me, kostenlos"},
        {"key": "attention", "label": "Boosts und Bewerbung (Stufe 4)", "n_1h": _count(conn, "SELECT COUNT(*) FROM intel_attention WHERE ts>?", (h,)),
         "last": _count(conn, "SELECT MAX(ts) FROM intel_attention"), "cost": "schon vorhanden"},
        {"key": "model", "label": "Wallet-Stufen (Modell)", "n_1h": _count(conn, "SELECT COUNT(*) FROM model_wallet_stats WHERE tier IS NOT NULL"),
         "last": _count(conn, "SELECT MAX(ts) FROM model_versions"), "cost": "schon vorhanden"},
    ]
    for s in out:
        s["age_s"] = None if not s["last"] else now - int(s["last"])
        s["ok"] = s["age_s"] is not None and s["age_s"] <= (3 * 3600 if s["key"] in ("model", "macro", "attention") else 1800)
    return out


def coin_view(conn, mint, now=None, hours=24):
    """Dossier of one coin: latest features with coverage, flags, the coin's ticker events and the feature history."""
    now = int(now or time.time())
    if not store.has(conn, "intel_latest"):
        return {"available": False}
    r = conn.execute("SELECT ts, coverage, features_json, meta_json FROM intel_latest WHERE kind='coin' AND key=?", (mint,)).fetchone()
    if r:
        f, meta, ts = _j(r[2], {}), _j(r[3], {}), r[0]
    else:  # not in the universe: compute on demand (cheap, DB only)
        x = coin.coin_features(conn, mint, now)
        f, meta, ts = x["features"], dict(x["meta"], coverage=x["coverage"]), now
    hist = [{"ts": t, **{k: v for k, v in _j(fj, {}).items() if k in HISTORY}} for t, fj in conn.execute(
        "SELECT ts, features_json FROM intel_snapshots WHERE kind='coin' AND key=? AND ts>=? ORDER BY ts", (mint, now - hours * 3600))]
    ev = [dict(e) for e in conn.execute("SELECT ts, kind, severity, title, text FROM intel_events WHERE mint=? ORDER BY ts DESC LIMIT 40", (mint,))]
    for e in ev:
        e["kind_label"], e["tone"] = events.KINDS.get(e["kind"], (e["kind"], ""))
    groups = defaultdict(list)
    for k, (g, label, unit) in coin.FEATURES.items():
        groups[g].append({"feature": k, "label": label, "unit": unit, "value": f.get(k)})
    return {"available": True, "mint": mint, "ts": ts, "coverage": meta.get("coverage"), "flags": flags(f),
            "groups": [{"group": g, "label": coin.GROUPS[g], "items": groups[g]} for g in coin.GROUPS],
            "history": hist, "events": ev, "meta": {k: v for k, v in meta.items() if k != "coverage"}}


HISTORY = ("tape_holders", "tape_top10", "tape_insider_held", "tape_smart_held", "exec_q_0_25", "flow_net_usd_15m", "flow_smart_net_usd_60m", "flow_smart_entities_60m", "vol_roundtrip_60m", "liq_usd",
           "hold_top10", "hold_insider", "exec_rt_0_25", "px_vol_15m", "flow_buy_ratio_15m")
