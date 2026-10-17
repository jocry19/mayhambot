"""Signal v2: where smart money accumulates.

v1 fires on a burst: smart wallets buying a coin inside the 30 min window. v2 looks at the last WINDOW_H hours and
fires when several distinct smart owners have built a position in a coin over time and still hold it:

- holders: Ultra/Smart entities (one per model cluster) that bought in the window and sold less than HOLD_SELL_MAX
  of it. Receiver wallets count too: a wallet funded by an Ultra/Smart wallet (research_funding, exchanges and hubs
  excluded) is that funder's money, so its buys count for the funder's entity and are listed as "Empfänger".
- the call needs MIN_HOLDERS holders, HOLD_SHARE of the entities that bought still holding, their net position at
  least NET_MULT x the v1 net minimum, first buys spread over at least MIN_SPAN_MIN (a single burst is v1's job),
  and a holder buy within FRESH_MIN (the accumulation is still going on, not a stale bag).
- the same vetoes as v1: thin pool, wash trading / high rug risk (safety), not a memecoin, stale price.

Calls go into the signals table with kind='v2', so alerts, dashboard, terminal, outcomes (high after the call) and
the target / stop follow-ups work as for v1; plan levels and the report are kept per kind.
"""
import json
import logging
import math
import os
import time

from core.config import settings
from signals import engine, plan

log = logging.getLogger("mayhambot.signals.v2")

KIND = "v2"
ENABLED = os.getenv("SIGNAL_V2", "true").lower() not in ("0", "false", "off", "no")
WINDOW_H = float(os.getenv("SIGNAL_V2_WINDOW_H", 6))
MIN_HOLDERS = int(os.getenv("SIGNAL_V2_MIN_HOLDERS", 3))
HOLD_SELL_MAX = 0.5          # sold less than half of what it bought in the window: still holding
HOLD_SHARE = float(os.getenv("SIGNAL_V2_HOLD_SHARE", 0.7))
NET_MULT = float(os.getenv("SIGNAL_V2_NET_MULT", 3))
MIN_SPAN_MIN = int(os.getenv("SIGNAL_V2_MIN_SPAN_MIN", 20))
FRESH_MIN = int(os.getenv("SIGNAL_V2_FRESH_MIN", 60))
COOLDOWN_H = float(os.getenv("SIGNAL_V2_COOLDOWN_H", 6))
RECALL_MORE_HOLDERS = 2      # inside the cooldown a coin is called again once this many more owners hold it
PRICE_MAX_AGE_S = 900
NET_REF_USD = 3000.0
HOLDERS_REF = 4.0
MODEL_VERSION = "accum-v2"

FUNNEL_KEYS = ("coins", "few_holders", "low_share", "low_net", "short_span", "stale", "stale_price", "thin_liq",
               "safety_drop", "cooldown", "fired")
last_funnel = {}


def receivers(conn, tiered):
    """{wallet: funder} for wallets funded by an Ultra/Smart wallet. Funders of many wallets are exchanges or hubs
    (research.funding.hubs), not one owner, and are skipped. Empty when research/ has not checked funders yet."""
    try:
        rows = conn.execute("SELECT wallet, funder FROM research_funding WHERE funder IS NOT NULL").fetchall()
    except Exception:  # noqa: BLE001 - research/ not set up on this DB
        return {}
    try:
        from research.config import rsettings
        hub_min = rsettings.funding_hub_min
    except Exception:  # noqa: BLE001
        hub_min = 12
    funded = {}
    for w, f in rows:
        funded[f] = funded.get(f, 0) + 1
    return {w: f for w, f in rows if f in tiered and w not in tiered and w != f and funded[f] < hub_min}


