"""Research report: what predicts a pump, which wallets are the winners, how good the model is.
Returns (markdown, data) — markdown for humans, data (JSON) for the dashboard."""
import math
import time
from collections import defaultdict

from model.config import TIER_LABELS, msettings
from research import stats
from research.config import rsettings
from model.learn import weights_full
from research.events import FEATURE_LABELS, FEATURE_NAMES
from research.wallet_stats import COMPONENT_LABELS, COMPONENTS


def _pct(x):
    return "–" if x is None else f"{100 * x:.0f} %"


def _usd(x):
    if x is None:
        return "–"
    a = abs(x)
    return f"{x / 1e6:.1f} M$" if a >= 1e6 else f"{x / 1e3:.0f} k$" if a >= 1e3 else f"{x:.0f} $"


def _num(x):
    return "–" if x is None else f"{x:.0f}"


def _bucket(v, edges, labels):
    for e, lab in zip(edges, labels):
        if v < e:
            return lab
    return labels[-1]


def combos(events, horizon):
    rows = [e for e in events if e["labels"].get(horizon)]
    if not rows:
        return {}
    def tiermix(f):
        u, s, g = int(f["n_ultra"]), int(f["n_smart"]), int(f["n_good"])
        return f"U{min(u, 2)}{'+' if u >= 2 else ''} S{min(s, 3)}{'+' if s >= 3 else ''} G{min(g, 3)}{'+' if g >= 3 else ''}"

    def inflow(f):
        net = sum(math.copysign(math.expm1(abs(f[k])), f[k]) for k in ("net_sol_ultra", "net_sol_smart", "net_sol_good"))
        return _bucket(net, [0.01, 5, 20, 50], ["≤0 SOL", "0–5 SOL", "5–20 SOL", "20–50 SOL", ">50 SOL"])

    def mcap(f):
        return _bucket(10 ** f["log_mcap"], [50e3, 200e3, 1e6, 5e6], ["<50k", "50–200k", "200k–1M", "1–5M", ">5M"])

    def age(f):
        return _bucket(10 ** f["log_age"] - 1, [30, 180, 1440], ["<30 min", "30 min–3 h", "3–24 h", ">1 Tag"])

    dims = {"Tier-Mix (Ultra/Smart/Good-Käufer)": tiermix, "Smart-Money-Inflow": inflow,
            "Market Cap beim Signal": mcap, "Coin-Alter": age}
    out = {}
    for dname, fn in dims.items():
        g = defaultdict(lambda: [0, 0, []])
        for e in rows:
            k = fn(e["f"])
            g[k][0] += 1
            g[k][1] += e["labels"][horizon]["pumped"]
            g[k][2].append(e["labels"][horizon]["max_ret"])
        out[dname] = sorted(({"bucket": k, "n": n, "pumps": p, "rate": stats.wilson(p, n)[0],
                              "ci_low": stats.wilson(p, n)[1], "median_max_ret": stats.median(mr)}
                             for k, (n, p, mr) in g.items()), key=lambda d: d["bucket"])
    # best joint combos: tier mix x inflow x mcap, ranked by the lower confidence bound
    g = defaultdict(lambda: [0, 0, []])
    for e in rows:
        k = f"{tiermix(e['f'])} | {inflow(e['f'])} | MC {mcap(e['f'])}"
        g[k][0] += 1
        g[k][1] += e["labels"][horizon]["pumped"]
        g[k][2].append(e["labels"][horizon]["max_ret"])
    joint = [{"combo": k, "n": n, "pumps": p, "rate": stats.wilson(p, n)[0], "ci_low": stats.wilson(p, n)[1],
              "median_max_ret": stats.median(mr)} for k, (n, p, mr) in g.items() if n >= 4]
    out["Beste Kombinationen"] = sorted(joint, key=lambda d: -d["ci_low"])[:15]
    return out


def univariate(events, horizon):
    rows = [e for e in events if e["labels"].get(horizon)]
    y = [e["labels"][horizon]["pumped"] for e in rows]
    out = []
    for i, fn in enumerate(FEATURE_NAMES):
        a = stats.auc([e["x"][i] for e in rows], y) if rows else None
        out.append({"feature": fn, "label": FEATURE_LABELS[fn], "auc": a,
                    "direction": None if a is None else ("höher = besser" if a >= 0.5 else "niedriger = besser"),
                    "power": None if a is None else abs(a - 0.5) * 2})
    return sorted(out, key=lambda d: -(d["power"] or 0))


