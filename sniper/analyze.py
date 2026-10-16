"""Launch score: pure function of the stream state and the (optional) enrichment. No network, no DB.

    momentum 0..100  how strongly organic money flows in (buyers, net SOL, buy pressure, smart wallets, spread)
    risk     0..100  bundles, dev, concentration, shared funders, dev history, known bundlers, bots, socials
                     combined as independent dangers: 100 * (1 - prod(1 - r_i/100)), like safety/
    score    = momentum * (1 - risk/100) * safety_factor   (model probability blended in when available)
Flags (bundle, insider, hard) skip a launch regardless of the score.
"""
from dataclasses import dataclass, field

from safety.checks import noisy_or, ramp, short

TIER_WEIGHT = {"ultra": 1.0, "smart": 0.6, "good": 0.3}


@dataclass
class Analysis:
    stage: int
    momentum: float = 0.0
    risk: float = 0.0
    score: float = 0.0
    bundle: bool = False
    insider: bool = False
    hard: bool = False
    reasons: list = field(default_factory=list)     # [{check, risk, text}] worst first
    positives: list = field(default_factory=list)
    features: dict = field(default_factory=dict)
    momentum_parts: dict = field(default_factory=dict)
    wallets: list = field(default_factory=list)     # rows for sniper_launch_wallets
    insiders: dict = field(default_factory=dict)    # wallet -> role, watched for the exit rule

    def add(self, check, risk, text):
        if risk > 0:
            self.reasons.append({"check": check, "risk": round(float(risk), 1), "text": text})

    def skip_reason(self, cfg):
        if self.hard:
            return "hard"
        if self.bundle:
            return "bundle"
        if self.insider and cfg.insider_block:
            return "insider"
        if self.risk > cfg.max_risk:
            return "risk"
        if self.score < cfg.min_score:
            return "score"
        return None


def _pct(x, supply):
    return 100.0 * x / supply if supply else 0.0