def candidates(conn, wallets, now):
    """Coins bought by MIN_HOLDERS+ of these wallets in the window, with one buy within FRESH_MIN."""
    if not wallets:
        return []
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS v2_wallets(address TEXT PRIMARY KEY)")
    conn.execute("DELETE FROM v2_wallets")
    conn.executemany("INSERT OR IGNORE INTO v2_wallets VALUES (?)", [(w,) for w in wallets])
    return [r[0] for r in conn.execute(
        """SELECT tr.mint FROM trades tr JOIN v2_wallets v ON v.address=tr.wallet
           WHERE tr.side='buy' AND tr.ts > ? AND tr.ts <= ? GROUP BY tr.mint
           HAVING COUNT(DISTINCT tr.wallet) >= ? AND MAX(tr.ts) > ?
           ORDER BY COUNT(DISTINCT tr.wallet) DESC LIMIT 200""",
        (now - int(WINDOW_H * 3600), now, MIN_HOLDERS, now - FRESH_MIN * 60))]


def metrics(conn, mint, qc, recv, now):
    """Accumulation of one coin: per owner (entity) what it bought and sold in the window, who still holds."""
    since = now - int(WINDOW_H * 3600)
    sol_usd = getattr(qc, "sol_usd", 0.0)
    per = {}
    for r in conn.execute("SELECT wallet, side, usd_amount, sol_amount, ts FROM trades WHERE mint=? AND ts>? AND ts<=?",
                          (mint, since, now)):
        w = r["wallet"]
        score, tier, _ = qc.get(w, mint)
        funder = None
        if tier not in ("ultra", "smart"):
            funder = recv.get(w)
            if not funder:
                continue
            score, tier, _ = qc.get(funder, mint)
            if tier not in ("ultra", "smart"):
                continue
        ent = qc.entity(funder or w)
        d = per.setdefault(ent, dict(entity=ent, tier=tier, score=score, buy_usd=0.0, sell_usd=0.0, buy_sol=0.0,
                                     sell_sol=0.0, first_buy=None, last_buy=None, wallets={}, receivers=set()))
        if engine.TIER_RANK.get(tier, 0) > engine.TIER_RANK.get(d["tier"], 0):
            d["tier"] = tier
        d["score"] = max(d["score"], score)
        usd = engine._usd(r, sol_usd)
        wd = d["wallets"].setdefault(w, dict(wallet=w, tier=tier, score=score, buy_usd=0.0, sell_usd=0.0,
                                             role="Empfänger" if funder else "Smart", funder=funder))
        if funder:
            d["receivers"].add(w)
        if r["side"] == "buy":
            d["buy_usd"] += usd
            d["buy_sol"] += r["sol_amount"] or 0
            wd["buy_usd"] += usd
            d["first_buy"] = r["ts"] if d["first_buy"] is None else min(d["first_buy"], r["ts"])
            d["last_buy"] = r["ts"] if d["last_buy"] is None else max(d["last_buy"], r["ts"])
        else:
            d["sell_usd"] += usd
            d["sell_sol"] += r["sol_amount"] or 0
            wd["sell_usd"] += usd
    bought = [d for d in per.values() if d["buy_usd"] > 0]
    holders = [d for d in bought if d["sell_usd"] < HOLD_SELL_MAX * d["buy_usd"]]
    net = sum(d["buy_usd"] - d["sell_usd"] for d in holders)
    firsts = [d["first_buy"] for d in holders]
    wsum = sum(d["buy_usd"] for d in holders)
    avg_q = sum(d["score"] * d["buy_usd"] for d in holders) / wsum if wsum else 0.0
    n = len(holders)
    share = n / len(bought) if bought else 0.0
    parts = {
        "holders": (0.35, 1 - math.exp(-n / HOLDERS_REF), f"{n} Smart-Besitzer halten"),
        "position": (0.35, 1 - math.exp(-max(net, 0) / NET_REF_USD), f"Netto-Position ${net:,.0f}"),
        "conviction": (0.15, share, f"{share * 100:.0f} % der Käufer halten noch"),
        "quality": (0.15, avg_q / 100, f"Ø Wallet-Score {avg_q:.0f}"),
    }
    contributions = {k: dict(weight=w, value=round(v, 4), points=round(100 * w * v, 1), why=why)
                     for k, (w, v, why) in parts.items()}
    wallets = []
    for d in sorted(holders, key=lambda d: d["buy_usd"], reverse=True):
        for wd in sorted(d["wallets"].values(), key=lambda x: x["buy_usd"], reverse=True):
            wallets.append(dict(wd, entity=d["entity"]))
    return dict(ts=now, mint=mint, holders=n, bought=len(bought), share=share, net_usd=net,
                buy_usd=sum(d["buy_usd"] for d in holders),
                buy_sol=sum(d["buy_sol"] for d in holders),
                net_sol=sum(d["buy_sol"] - d["sell_sol"] for d in holders),
                n_ultra=sum(1 for d in holders if d["tier"] == "ultra"),
                n_smart=sum(1 for d in holders if d["tier"] == "smart"),
                n_receivers=sum(len(d["receivers"]) for d in holders),
                n_sellers=len(bought) - n, avg_quality=avg_q,
                first_ts=min(firsts) if firsts else None, last_ts=max((d["last_buy"] for d in holders), default=None),
                span_min=(max(firsts) - min(firsts)) / 60 if firsts else 0.0,
                score=round(sum(c["points"] for c in contributions.values()), 1) if n and net > 0 else 0.0,
                contributions=contributions, wallets=wallets, model_version=MODEL_VERSION,
                smart_buyers=n)