def tiers_vs_paper(conn, min_n=3):
    """Copy-trader paper results per tier of the copied wallet (closed positions), and wallets whose copies lose:
    candidates for a downgrade. The tier itself stays the wallet's own skill; this shows whether copying pays."""
    try:
        rows = conn.execute("""SELECT p.wallet, COALESCE(m.tier, 'ohne'), p.pnl_pct, p.pnl_sol FROM ct_positions p
                               LEFT JOIN model_wallet_stats m ON m.address = p.wallet
                               WHERE p.status = 'closed' AND p.wallet IS NOT NULL AND p.wallet != ''""").fetchall()
    except Exception:  # noqa: BLE001  (copytrade tables missing)
        return {}
    by_tier, by_wallet = defaultdict(list), defaultdict(list)
    for w, t, pct, sol in rows:
        by_tier[t].append((pct or 0.0, sol or 0.0))
        by_wallet[(w, t)].append((pct or 0.0, sol or 0.0))
    tiers = [dict(tier=t, n=len(v), win_rate=sum(1 for x in v if x[0] > 0) / len(v),
                  mean_pct=sum(x[0] for x in v) / len(v), pnl_sol=sum(x[1] for x in v))
             for t, v in sorted(by_tier.items(), key=lambda kv: ("ultra", "smart", "good", "ohne").index(kv[0])
                                if kv[0] in ("ultra", "smart", "good", "ohne") else 9)]
    losers = sorted((dict(address=w, tier=t, n=len(v), mean_pct=sum(x[0] for x in v) / len(v),
                          pnl_sol=sum(x[1] for x in v)) for (w, t), v in by_wallet.items()
                     if len(v) >= min_n and t != "ohne" and sum(x[1] for x in v) < 0), key=lambda d: d["pnl_sol"])
    return dict(tiers=tiers, downgrade=losers[:15])


def paper_vs_signals(conn):
    """Per grade: how stored predictions turned out, and the copy-trader's paper P&L on the same coins
    (paper position opened within 30 min after a prediction of that coin)."""
    out = {"signals": [], "paper": []}
    try:
        rows = conn.execute("""SELECT score, max_ret_1h, ret_1h FROM model_predictions
                               WHERE max_ret_1h IS NOT NULL""").fetchall()
    except Exception:  # noqa: BLE001
        rows = []
    def grade(sc):
        p = (sc or 0) / 100
        return "A" if p >= 0.6 else "B" if p >= 0.45 else "C" if p >= 0.3 else "D" if p >= 0.15 else "E"
    g = defaultdict(list)
    for r in rows:
        g[grade(r[0])].append(r)
    for k in sorted(g):
        v = g[k]
        out["signals"].append(dict(grade=k, n=len(v), hit_50=sum(1 for r in v if (r[1] or 0) >= 0.5) / len(v),
                                   median_max_ret_1h=stats.median([r[1] for r in v]),
                                   median_ret_1h=stats.median([r[2] for r in v])))
    try:
        pos = conn.execute("""SELECT p.mode, p.pnl_pct, p.pnl_sol,
                   (SELECT score FROM model_predictions m WHERE m.mint = p.mint AND m.ts <= p.opened_at
                      AND m.ts >= p.opened_at - 1800 ORDER BY m.ts DESC LIMIT 1) AS score
                FROM ct_positions p WHERE p.status = 'closed'""").fetchall()
    except Exception:  # noqa: BLE001
        pos = []
    g = defaultdict(list)
    for r in pos:
        g[(r[0] or "?", grade(r[3]) if r[3] is not None else "ohne Signal")].append(r)
    for (mode, gr), v in sorted(g.items()):
        out["paper"].append(dict(mode=mode, grade=gr, n=len(v), win_rate=sum(1 for r in v if (r[1] or 0) > 0) / len(v),
                                 avg_pnl_pct=sum(r[1] or 0 for r in v) / len(v), pnl_sol=sum(r[2] or 0 for r in v)))
    return out