def analyze(st, cfg, enr=None, tiers=None, now=None):
    """st: LaunchState, enr: enrichment dict from sniper.enrich (None = stage 1, stream only)."""
    enr = enr or {}
    tiers = tiers or {}
    a = Analysis(stage=2 if enr else 1)
    F = a.features
    supply = st.supply or 1e9
    creator = st.creator
    now = now or st.last_trade_ts or st.created_ts

    # ------------------------------------------------------------ bundles (creation slot / second)
    early = [e for e in enr.get("early") or [] if e.get("wallet")]
    create_slot = enr.get("create_slot")
    if create_slot is None and early:
        cs = [e.get("slot") for e in early if e.get("sig") == st.create_sig and e.get("slot") is not None]
        create_slot = cs[0] if cs else min((e["slot"] for e in early if e.get("slot") is not None), default=None)
    bundle = {}
    if early and create_slot is not None:
        src = "slot"
        for e in early:
            if e.get("side") == "buy" and e["wallet"] != creator and e.get("slot") is not None \
                    and create_slot <= e["slot"] < create_slot + max(1, cfg.bundle_slots):
                b = bundle.setdefault(e["wallet"], {"wallet": e["wallet"], "role": "bundle", "slot": e["slot"],
                                                    "ts": e.get("ts"), "tokens": 0.0, "sol": 0.0})
                b["tokens"] += float(e.get("tokens") or 0)
                b["sol"] += float(e.get("sol") or 0)
        bundle_tokens = sum(b["tokens"] for b in bundle.values())
    else:
        src = "stream"
        # PumpPortal pushes bundled buys right after the create; the RPC log stream subscribes later, so its
        # early trades are next-slot snipers and bundles only show up in the curve estimate below
        for t in ([] if st.trade_src == "rpc_logs" else st.trades):
            if t.side == "buy" and t.wallet != creator and t.ts - st.created_ts < cfg.early_sec:
                b = bundle.setdefault(t.wallet, {"wallet": t.wallet, "role": "bundle", "ts": t.ts, "tokens": 0.0,
                                                 "sol": 0.0})
                b["tokens"] += t.tokens
                b["sol"] += t.sol
        bundle_tokens = sum(b["tokens"] for b in bundle.values())
    unseen = st.unseen_tokens()
    unseen_pct = _pct(unseen, supply)
    explained = bool(early)  # the early list from pump.fun / RPC contains the trades the stream missed
    bundle_pct = _pct(bundle_tokens, supply) + (0.0 if explained else unseen_pct)
    n_bundle = len(bundle)
    F.update(bundle_src=src, create_slot=create_slot, bundle_wallets=n_bundle, bundle_pct=round(bundle_pct, 2),
             unseen_pct=round(unseen_pct, 2))
    still = sum(st.balances.get(w, b["tokens"]) for w, b in bundle.items())
    F["bundle_still_pct"] = round(_pct(still, supply) + (0.0 if explained else unseen_pct), 2)
    where = "im Erstellungs-Slot" if src == "slot" else "zusammen mit der Erstellung"
    if bundle_pct >= cfg.bundle_warn_pct or n_bundle >= cfg.bundle_min_wallets:
        r = max(ramp(bundle_pct, cfg.bundle_warn_pct, cfg.bundle_block_pct, 30, 85),
                ramp(n_bundle, cfg.bundle_min_wallets - 1, cfg.bundle_min_wallets + 5, 20, 60))
        txt = (f"{n_bundle} Wallets kauften {where} {bundle_pct:.1f} % des Supplies (Bundle)" if n_bundle else
               f"{unseen_pct:.1f} % des Supplies wurden zusammen mit der Erstellung gekauft (Bundle, Käufer verdeckt)")
        a.add("bundle", r, txt + ".")
    if bundle_pct >= cfg.bundle_block_pct or (n_bundle >= cfg.bundle_min_wallets and bundle_pct >= cfg.bundle_warn_pct):
        a.bundle = True
    elif bundle_pct < cfg.bundle_warn_pct / 2 and n_bundle < 2:
        a.positives.append(f"Kein Bundle: {where} nur {bundle_pct:.1f} % gekauft.")
    for b in bundle.values():
        a.insiders[b["wallet"]] = "bundle"

    # ------------------------------------------------------------ dev wallet
    dev_bought = st.bought.get(creator, st.dev_tokens) if creator else 0.0
    dev_bal = st.balances.get(creator, dev_bought) if creator else 0.0
    dev_pct = _pct(dev_bought, supply)
    dev_sold_pct = 100 * (1 - dev_bal / dev_bought) if dev_bought > 0 else 0.0
    F.update(dev_buy_pct=round(dev_pct, 2), dev_sold_pct=round(max(0.0, dev_sold_pct), 1))
    if dev_pct >= cfg.dev_warn_pct:
        a.add("dev", ramp(dev_pct, cfg.dev_warn_pct, cfg.dev_block_pct, 15, 60),
              f"Dev kaufte selbst {dev_pct:.1f} % des Supplies.")
    elif creator:
        a.positives.append(f"Dev hält nur {dev_pct:.1f} %.")
    if dev_sold_pct >= 50:
        a.add("dev", 75, f"Dev hat schon {dev_sold_pct:.0f} % seiner Tokens verkauft.")
        a.insider = a.stage >= 2  # stage 1 has its own skip reason (dev_sold); the insider flag means more than that
    elif dev_sold_pct >= 10:
        a.add("dev", 35, f"Dev verkauft bereits ({dev_sold_pct:.0f} % seiner Tokens).")
    if creator:
        a.insiders[creator] = "dev"

    # ------------------------------------------------------------ holder concentration
    holders = dict(st.balances)
    for e in early:
        if e.get("side") == "buy" and e["wallet"] not in holders:
            holders[e["wallet"]] = holders.get(e["wallet"], 0.0) + float(e.get("tokens") or 0)
    top = sorted((v for v in holders.values() if v > 0), reverse=True)[:10]
    top10 = _pct(sum(top), supply) + (0.0 if explained else unseen_pct)
    F["top10_pct"] = round(top10, 2)
    if top10 >= cfg.top10_warn_pct:
        a.add("holders", ramp(top10, cfg.top10_warn_pct, cfg.top10_block_pct, 20, 70),
              f"Top-10-Holder halten {top10:.0f} % (ohne Bonding Curve).")

    # ------------------------------------------------------------ fast sniper bots (first 2 s, not bundled)
    def first_ts():
        if early and create_slot is not None:
            t0 = next((e.get("ts") for e in early if e.get("slot") == create_slot and e.get("ts")), None)
            if t0:
                return {e["wallet"]: e["ts"] - t0 for e in early if e.get("side") == "buy" and e.get("ts")}
        return {w: ts - st.created_ts for w, ts in st.first_buy_ts.items()}
    fast = {w for w, dt in first_ts().items() if dt <= 2 and w != creator and w not in bundle}
    F["fast_buyers"] = len(fast)
    if len(fast) >= cfg.sniper_bots_warn:
        a.add("bots", ramp(len(fast), cfg.sniper_bots_warn, cfg.sniper_bots_warn * 3, 10, 40),
              f"{len(fast)} Sniper-Bots kauften in den ersten 2 Sekunden (verkaufen meist beim ersten Anstieg).")

    # ------------------------------------------------------------ shared funding source of the first buyers
    funders = enr.get("funders") or {}
    clusters = enr.get("clusters") or {}
    exchanges = enr.get("exchanges") or {}
    groups = {}
    for w, fu in funders.items():
        if fu and fu not in exchanges and w != creator:
            groups.setdefault(("f", fu), set()).add(w)
    for w, c in clusters.items():
        if c and w != creator:
            groups.setdefault(("c", c), set()).add(w)
    shared = {k: ws for k, ws in groups.items() if len(ws) >= 2 or (k[0] == "f" and k[1] == creator)}
    F["shared_funder_groups"] = len(shared)
    shared_pct = 0.0
    for (kind, key), ws in shared.items():
        pct = _pct(sum(holders.get(w, 0.0) for w in ws), supply)
        shared_pct += pct
        who = "dem Dev" if key == creator else (f"derselben Wallet {short(key)}" if kind == "f"
                                               else f"demselben Forschungs-Cluster {short(str(key))}")
        a.add("funders", 50 if key == creator else 40,
              f"{len(ws)} erste Käufer wurden von {who} finanziert ({pct:.1f} % des Supplies).")
        for w in ws:
            a.insiders.setdefault(w, "insider")
    if shared and (shared_pct >= cfg.bundle_warn_pct or any(k[1] == creator for k in shared)):
        a.insider = True
    F["shared_funder_pct"] = round(shared_pct, 2)
    if funders and not shared:
        a.positives.append(f"{len(funders)} erste Käufer ohne gemeinsame Geldquelle.")

    # ------------------------------------------------------------ dev history (rug history)
    hist = enr.get("creator_hist") or {}
    coins = enr.get("dev_coins") or {}
    n_launch = max(int(hist.get("n") or 0), int(coins.get("n") or 0))
    n24 = max(int(hist.get("n24") or 0), int(coins.get("n24") or 0))
    rugs = int(hist.get("rugs") or 0) + int(hist.get("dumps") or 0)
    F.update(dev_launches=n_launch, dev_launches_24h=n24, dev_rugs=rugs,
             dev_migrated=max(int(hist.get("mig") or 0), int(coins.get("n_complete") or 0)))
    if n24 >= cfg.dev_max_launches_24h:
        a.add("dev_history", ramp(n24, cfg.dev_max_launches_24h, cfg.dev_max_launches_24h * 4, 45, 80),
              f"Serien-Launcher: der Dev hat {n24} Coins in 24 h erstellt.")
    if rugs:
        a.add("dev_history", ramp(rugs, 1, 4, 40, 85), f"{rugs} frühere Launches dieses Devs endeten als Rug oder Dump.")
        if rugs >= 2:
            a.insider = True
    if n_launch and not rugs and F["dev_migrated"]:
        a.positives.append(f"Dev hat schon {F['dev_migrated']} Coins bis zur Migration gebracht.")

    # ------------------------------------------------------------ known bundlers / ruggers among the first buyers
    bad = {w: n for w, n in (enr.get("known_bad") or {}).items() if n}
    F["known_bad"] = len(bad)
    if bad:
        a.add("known_bad", ramp(len(bad), 1, 4, 35, 80),
              f"{len(bad)} erste Käufer waren schon bei {sum(bad.values())} gerugten Launches Bundle- oder Insider-Wallets.")
        if len(bad) >= 2:
            a.insider = True
        for w in bad:
            a.insiders.setdefault(w, "insider")

    # ------------------------------------------------------------ insiders already selling
    sold = {w: s for w, s in st.insider_sold.items() if s * 100 >= cfg.insider_sell_min_pct and w in a.insiders}
    bundle_sold = [w for w, b in bundle.items() if b["tokens"] and st.balances.get(w, b["tokens"]) < b["tokens"] * 0.75]
    if bundle_sold:
        a.add("bundle_sells", 60, f"{len(bundle_sold)} Bundle-Wallets verkaufen schon.")
        a.insider = True
    elif sold:
        a.add("bundle_sells", 50, f"{len(sold)} Insider-Wallets verkaufen schon.")

    # ------------------------------------------------------------ metadata, socials, authorities, safety
    coin = enr.get("coin")
    if coin is not None:
        socials = [k for k in ("twitter", "telegram", "website") if coin.get(k)]
        F["has_socials"] = int(bool(socials))
        if socials:
            a.positives.append("Socials: " + ", ".join(socials) + ".")
        else:
            a.add("socials", 15, "Keine Website und keine Socials hinterlegt.")
    auth = enr.get("authorities") or {}
    if auth.get("mint_authority") or auth.get("freeze_authority"):
        a.hard = True
        a.add("authorities", 90, "Mint- oder Freeze-Authority ist noch aktiv.")
    saf = enr.get("safety")
    factor = 1.0
    if saf:
        F.update(safety_risk=saf.get("risk"), safety_level=saf.get("level"))
        factor = float(saf.get("factor", 1.0))
        if saf.get("blocked"):
            a.hard = True
            a.add("safety", max(75.0, float(saf.get("risk") or 75)),
                  "Rug-Schutz sperrt den Coin: " + "; ".join(saf.get("reasons") or [])[:180])

    # ------------------------------------------------------------ momentum (organic flow only)
    excl = set(a.insiders)
    buys = [t for t in st.trades if t.side == "buy" and t.wallet not in excl]
    sells = [t for t in st.trades if t.side == "sell" and t.wallet not in excl]
    buyers = {t.wallet for t in buys}
    buy_sol, sell_sol = sum(t.sol for t in buys), sum(t.sol for t in sells)
    net = buy_sol - sell_sol
    pressure = buy_sol / (buy_sol + sell_sol) if buy_sol + sell_sol > 0 else 0.0
    smart = {w: tiers[w] for w in buyers if w in tiers}
    smart_w = sum(TIER_WEIGHT.get(t, 0.0) for t in smart.values())
    by_w = {}
    for t in buys:
        by_w[t.wallet] = by_w.get(t.wallet, 0.0) + t.sol
    top3 = sum(sorted(by_w.values(), reverse=True)[:3]) / buy_sol if buy_sol > 0 else 1.0
    parts = {
        "buyers": (0.25, min(1.0, ramp(len(buyers), 3, 40, 0, 1)), f"{len(buyers)} organische Käufer"),
        "inflow": (0.30, min(1.0, ramp(net, 0.5, 25, 0, 1)), f"{net:+.1f} SOL netto organisch"),
        "pressure": (0.15, min(1.0, ramp(pressure, 0.5, 0.9, 0, 1)), f"{pressure:.0%} Kaufdruck"),
        "smart": (0.20, min(1.0, smart_w / 2), f"{len(smart)} Smart-Money-Wallets"),
        "spread": (0.10, min(1.0, ramp(1 - top3, 0.2, 0.7, 0, 1)), f"Top-3-Käufer {top3:.0%} des Volumens"),
    }
    a.momentum_parts = {k: {"weight": w, "value": round(v, 3), "points": round(100 * w * v, 1), "why": why}
                        for k, (w, v, why) in parts.items()}
    a.momentum = round(sum(p["points"] for p in a.momentum_parts.values()), 1)
    if smart:
        a.positives.append(f"Smart Money kauft: {', '.join(f'{short(w)} ({t})' for w, t in list(smart.items())[:4])}.")
    prog = None
    from sniper.state import curve_progress
    prog = curve_progress(st.v_tok, st.pool)
    F.update(n_trades=st.n_seen, n_buyers=len(buyers), n_sellers=len({t.wallet for t in sells}),
             buy_sol=round(buy_sol, 3), sell_sol=round(sell_sol, 3), net_sol=round(net, 3), pressure=round(pressure, 3),
             top3_share=round(top3, 3), curve_progress=round(prog, 4) if prog is not None else None,
             age_sec=round(now - st.created_ts, 1), mcap_sol=round(st.mcap_sol, 2),
             mcap_growth=round(st.mcap_sol / st.mcap0_sol - 1, 3) if st.mcap0_sol else None,
             drawdown=round(1 - st.mcap_sol / st.max_mcap_sol, 3) if st.max_mcap_sol else None,
             n_ultra=sum(1 for t in smart.values() if t == "ultra"),
             n_smart=sum(1 for t in smart.values() if t == "smart"),
             n_good=sum(1 for t in smart.values() if t == "good"))

    # ------------------------------------------------------------ combine
    a.reasons.sort(key=lambda r: -r["risk"])
    a.risk = round(noisy_or(r["risk"] for r in a.reasons), 1)
    score = a.momentum * (1 - a.risk / 100) * factor
    p = enr.get("model_p")
    if p is not None:
        F["model_p"] = round(float(p), 4)
        score = 0.8 * score + 0.2 * 100 * float(p) * (1 - a.risk / 100) * factor
    a.score = round(score, 1)
    F.update(momentum=a.momentum, risk=a.risk, score=a.score, safety_factor=factor)
    if enr.get("errors"):
        F["errors"] = enr["errors"][:6]
    if enr.get("early_src"):
        F["early_src"] = enr["early_src"]

    # wallets for the research table: bundle, insiders, dev, first 20 buyers
    rows = {(b["wallet"], "bundle"): dict(b, funder=funders.get(b["wallet"])) for b in bundle.values()}
    for w, role in a.insiders.items():
        if role != "bundle":
            rows[(w, role)] = {"wallet": w, "role": role, "tokens": holders.get(w), "funder": funders.get(w)}
    order = sorted(st.first_buy_ts.items(), key=lambda kv: kv[1])[:20]
    for w, ts in order:
        if (w, "early") not in rows and w != creator:
            rows[(w, "early")] = {"wallet": w, "role": "early", "ts": ts, "tokens": st.bought.get(w),
                                  "funder": funders.get(w)}
    for r in rows.values():
        got = st.bought.get(r["wallet"]) or r.get("tokens") or 0
        bal = st.balances.get(r["wallet"])
        r["sold_pct"] = round(100 * (1 - bal / got), 1) if got and bal is not None else None
    a.wallets = [r for r in rows.values() if not str(r["wallet"]).startswith("unbekannt-")]
    return a