def _reason(m, min_net, now, tok):
    if m["holders"] < MIN_HOLDERS:
        return "few_holders"
    if m["share"] < HOLD_SHARE:
        return "low_share"
    if m["net_usd"] < NET_MULT * min_net:
        return "low_net"
    if m["span_min"] < MIN_SPAN_MIN:
        return "short_span"
    if not m["last_ts"] or now - m["last_ts"] > FRESH_MIN * 60:
        return "stale"
    if not tok or not tok["price_usd"] or now - (tok["last_updated"] or 0) > PRICE_MAX_AGE_S:
        return "stale_price"
    if engine._thin(m):
        return "thin_liq"
    sp = m.get("safety") or {}
    if sp.get("red_flags") or sp.get("factor", 1.0) < engine.EV_MIN_SAFETY_FACTOR:
        return "safety_drop"
    return None


def explain(m, last_v1=None, now=None):
    pro = [f"{m['holders']} Smart-Besitzer ({m['n_ultra']} Ultra) haben in {WINDOW_H:.0f} h netto "
           f"${m['net_usd']:,.0f} aufgebaut und halten noch",
           f"Käufe über {m['span_min']:.0f} min verteilt"]
    if m["n_receivers"]:
        pro.append(f"{m['n_receivers']} Empfänger-Wallet{'s' if m['n_receivers'] > 1 else ''} von Smart-Wallets kauft mit")
    con = []
    if m["n_sellers"]:
        con.append(f"{m['n_sellers']} Smart-Käufer {'hat' if m['n_sellers'] == 1 else 'haben'} schon verkauft")
    liq = m.get("liquidity_usd")
    if liq is not None and liq < engine.STRICT_LIQ_USD:
        con.append(f"nur ${liq:,.0f} Liquidität")
    sp = m.get("safety") or {}
    if sp.get("risk") is not None and sp.get("factor", 1.0) < 1.0:
        con.append(f"Rug-Risiko {sp['risk']:.0f}" + (f": {sp['reasons'][0]}" if sp.get("reasons") else ""))
    elif sp.get("checked") is False:
        con.append("Rug-Check steht noch aus")
    if last_v1 and now:
        con.append(f"v1-Call auf den Coin vor {(now - last_v1) // 60} min (Preis evtl. schon gelaufen)")
    return "Dafür: " + "; ".join(pro) + (". Dagegen: " + "; ".join(con) if con else "")