VARIANT_LABELS = dict(net="Gewichte auf Netto", excess="Gewichte auf Vorsprung vor anderen Käufern",
                      pumped="Gewichte auf Pump (alt)", prior="feste Startgewichte")


def build(conn, version_id, bt, tiers_now, weights, weight_info, models, prev=None):
    ms = msettings
    main = "1h" if any(h[0] == "1h" for h in ms.horizons) else ms.horizons[0][0]
    n_trades = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    span = conn.execute("SELECT MIN(ts), MAX(ts) FROM trades").fetchone()
    n_coins = conn.execute("SELECT COUNT(DISTINCT mint) FROM trades").fetchone()[0]
    n_wallets = conn.execute("SELECT COUNT(DISTINCT wallet) FROM trades").fetchone()[0]
    n_cand = conn.execute("SELECT COUNT(*) FROM research_candles").fetchone()[0]
    counts = {t: sum(1 for v in tiers_now.values() if v[0] == t) for t in ("ultra", "smart", "good")}
    evs = bt["events"]
    m = bt.get("metrics", {})
    data = dict(ts=int(time.time()), version=version_id, horizon=main,
                dataset=dict(trades=n_trades, coins=n_coins, wallets=n_wallets, candles=n_cand,
                             from_ts=span[0], to_ts=span[1], scored_wallets=len(tiers_now)),
                tier_counts=counts, weights=weights, weight_info=weight_info,
                tier_profiles={}, tier_forward=m.get("tier_forward", {}),
                combos=combos(evs, main), univariate=univariate(evs, main),
                backtest={k: v for k, v in m.items() if k not in ("tier_forward", "selection", "exits", "wallet_rank", "wallet_rank_h")},
                exits=m.get("exits") or {}, wallet_rank=m.get("wallet_rank") or {},
                wallet_rank_h=m.get("wallet_rank_h") or {},
                models={h: {"kind": mm.get("kind"), "algo": (mm.get("cfg") or {}).get("algo", mm.get("kind")),
                            "drop": (mm.get("cfg") or {}).get("drop"), "n": mm.get("n"), "pos": mm.get("pos"),
                            "weights": sorted(({"feature": f, "label": FEATURE_LABELS[f], "weight": c}
                                               for f, c in zip(FEATURE_NAMES, weights_full(mm))),
                                              key=lambda d: -abs(d["weight"]))} for h, mm in models.items()},
                selection=m.get("selection", {}), paper=paper_vs_signals(conn), tier_paper=tiers_vs_paper(conn),
                n_events=len(evs), n_labelled=sum(1 for e in evs if e["labels"].get(main)),
                checkpoints=len(bt["checkpoints"]), tier_history=bt["tier_counts"], previous=prev)
    for t in ("ultra", "smart", "good"):
        S = [s for (tt, sc, s) in tiers_now.values() if tt == t]
        if S:
            data["tier_profiles"][t] = dict(
                n=len(S), win_rate=stats.median([s["win_rate"] for s in S]), roi=stats.median([s["roi"] for s in S]),
                pnl=sum(s["total_pnl_usd"] for s in S), entry_mcap=stats.median([s["median_entry_mcap"] for s in S]),
                followers=stats.median([s["influence_followers"] for s in S]),
                min_to_2x=stats.median([s["median_min_to_2x"] for s in S]))
    top = sorted(((w, v) for w, v in tiers_now.items()), key=lambda x: -x[1][1])[:25]
    data["top_wallets"] = [dict(address=w, tier=t, score=sc, n=s["n_tokens"], win_rate=s["win_rate"], roi=s["roi"],
                                pnl=s["total_pnl_usd"], entry_mcap=s["median_entry_mcap"],
                                followers=s["influence_followers"], min_to_2x=s["median_min_to_2x"])
                           for w, (t, sc, s) in top]
    data["funding"] = funding_summary(conn, tiers_now)
    return render(data), data


def funding_summary(conn, tiers_now):
    """How many wallets have a known funder, and how many tiered wallets share one (= likely one person)."""
    from research.funding import hubs
    try:
        rows = conn.execute("SELECT wallet, funder, ts, status FROM research_funding").fetchall()
    except Exception:  # noqa: BLE001
        return {}
    fmap = {r[0]: (r[1], r[2]) for r in rows if r[1]}
    skip = hubs(fmap)
    groups = {}
    for w, (t, _, _) in tiers_now.items():
        f = fmap.get(w)
        if t and f and f[0] not in skip:
            groups.setdefault(f[0], []).append(w)
    shared = {f: ws for f, ws in groups.items() if len(ws) > 1}
    return dict(checked=len(rows), found=len(fmap), deep=sum(1 for r in rows if r[3] == "deep"), hubs=len(skip),
                shared_groups=len(shared), wallets_in_shared=sum(len(v) for v in shared.values()))


def render(d):
    L = []
    ds = d["dataset"]
    span_h = ((ds["to_ts"] or 0) - (ds["from_ts"] or 0)) / 3600
    L.append(f"# Wallet-Forschung — Modell v{d['version']}  ({time.strftime('%d.%m.%Y %H:%M', time.localtime(d['ts']))})\n")
    L.append(f"**Datenbasis:** {ds['trades']:,} Trades, {ds['coins']:,} Coins, {ds['wallets']:,} Wallets, "
             f"{span_h:.0f} h Historie, {ds['candles']:,} Kerzen. Bewertet: {ds['scored_wallets']:,} Wallets mit echten Positionen.\n")
    c = d["tier_counts"]
    L.append(f"## Tiers jetzt\n\nUltra Smart **{c['ultra']}** · Smart **{c['smart']}** · Good **{c['good']}**\n")
    if d["tier_profiles"]:
        L.append("| Tier | Wallets | Ø Trefferquote | Ø ROI | PnL gesamt | Ø Einstiegs-MC | Ø Follower/10 min | Ø min bis 2x |")
        L.append("|---|---|---|---|---|---|---|---|")
        for t, p in d["tier_profiles"].items():
            L.append(f"| {TIER_LABELS[t]} | {p['n']} | {_pct(p['win_rate'])} | {_pct(p['roi'])} | {_usd(p['pnl'])} | "
                     f"{_usd(p['entry_mcap'])} | {_num(p['followers'])} | "
                     f"{_num(p['min_to_2x'])} |")
    fu = d.get("funding") or {}
    if fu.get("checked"):
        L.append(f"\n**Geldherkunft:** {fu['checked']} Wallets geprüft, bei {fu['found']} den ersten SOL-Absender gefunden "
                 f"({fu['deep']} mit zu langer Historie, {fu['hubs']} Börsen/Sammeladressen ignoriert). "
                 f"{fu['wallets_in_shared']} Tier-Wallets teilen sich mit anderen einen Absender "
                 f"({fu['shared_groups']} Gruppen) und zählen im Modell als eine Gruppe.\n")
    L.append("\n**Wallet-Score-Gewichte** (gelernt aus den Folge-Käufen, gemischt mit der Startannahme "
             f"zu {100 * (d['weight_info'].get('lam') or 0):.0f} % gelernt, {d['weight_info'].get('n', 0)} Beispiele, "
             f"Ziel: {VARIANT_LABELS.get(d['weight_info'].get('variant'), d['weight_info'].get('variant') or 'Netto')}):\n")
    L.append(" · ".join(f"{COMPONENT_LABELS[k]} {100 * d['weights'][k]:.0f} %" for k in COMPONENTS) + "\n")
    tf = d["tier_forward"]
    if tf:
        L.append(f"## Sagen die Tiers etwas voraus? (nächste Käufe, Pump ≥ Schwelle in {d['horizon']}, ohne Blick in die Zukunft)\n")
        L.append("| Tier | Käufe | Pump-Quote | 95 %-Bereich | Median max. Anstieg (max. +5000 %) | Ø nach Kosten (nachgekauft) | Gewinnquote nach Kosten |")
        L.append("|---|---|---|---|---|---|---|")
        for t in ("ultra", "smart", "good", "none"):
            if t in tf:
                v = tf[t]
                L.append(f"| {TIER_LABELS.get(t, 'ohne Tier')} | {v['n']} | {_pct(v['pump_rate'])} | "
                         f"{_pct(v['ci_low'])}–{_pct(v['ci_high'])} | {_pct(v['median_max_ret'])} | "
                         f"{_pct(v.get('mean_net'))} | {_pct(v.get('win_rate_net'))} |")
        L.append("")
    for dname, rows in d["combos"].items():
        if not rows:
            continue
        L.append(f"## {dname} → Pump-Quote ({d['horizon']})\n")
        key = "combo" if dname == "Beste Kombinationen" else "bucket"
        L.append(f"| {'Kombination' if key == 'combo' else 'Gruppe'} | Signale | Pumps | Quote | sicher mind. | Median max. Anstieg |")
        L.append("|---|---|---|---|---|---|")
        for r in rows:
            L.append(f"| {r[key]} | {r['n']} | {r['pumps']} | {_pct(r['rate'])} | {_pct(r['ci_low'])} | {_pct(r['median_max_ret'])} |")
        L.append("")
    if d["univariate"]:
        L.append(f"## Welche Merkmale trennen Pumps von Nicht-Pumps ({d['horizon']})\n")
        L.append("| Merkmal | Trennschärfe (AUC) | Richtung |")
        L.append("|---|---|---|")
        for u in d["univariate"][:12]:
            if u["auc"] is not None:
                L.append(f"| {u['label']} | {u['auc']:.2f} | {u['direction']} |")
        L.append("")
    L.append("## Modell pro Horizont\n")
    algo_name = {"logistic": "logistische Regression", "gbm": "Gradient Boosting (Entscheidungsbäume)", "base": "nur Basisrate"}
    for h, mm in d["models"].items():
        if mm["kind"] == "prior":
            kind = "Startannahme (noch zu wenig Daten)"
        else:
            kind = f"gelernt: {algo_name.get(mm['algo'], mm['algo'])}" + (f", ohne Gruppe {mm['drop']}" if mm.get("drop") else "")
        unit = "Anteil am Lernerfolg" if mm.get("algo") == "gbm" else "Gewicht pro Std.-Abw."
        L.append(f"**{h}** — {kind}, {mm['n']} Beispiele, {mm['pos']} Pumps. Wichtigste Merkmale ({unit}): " +
                 ", ".join(f"{w['label']} {w['weight']:+.2f}" for w in mm["weights"][:6]) + "\n")
    sel = d.get("selection") or {}
    if any(v.get("status") in ("selected", "base") for v in sel.values()):
        L.append("## Automatische Modellauswahl (ehrlicher Test auf den neuesten 30 % der Fälle)\n")
        L.append("| Horizont | gewählt | Grund | Test-Fälle | Log-Loss Modell | Log-Loss ohne Wissen | AUC |")
        L.append("|---|---|---|---|---|---|---|")
        for h, v in sel.items():
            if v.get("status") == "base":
                L.append(f"| {h} | nur Basisrate | {v.get('reason', '')} | {v.get('n_test', '–')} | – | "
                         f"{v.get('base_logloss', float('nan')):.3f} | – |")
                continue
            if v.get("status") != "selected":
                L.append(f"| {h} | Startannahme | {v.get('reason', '')} | – | – | – | – |")
                continue
            ho = v.get("holdout") or {}
            c = v["chosen"]
            L.append(f"| {h} | {algo_name.get(c['algo'], c['algo'])}{' ohne ' + c['drop'] if c.get('drop') else ''} | {v['why']} | "
                     f"{v['n_test']} | {ho.get('logloss', float('nan')):.3f} | {v['base_logloss']:.3f} | "
                     f"{'–' if ho.get('auc') is None else round(ho['auc'], 2)} |")
        L.append("")
    pp = d.get("paper") or {}
    if pp.get("signals") or pp.get("paper"):
        L.append("## Signale gegen Papier-Trading\n")
        if pp.get("signals"):
            L.append("| Note | Signale | +50 % in 1h erreicht | Median max. Anstieg 1h | Median Ergebnis nach 1h |")
            L.append("|---|---|---|---|---|")
            for r in pp["signals"]:
                L.append(f"| {r['grade']} | {r['n']} | {_pct(r['hit_50'])} | {_pct(r['median_max_ret_1h'])} | {_pct(r['median_ret_1h'])} |")
            L.append("")
        if pp.get("paper"):
            L.append("| Copy-Trader-Modus | Note beim Einstieg | Trades | Gewinnquote | Ø P&L | P&L gesamt (SOL) |")
            L.append("|---|---|---|---|---|---|")
            for r in pp["paper"]:
                L.append(f"| {r['mode']} | {r['grade']} | {r['n']} | {_pct(r['win_rate'])} | {100 * r['avg_pnl_pct']:+.1f} % | {r['pnl_sol']:+.3f} |")
            L.append("")
    tp = d.get("tier_paper") or {}
    if tp.get("tiers"):
        L.append("## Stufen gegen Papier-Kopien (was das Kopieren der Wallets gebracht hat)\n")
        L.append("| Stufe der kopierten Wallet | Trades | Gewinnquote | Ø P&L | P&L gesamt (SOL) |")
        L.append("|---|---|---|---|---|")
        for r in tp["tiers"]:
            L.append(f"| {TIER_LABELS.get(r['tier'], 'ohne Stufe')} | {r['n']} | {_pct(r['win_rate'])} | "
                     f"{100 * r['mean_pct']:+.1f} % | {r['pnl_sol']:+.3f} |")
        if tp.get("downgrade"):
            L.append("\n**Vorschlag zum Abstufen** (≥ 3 Kopien, zusammen im Minus): " + ", ".join(
                f"`{r['address'][:6]}…` {TIER_LABELS.get(r['tier'], r['tier'])} ({r['n']} Trades, {r['pnl_sol']:+.3f} SOL)"
                for r in tp["downgrade"]) + "\n")
        L.append("")
    bt = d["backtest"]
    if any(v.get("n") for v in bt.values()):
        L.append("## Walk-Forward-Backtest (jede Vorhersage nur mit Daten von davor)\n")
        L.append("| Horizont | Signale | Basis-Pumpquote | AUC Modell | AUC alter Score | Top-10 % Pumpquote | Median max. Anstieg Top-10 % |")
        L.append("|---|---|---|---|---|---|---|")
        for h, v in bt.items():
            if v.get("n"):
                L.append(f"| {h} | {v['n']} | {_pct(v['base_rate'])} | {v['auc'] if v['auc'] is None else round(v['auc'], 2)} | "
                         f"{v['auc_baseline'] if v['auc_baseline'] is None else round(v['auc_baseline'], 2)} | "
                         f"{_pct(v['top10_pump_rate'])} | {_pct(v['top10_median_max_ret'])} |")
        L.append("")
        L.append(f"**Nach Kosten** (Verkauf an der Pump-Schwelle, sonst am Horizont-Ende, minus {100 * rsettings.cost_roundtrip:.0f} % je Umlauf):\n")
        L.append("| Horizont | Fälle | Ø netto alle | Gewinnquote alle | Ø netto oberstes Fünftel (Pump-Score) | "
                 "AUC Netto-Modell | Ø netto oberstes Fünftel (Netto-Modell) | Kalibrierfehler Pump |")
        L.append("|---|---|---|---|---|---|---|---|")
        for h, v in bt.items():
            nm = v.get("net") or {}
            if v.get("n") and nm:
                L.append(f"| {h} | {nm['n']} | {_pct(nm['mean_net_all'])} | {_pct(nm['win_rate_all'])} | "
                         f"{_pct(nm['mean_net_top_pump'])} | {'–' if nm.get('auc_net') is None else round(nm['auc_net'], 2)} | "
                         f"{_pct(nm.get('mean_net_top_net'))} | {_pct((v.get('calibration') or {}).get('ece'))} |")
        L.append("")
        for h, v in bt.items():
            qs = v.get("quintiles") if v.get("n") else None
            if qs and qs[0].get("mean_net") is not None:
                L.append(f"Score-Fünftel {h} (1 = höchster Score), Ø netto: " +
                         " · ".join(f"{q['quintile']}: {_pct(q['mean_net'])} (n {q['n']})" for q in qs) + "\n")
        for h, v in bt.items():
            nm = v.get("net") or {}
            for key, name in (("bands_pump", "Pump-Score"), ("bands_net", "Netto-Modell")):
                if nm.get(key):
                    L.append(f"Score-Bänder {h} ({name}), Median netto: " + " · ".join(
                        f"{b['band']}: {_pct(b['median_net'])} (n {b['n']})" for b in nm[key]) + "\n")
            rc, rn = nm.get("rank_corr_pump_net"), nm.get("rank_corr_netmodel_net")
            if rc is not None:
                L.append(f"Rangkorrelation {h} mit Netto: Pump-Score {rc:+.2f}"
                         + (f", Netto-Modell {rn:+.2f}" if rn is not None else "")
                         + f"; unterstes Fünftel nach Pump-Score Ø {_pct(nm.get('mean_net_bottom_pump'))}\n")
        wr = d.get("wallet_rank") or {}
        if wr.get("all"):
            names = dict(VARIANT_LABELS, all="alle Käufe")
            L.append("Wallet-Rangfolge vorwärts (oberstes Fünftel der Wallets nach Score, nächster Kauf, netto): " + " · ".join(
                f"{names.get(k, k)} Ø {_pct(v['mean_net'])} (Untergrenze {_pct(v['ci_low'])}, Median {_pct(v['median_net'])}, n {v['n']})"
                for k, v in wr.items()) + "\n")
        for hn, row in (d.get("wallet_rank_h") or {}).items():
            parts = [f"{VARIANT_LABELS.get(k, k)}: oberstes Fünftel {_pct(v['mean_net_top'])} ({'+' if v['lift'] >= 0 else ''}"
                     f"{100 * v['lift']:.1f} Pkt. gegen alle), Rangkorrelation "
                     + ("–" if v["rank_corr"] is None else f"{v['rank_corr']:+.2f}")
                     + ("" if v.get("lift_at_fill") is None else f", zum Kurs der Wallet selbst {100 * v['lift_at_fill']:+.1f} Pkt.")
                     for k, v in row.items() if k != "all"]
            L.append(f"Wallet-Score vorwärts {hn} (alle Käufe Ø {_pct(row['all']['mean_net'])}, n {row['all']['n']}): "
                     + " · ".join(parts) + "\n")
        ex = d.get("exits") or {}
        if ex.get("top"):
            def rule(r):
                return (f"Ziel +{100 * r['tp']:.0f} %, " + (f"Stopp {100 * r['sl']:.0f} %" if r["sl"] is not None else "kein Stopp")
                        + f", max. {r['hold']}")
            L.append(f"### Ausstiege nach Kosten (oberstes Fünftel nach Modell, {ex['n_top']} von {ex['n']} Signalen)\n")
            L.append("| Regel | Fälle | Ø netto | Gewinnquote |")
            L.append("|---|---|---|---|")
            for r in ex["top"]:
                L.append(f"| {rule(r['rule'])} | {r['n']} | {_pct(r['mean_net'])} | {_pct(r['win_rate'])} |")
            o = ex.get("out_of_sample")
            if o:
                L.append(f"\nEhrlicher Test: Die beste Regel der älteren 60 % ({rule(o['rule'])}, dort {_pct(o['in_sample_mean'])}) "
                         f"bringt auf den neueren 40 % **{_pct(o['mean_net'])}** im Schnitt nach Kosten "
                         f"(Gewinnquote {_pct(o['win_rate'])}, n {o['n']}).\n")
    else:
        L.append(f"_Backtest: noch zu wenig Historie ({d['checkpoints']} Prüfpunkte). Er läuft automatisch, sobald mehr Daten da sind._\n")
    if d["top_wallets"]:
        L.append("## Top-Wallets\n")
        L.append("| Wallet | Tier | Score | Coins | Trefferquote | ROI | PnL | Einstiegs-MC | Follower |")
        L.append("|---|---|---|---|---|---|---|---|---|")
        for w in d["top_wallets"][:20]:
            L.append(f"| `{w['address'][:6]}…{w['address'][-4:]}` | {TIER_LABELS.get(w['tier'], '–')} | {w['score']:.0f} | {w['n']} | "
                     f"{_pct(w['win_rate'])} | {_pct(w['roi'])} | {_usd(w['pnl'])} | {_usd(w['entry_mcap'])} | "
                     f"{_num(w['followers'])} |")
    p = d.get("previous")
    if p:
        L.append(f"\n## Entwicklung gegenüber v{p['version']}\n")
        L.append(f"Tiers vorher: Ultra {p['tier_counts'].get('ultra', 0)}, Smart {p['tier_counts'].get('smart', 0)}, "
                 f"Good {p['tier_counts'].get('good', 0)}. AUC 1h vorher: {p.get('auc')}\n")
    return "\n".join(L)