def evaluate(conn, cfg=settings, now=None):
    """Find accumulating coins and store v2 calls. Returns the new signal rows (for notifier.notify)."""
    now = now or int(time.time())
    f = last_funnel
    f.clear()
    f.update({k: 0 for k in FUNNEL_KEYS}, max_holders=0, receivers=0)
    if not ENABLED:
        return []
    qc = engine.QualityCache(conn, cfg)
    tiered = {r[0] for r in conn.execute("SELECT address FROM wallets WHERE tier IN ('ultra','smart')")}
    tiered |= {w for w, v in qc.model_tiers.items() if v[0] in ("ultra", "smart")}
    recv = receivers(conn, tiered)
    f["receivers"] = len(recv)
    min_net = engine.min_net_usd(conn, cfg, now)
    f["min_net"] = NET_MULT * min_net
    new, lv = [], None
    for mint in candidates(conn, tiered | set(recv), now):
        tok = conn.execute("SELECT symbol, name, price_usd, mcap_usd, liquidity_usd, last_updated FROM tokens WHERE mint=?",
                           (mint,)).fetchone()
        if engine.not_a_memecoin(tok):
            continue
        m = metrics(conn, mint, qc, recv, now)
        m["liquidity_usd"] = tok["liquidity_usd"] if tok else None
        f["coins"] += 1
        f["max_holders"] = max(f["max_holders"], m["holders"])
        why = _reason(m, min_net, now, tok)
        if why is None:  # passes on numbers: now the safety vetoes (quick check first for an unchecked coin)
            m = engine.check_before_signal(conn, mint, engine.apply_safety(conn, mint, m))
            if (m.get("safety") or {}).get("checked") is False:
                why = "safety_drop"  # still unchecked after the quick check: no call on a coin nobody looked at
            else:
                why = _reason(m, min_net, now, tok)
        if why:
            f[why] += 1
            continue
        last = conn.execute("SELECT ts, smart_buyers FROM signals WHERE mint=? AND kind=? ORDER BY ts DESC LIMIT 1",
                            (mint, KIND)).fetchone()
        if last and now - last["ts"] < COOLDOWN_H * 3600 and not (
                m["holders"] >= (last["smart_buyers"] or 0) + RECALL_MORE_HOLDERS and now - last["ts"] >= 3600):
            f["cooldown"] += 1
            continue
        f["fired"] += 1
        call_no = 1 + conn.execute("SELECT COUNT(*) FROM signals WHERE mint=? AND kind=? AND ts>?",
                                   (mint, KIND, now - 86400)).fetchone()[0]
        v1 = conn.execute("SELECT MAX(ts) FROM signals WHERE mint=? AND COALESCE(kind,'v1')='v1' AND ts>?",
                          (mint, now - 6 * 3600)).fetchone()[0]
        reason = explain(m, v1, now)
        if call_no > 1:
            reason += f". v2-Call Nr. {call_no}: inzwischen {m['holders']} statt {last['smart_buyers']} Besitzer"
        if lv is None:
            lv = plan.levels(conn, now, kind=KIND)
        sp = m.get("safety") or {}
        cur = conn.execute(
            """INSERT INTO signals(ts, mint, symbol, score, net_usd, buy_usd, smart_buyers, avg_quality, price_usd,
                                   mcap_usd, wallets_json, buy_sol, net_sol, liquidity_usd, n_ultra, n_smart, n_good,
                                   contributions_json, model_version, score_raw, safety_factor, safety_risk,
                                   safety_level, safety_reasons_json, reason, call_no, target_ret, stop_ret,
                                   lag_first_s, lag_last_s, kind)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (now, mint, tok["symbol"] or "", m["score"], m["net_usd"], m["buy_usd"], m["holders"], m["avg_quality"],
             tok["price_usd"], tok["mcap_usd"], json.dumps(m["wallets"], ensure_ascii=False), m["buy_sol"],
             m["net_sol"], tok["liquidity_usd"], m["n_ultra"], m["n_smart"], 0, json.dumps(m["contributions"],
                                                                                             ensure_ascii=False),
             MODEL_VERSION, m.get("score_raw", m["score"]), sp.get("factor"), sp.get("risk"), sp.get("level"),
             json.dumps(sp.get("reasons") or [], ensure_ascii=False), reason, call_no, lv["target"], lv["stop"],
             now - m["first_ts"], now - m["last_ts"], KIND))
        new.append(conn.execute("SELECT * FROM signals WHERE id=?", (cur.lastrowid,)).fetchone())
        log.info("signal v2 %s: %d holders, net $%.0f over %.0f min", tok["symbol"], m["holders"], m["net_usd"],
                 m["span_min"])
    conn.commit()
    return new
