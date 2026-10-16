"""The individual safety checks. Each returns a CheckResult with a risk 0..100 and plain German reasons.

A Context fetches every piece of on-chain/market data at most once per assessment and caches the
expensive, stable parts (launch slots, wallet funders) in the DB, so re-checks of a coin are cheap.
"""
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field

from core.models import SOL_MINT

from safety import store
from safety.sources import BudgetExceeded

log = logging.getLogger("mayhambot.safety")

SYSTEM_PROGRAM = "11111111111111111111111111111111"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
INCINERATOR = "1nc1nerator11111111111111111111111111111111"
# owners whose token accounts are pools/curves/lockers, never a person's bag
KNOWN_PROGRAM_OWNERS = {
    "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1": "Raydium AMM",
    "GpMZbSM2GgvTKHJirzeGfMFoaZ8UR2X7F4v8vHTvxFbL": "Raydium CPMM",
    INCINERATOR: "Burn",
}
CONCENTRATED_DEXES = {"meteora", "orca", "raydium-clmm", "meteoradlmm"}


def short(a):
    return f"{a[:4]}…{a[-4:]}" if a and len(a) > 10 else (a or "?")


def ramp(x, lo, hi, rlo, rhi):
    """0 below lo, rlo..rhi between lo and hi, rhi above."""
    if x is None or x < lo:
        return 0.0
    if x >= hi or hi <= lo:
        return float(rhi)
    return rlo + (rhi - rlo) * (x - lo) / (hi - lo)


def noisy_or(risks):
    p = 1.0
    for r in risks:
        p *= 1 - max(0.0, min(100.0, r)) / 100
    return 100 * (1 - p)


def load_exchanges(path):
    out = {}
    try:
        with open(path) as f:
            for line in f:
                parts = line.split("#")[0].split()
                if parts:
                    out[parts[0]] = " ".join(parts[1:]) or "Börse"
    except OSError:
        pass
    return out


@dataclass
class CheckResult:
    name: str
    label: str
    status: str = "ok"            # ok | unknown | skipped | error
    risk: float = 0.0
    hard: bool = False            # a single hard flag blocks the coin whatever the rest says
    reasons: list = field(default_factory=list)
    positives: list = field(default_factory=list)
    data: dict = field(default_factory=dict)

    def add(self, risk, text):
        if risk > 0:
            self.reasons.append((round(risk, 1), text))

    def finish(self, cap=100.0):
        self.risk = round(min(cap, noisy_or(r for r, _ in self.reasons)), 1)
        return self


# ---------------------------------------------------------------- parsing helpers

def _keys(tx):
    return [k["pubkey"] if isinstance(k, dict) else k for k in tx["transaction"]["message"]["accountKeys"]]


def sol_deltas(tx):
    meta = tx["meta"]
    return {k: (post - pre) / 1e9 for k, pre, post in zip(_keys(tx), meta["preBalances"], meta["postBalances"])}


def token_deltas(tx, mint):
    """{owner: ui amount change} of `mint` in one transaction."""
    out = defaultdict(float)

    def amt(b):
        u = b.get("uiTokenAmount") or {}
        return float(u.get("uiAmountString") or u.get("uiAmount") or 0)
    for b in tx["meta"].get("preTokenBalances") or []:
        if b.get("mint") == mint and b.get("owner"):
            out[b["owner"]] -= amt(b)
    for b in tx["meta"].get("postTokenBalances") or []:
        if b.get("mint") == mint and b.get("owner"):
            out[b["owner"]] += amt(b)
    return out


def funder_of(tx, wallet):
    """Who sent `wallet` its SOL in this (its first) transaction -> (funder, sol)."""
    if not tx or not tx.get("meta"):
        return None, 0.0
    d = sol_deltas(tx)
    got = d.get(wallet, 0.0)
    if got > 0:
        sender = min((k for k in d if k != wallet), key=lambda k: d[k], default=None)
        if sender and d[sender] < 0:
            return sender, round(got, 4)
    payer = _keys(tx)[0]
    return (payer if payer != wallet else None), round(max(got, 0.0), 4)


# ---------------------------------------------------------------- context

class Context:
    def __init__(self, conn, mint, fetcher, cfg, deep=True, hot=False):
        self.conn, self.mint, self.f, self.cfg, self.deep = conn, mint, fetcher, cfg, deep
        self.hot = hot or deep   # hot coins (signal, position, buy wish) get fresher holder snapshots
        self.now = int(time.time())
        self.exchanges = load_exchanges(cfg.exchanges_file)
        self._memo = {}

    def _once(self, key, fn):
        if key not in self._memo:
            self._memo[key] = fn()
        return self._memo[key]

    def tx(self, sig):
        return self._once(("tx", sig), lambda: self.f.rpc(
            "getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1}]))

    def mint_info(self):
        def load():
            res = self.f.rpc("getAccountInfo", [self.mint, {"encoding": "jsonParsed"}]) or {}
            v = res.get("value")
            if not v:
                return None
            info = dict(((v.get("data") or {}).get("parsed") or {}).get("info") or {})
            info["program"] = v.get("owner")
            dec = int(info.get("decimals") or 0)
            info["supply_ui"] = float(info.get("supply") or 0) / 10 ** dec
            return info
        return self._once("mint", load)

    def pair(self):
        """Most liquid DexScreener pair of the token (or None)."""
        def load():
            pairs = [p for p in self.f.dexscreener_pairs(self.mint) or []
                     if (p.get("baseToken") or {}).get("address") == self.mint]
            return max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0, default=None)
        return self._once("pair", load)

    def rugcheck(self):
        if not self.cfg.use_rugcheck:
            return None

        def load():
            try:
                return self.f.rugcheck(self.mint)
            except Exception as e:  # noqa: BLE001 - optional source
                if isinstance(e, BudgetExceeded):
                    raise
                log.debug("rugcheck %s failed: %s", self.mint, e)
                return None
        return self._once("rugcheck", load)

    def holders(self):
        """Top holders aggregated by owner: [{owner, amount, pct, kind}] (kind: wallet|program|burn|pool)."""
        def load():
            # getTokenLargestAccounts is the RPC method public nodes limit hardest: one snapshot per mint is
            # shared by every process for SAFETY_HOLDERS_TTL_HOT_SEC / SAFETY_HOLDERS_TTL_SEC
            ttl = self.cfg.holders_ttl_hot_sec if self.hot else self.cfg.holders_ttl_sec
            cached = store.cache_get(self.conn, "holders:" + self.mint, ttl)
            if cached is not None:
                return cached
            out = fetch()
            if out:
                store.cache_put(self.conn, "holders:" + self.mint, out)
            return out

        def fetch():
            mi = self.mint_info()
            supply = (mi or {}).get("supply_ui") or 0
            largest = (self.f.rpc("getTokenLargestAccounts", [self.mint]) or {}).get("value") or []
            if not largest or not supply:
                return []
            accs = [a["address"] for a in largest]
            infos = (self.f.rpc("getMultipleAccounts", [accs, {"encoding": "jsonParsed"}]) or {}).get("value") or []
            by_owner = defaultdict(float)
            for a, inf in zip(largest, infos):
                owner = ((((inf or {}).get("data") or {}).get("parsed") or {}).get("info") or {}).get("owner")
                by_owner[owner or a["address"]] += float(a.get("uiAmountString") or a.get("uiAmount") or 0)
            owners = list(by_owner)
            oinfo = (self.f.rpc("getMultipleAccounts", [owners, {"encoding": "base64",
                     "dataSlice": {"offset": 0, "length": 0}}]) or {}).get("value") or [None] * len(owners)
            pair = self._memo.get("pair") or {}
            pool = pair.get("pairAddress")
            out = []
            for o, inf in zip(owners, oinfo):
                if o == INCINERATOR:
                    kind = "burn"
                elif o == pool:
                    kind = "pool"
                elif o in KNOWN_PROGRAM_OWNERS or (inf is not None and inf.get("owner") != SYSTEM_PROGRAM):
                    kind = "program"   # PDA of a pool, bonding curve or locker
                # inf None = the owner has no SOL account at all: an emptied burner wallet, i.e. a person.
                # (Counted as "program" before Oct 2026, which hid insiders who drained their SOL, e.g. SYCAMORE.)
                else:
                    kind = "wallet"
                out.append({"owner": o, "amount": by_owner[o], "pct": 100 * by_owner[o] / supply, "kind": kind})
            return sorted(out, key=lambda h: -h["amount"])
        return self._once("holders", load)

    def profile(self, address):
        """Age, activity and first funder of a wallet. Cached in safety_wallets for a day."""
        def load():
            p = store.wallet_profile(self.conn, address, self.cfg.wallet_profile_ttl_sec)
            if p:
                return p
            lim = self.cfg.fresh_wallet_max_txs
            sigs = self.f.rpc("getSignaturesForAddress", [address, {"limit": lim + 1}]) or []
            complete = len(sigs) <= lim
            p = {"address": address, "n_txs": len(sigs), "complete": complete,
                 "first_ts": sigs[-1].get("blockTime") if sigs else None,
                 "last_ts": sigs[0].get("blockTime") if sigs else None, "funder": None, "funded_sol": 0.0}
            if complete and sigs:
                p["funder"], p["funded_sol"] = funder_of(self.tx(sigs[-1]["signature"]), address)
            store.save_wallet_profile(self.conn, p)
            return p
        return self._once(("profile", address), load)

    def is_fresh(self, p):
        return bool(p and p["complete"] and p["first_ts"]
                    and self.now - p["first_ts"] < self.cfg.fresh_wallet_days * 86400)

    def launch(self):
        """Creator and every buyer of the first `bundle_slots` slots, cached forever (a launch never changes).
        Resumable: paging back to the launch and reading its transactions continue across runs when the
        RPC budget runs out, so a busy coin costs its calls once, spread over several cycles.
        state: paging -> txs -> done | unreachable. Only `done` has creator and buyers."""
        def load():
            d = store.launch(self.conn, self.mint)
            if d is not None and "state" not in d:      # rows from before resumable launches
                d["state"] = "done" if d.get("reachable") else "unreachable"
            if d is None:
                d = {"state": "paging", "pages": 0, "before": None, "tail": [], "reachable": None}
                pair = self.pair() if "pair" in self._memo else None
                created = int(float((pair or {}).get("pairCreatedAt") or 0) / 1000)
                if created and self.now - created > self.cfg.launch_max_age_h * 3600:
                    d.update(state="unreachable", reachable=False, why="too_old")
                    store.save_launch(self.conn, self.mint, d)
            if d["state"] in ("done", "unreachable"):
                return d
            try:
                self._launch_step(d)
            except BudgetExceeded:
                pass
            store.save_launch(self.conn, self.mint, d)
            return d
        return self._once("launch", load)

    def _launch_step(self, d):
        while d["state"] == "paging":
            if d["pages"] >= self.cfg.launch_max_pages:
                d.update(state="unreachable", reachable=False, why="too_many_txs")
                return
            opts = {"limit": 1000}
            if d["before"]:
                opts["before"] = d["before"]
            p = self.f.rpc("getSignaturesForAddress", [self.mint, opts]) or []
            d["pages"] += 1
            if p:
                d["before"] = p[-1]["signature"]
                d["tail"] = [{"signature": x["signature"], "slot": x["slot"], "blockTime": x.get("blockTime"),
                              "err": bool(x.get("err"))} for x in p[-300:]]
            if p and len(p) == 1000:
                continue
            # RPC lists newest first; reversed + stable sort = oldest first, creation tx at index 0
            sigs = sorted((x for x in reversed(d["tail"]) if not x.get("err")), key=lambda x: x["slot"])
            if not sigs:
                d.update(state="unreachable", reachable=False, why="no_signatures")
                return
            created = sigs[0]["slot"]
            launch = [x for x in sigs if x["slot"] < created + self.cfg.bundle_slots]
            d.update(state="txs", created_slot=created, created_ts=sigs[0].get("blockTime"), n_launch_txs=len(launch),
                     truncated=len(launch) > self.cfg.launch_max_txs, pending=launch[: self.cfg.launch_max_txs],
                     idx=0, buyers={}, creator=None, tail=[], before=None)
        while d["idx"] < len(d["pending"]):
            x = d["pending"][d["idx"]]
            tx = self.tx(x["signature"])
            if tx and tx.get("meta"):
                signer = _keys(tx)[0]
                if d["idx"] == 0:
                    d["creator"] = signer
                got = token_deltas(tx, self.mint).get(signer, 0.0)
                if got > 0:
                    b = d["buyers"].setdefault(signer, {"wallet": signer, "tokens": 0.0, "sol": 0.0, "slot": x["slot"]})
                    b["tokens"] += got
                    b["sol"] += max(0.0, -sol_deltas(tx).get(signer, 0.0))
            d["idx"] += 1
        d.update(state="done", reachable=True, buyers=list(d["buyers"].values()), pending=[])

    def creator(self):
        if self.deep:
            la = self.launch()
            if la.get("state") == "done" and la.get("creator"):
                return la["creator"]
        rc = self.rugcheck() or {}
        return rc.get("creator") or None


# ---------------------------------------------------------------- checks

def check_authorities(ctx):
    r = CheckResult("authorities", "Mint/Freeze-Rechte")
    mi = ctx.mint_info()
    if not mi:
        r.status = "unknown"
        r.add(20, "Mint-Konto nicht lesbar: Token-Rechte unbekannt.")
        return r.finish()
    if mi.get("mintAuthority"):
        r.hard = True
        r.add(85, "Mint-Authority aktiv: der Ersteller kann jederzeit neue Token drucken und den Kurs verwässern.")
    else:
        r.positives.append("Mint-Authority abgegeben (kein Nachdrucken möglich).")
    if mi.get("freezeAuthority"):
        r.hard = True
        r.add(85, "Freeze-Authority aktiv: der Ersteller kann deine Token einfrieren, Verkaufen wäre dann unmöglich.")
    else:
        r.positives.append("Freeze-Authority abgegeben.")
    r.data["token_2022"] = mi.get("program") == TOKEN_2022
    for ext in mi.get("extensions") or []:
        name, state = ext.get("extension"), ext.get("state") or {}
        if name == "transferFeeConfig":
            fee = state.get("newerTransferFee") or state.get("olderTransferFee") or {}
            pct = float(fee.get("transferFeeBasisPoints") or 0) / 100
            r.data["transfer_fee_pct"] = pct
            if pct >= ctx.cfg.tax_crit_pct:
                r.hard = True
            r.add(ramp(pct, 0.01, ctx.cfg.tax_crit_pct, 20, 85), f"Transfer-Gebühr im Token eingebaut: {pct:.1f} % bei jedem Kauf/Verkauf.")
        elif name == "permanentDelegate":
            r.hard = True
            r.add(90, "Permanent Delegate: eine fremde Adresse darf Token aus jeder Wallet abziehen oder verbrennen.")
        elif name == "nonTransferable":
            r.hard = True
            r.add(95, "Token ist nicht übertragbar: Verkaufen geht nicht.")
        elif name == "transferHook":
            r.add(60, "Transfer-Hook: ein Programm des Erstellers prüft jeden Transfer und kann Verkäufe blockieren.")
        elif name == "defaultAccountState" and str(state.get("accountState", "")).lower() == "frozen":
            r.hard = True
            r.add(90, "Neue Token-Konten starten eingefroren: Käufer können nur mit Freigabe verkaufen.")
        elif name in ("pausableConfig", "pausable"):
            r.hard = True
            r.add(85, "Token kann pausiert werden: alle Transfers lassen sich stoppen.")
    return r.finish()


def check_sell_sim(ctx):
    r = CheckResult("sell_sim", "Verkaufs-Simulation")
    lamports = int(ctx.cfg.sim_size_sol * 1e9)
    buy = ctx.f.jupiter_quote(SOL_MINT, ctx.mint, lamports)
    if not buy or buy.get("error") or not int(buy.get("outAmount") or 0):
        r.status = "unknown"
        r.add(10, "Jupiter findet keinen Kaufweg (noch nicht handelbar oder zu wenig Liquidität).")
        return r.finish()
    sell = ctx.f.jupiter_quote(ctx.mint, SOL_MINT, int(buy["outAmount"]))
    if not sell or sell.get("error") or not int(sell.get("outAmount") or 0):
        r.hard = True
        r.add(85, "Kaufen geht, Verkaufen nicht: Jupiter findet keinen Verkaufsweg (Honeypot-Verdacht).")
        return r.finish()
    back = int(sell["outAmount"])
    loss = 100 * (1 - back / lamports)
    impact = 100 * (float(buy.get("priceImpactPct") or 0) + float(sell.get("priceImpactPct") or 0))
    tax = max(0.0, loss - impact - ctx.cfg.fee_allowance_pct)
    r.data.update(roundtrip_loss_pct=round(loss, 2), impact_pct=round(impact, 2), hidden_tax_pct=round(tax, 2),
                  size_sol=ctx.cfg.sim_size_sol)
    if tax >= ctx.cfg.tax_crit_pct:
        r.hard = True
    if tax >= ctx.cfg.tax_warn_pct:
        r.add(ramp(tax, ctx.cfg.tax_warn_pct, ctx.cfg.tax_crit_pct, 35, 85),
              f"Versteckte Verkaufssteuer: Kauf+Verkauf von {ctx.cfg.sim_size_sol:g} SOL verliert {loss:.1f} %, "
              f"davon nur {impact:.1f} % Kurseinfluss, ca. {tax:.0f} % unerklärt.")
    else:
        r.positives.append(f"Verkauf simuliert: Hin und zurück {loss:.1f} % Verlust (normal für Gebühren und Kurseinfluss).")
    return r.finish()


def check_holders(ctx):
    r = CheckResult("holders", "Holder-Konzentration")
    hs = ctx.holders()
    if not hs:
        r.status = "unknown"
        return r.finish()
    wallets = [h for h in hs if h["kind"] == "wallet"]
    top10 = sum(h["pct"] for h in wallets[:10])
    top1 = wallets[0]["pct"] if wallets else 0.0
    burned = sum(h["pct"] for h in hs if h["kind"] == "burn")
    r.data.update(top10_pct=round(top10, 2), top1_pct=round(top1, 2), burned_pct=round(burned, 2),
                  top=[{k: (round(v, 3) if isinstance(v, float) else v) for k, v in h.items()} for h in hs[:20]])
    c = ctx.cfg
    r.add(ramp(top10, c.top10_warn_pct, c.top10_crit_pct, 20, 70),
          f"Die 10 größten Holder (ohne Pools) halten {top10:.0f} % des Supplies: sie können den Kurs jederzeit drücken.")
    if wallets:
        r.add(ramp(top1, c.top1_warn_pct, c.top1_crit_pct, 20, 60),
              f"Eine einzelne Wallet ({short(wallets[0]['owner'])}) hält {top1:.1f} % des Supplies.")
    if top10 < c.top10_warn_pct:
        r.positives.append(f"Breit verteilt: Top-10-Holder halten {top10:.0f} %.")
    if burned >= 1:
        r.positives.append(f"{burned:.0f} % des Supplies sind verbrannt.")
    return r.finish(cap=80)


def check_insiders(ctx):
    r = CheckResult("insiders", "Frische/verbundene Holder")
    wallets = [h for h in ctx.holders() if h["kind"] == "wallet" and h["pct"] >= 0.5][: ctx.cfg.holders_profiled]
    if not wallets:
        r.status = "unknown"
        return r.finish()
    creator = ctx.creator()
    profiles = {h["owner"]: ctx.profile(h["owner"]) for h in wallets}
    pct = {h["owner"]: h["pct"] for h in wallets}
    fresh = [w for w, p in profiles.items() if ctx.is_fresh(p)]
    groups = defaultdict(list)
    for w, p in profiles.items():
        f = p.get("funder")
        if f and f not in ctx.exchanges and f != SYSTEM_PROGRAM:
            groups[f].append(w)
    linked = set()
    for f, ws in groups.items():
        if len(ws) >= 2 or (creator and f == creator):
            linked.update(ws)
    by_creator = [w for w in profiles if creator and profiles[w].get("funder") == creator]
    insiders = set(fresh) | linked
    ins_pct = sum(pct[w] for w in insiders)
    rc_pct = sum(float(h.get("pct") or 0) for h in ((ctx.rugcheck() or {}).get("topHolders") or [])
                 if h.get("insider"))
    r.data.update(fresh=fresh, linked=sorted(linked), insider_pct=round(ins_pct, 2),
                  funder_groups={f: ws for f, ws in groups.items() if len(ws) >= 2}, rugcheck_insider_pct=round(rc_pct, 2))
    c = ctx.cfg
    risk = ramp(max(ins_pct, rc_pct), c.insider_warn_pct, c.insider_crit_pct, 25, 75)
    parts = []
    if fresh:
        parts.append(f"{len(fresh)} der Top-{len(wallets)}-Holder sind frische Wallets "
                     f"(jünger als {c.fresh_wallet_days:g} Tage, kaum Transaktionen)")
    for f, ws in groups.items():
        if len(ws) >= 2:
            parts.append(f"{len(ws)} Top-Holder wurden von derselben Wallet {short(f)} finanziert")
    if by_creator:
        parts.append(f"{len(by_creator)} Top-Holder {'wurde' if len(by_creator) == 1 else 'wurden'} direkt vom Ersteller finanziert")
    if parts:
        r.add(max(risk, 15), "; ".join(parts) + f". Zusammen halten diese Insider {ins_pct:.0f} % des Supplies.")
    if rc_pct > ins_pct and rc_pct >= c.insider_warn_pct:
        r.add(risk, f"RugCheck markiert Insider unter den Top-Holdern mit zusammen {rc_pct:.0f} %.")
    if not parts and rc_pct < c.insider_warn_pct:
        r.positives.append(f"Keine frischen oder verbundenen Wallets unter den Top-{len(wallets)}-Holdern.")
    return r.finish(cap=85)


def check_bundles(ctx):
    r = CheckResult("bundles", "Bundle-Käufe beim Launch")
    la = ctx.launch()
    if la.get("state") in ("paging", "txs"):
        raise BudgetExceeded("launch analysis continues next cycle")
    if not la.get("reachable"):
        r.status = "unknown"
        r.reasons.append((0, "Launch nicht mehr erreichbar (zu viele Transaktionen); Bundle-Prüfung entfällt."))
        return r.finish()
    supply = (ctx.mint_info() or {}).get("supply_ui") or 0
    buyers = la.get("buyers") or []
    if not supply or not buyers:
        r.positives.append("Keine Käufe in den ersten Slots nach dem Launch.")
        return r.finish()
    creator = la.get("creator")
    same_block = [b for b in buyers if b["slot"] == la["created_slot"] and b["wallet"] != creator]
    total_pct = 100 * sum(b["tokens"] for b in buyers) / supply
    others = sorted((b for b in buyers if b["wallet"] != creator), key=lambda b: -b["tokens"])[:5]
    profiles = {b["wallet"]: ctx.profile(b["wallet"]) for b in others}
    groups = defaultdict(list)
    for w, p in profiles.items():
        f = p.get("funder")
        if f and f not in ctx.exchanges:
            groups[f].append(w)
    shared = {f: ws for f, ws in groups.items() if len(ws) >= 2 or f == creator}
    held = {h["owner"]: h["pct"] for h in ctx.holders()}
    still = sum(held.get(b["wallet"], 0.0) for b in buyers)
    r.data.update(n_buyers=len(buyers), n_same_block=len(same_block), launch_pct=round(total_pct, 2),
                  still_held_pct=round(still, 2), shared_funders=shared, creator=creator)
    c = ctx.cfg
    slots = f"den ersten {c.bundle_slots} Slots" if c.bundle_slots > 1 else "dem Launch-Slot"
    r.add(ramp(total_pct, c.bundle_warn_pct, c.bundle_crit_pct, 30, 75),
          f"{len(buyers)} Wallets kauften in {slots} zusammen {total_pct:.0f} % des Supplies"
          + (f" ({len(same_block)} davon im selben Block wie die Erstellung: Bundle)" if same_block else "") + ".")
    if still >= c.bundle_warn_pct / 2:
        r.add(ramp(still, c.bundle_warn_pct / 2, c.bundle_crit_pct, 25, 70),
              f"Diese Launch-Käufer halten noch {still:.0f} % und können jederzeit abladen.")
    for f, ws in shared.items():
        who = "dem Ersteller" if f == creator else f"derselben Wallet {short(f)}"
        r.add(45, f"{len(ws)} Launch-Käufer {'wurde' if len(ws) == 1 else 'wurden'} von {who} finanziert (verdecktes Dev-Bundle).")
    if total_pct < c.bundle_warn_pct and not shared:
        r.positives.append(f"Sauberer Launch: in {slots} nur {total_pct:.0f} % gekauft.")
    return r.finish(cap=90)


def check_dev(ctx):
    r = CheckResult("dev", "Dev-Wallet")
    creator = ctx.creator()
    supply = (ctx.mint_info() or {}).get("supply_ui") or 0
    if not creator or not supply:
        r.status = "unknown"
        return r.finish()
    r.data["creator"] = creator
    held = {h["owner"]: h["amount"] for h in ctx.holders()}
    smallest = min((h["pct"] for h in ctx.holders()), default=100.0)
    if creator in held:
        bal = held[creator]
    elif ctx.holders() and smallest < ctx.cfg.dev_hold_warn_pct:
        # not among the ~20 largest holders: holds at most the smallest of them, below the warn line (no call)
        bal = smallest / 100 * supply
        r.data["dev_below_top_holders"] = True
    else:
        res = ctx.f.rpc("getTokenAccountsByOwner", [creator, {"mint": ctx.mint}, {"encoding": "jsonParsed"}]) or {}
        bal = 0.0
        for a in res.get("value") or []:
            ta = ((((a.get("account") or {}).get("data") or {}).get("parsed") or {}).get("info") or {}).get("tokenAmount") or {}
            bal += float(ta.get("uiAmountString") or ta.get("uiAmount") or 0)
    dev_pct = 100 * bal / supply
    la = ctx.launch() if ctx.deep else {}
    initial = next((b["tokens"] for b in la.get("buyers") or [] if b["wallet"] == creator), 0.0) \
        if la.get("state") == "done" else 0.0
    c = ctx.cfg
    r.data.update(dev_pct=round(dev_pct, 3), dev_initial_pct=round(100 * initial / supply, 3))
    r.add(ramp(dev_pct, c.dev_hold_warn_pct, c.dev_hold_crit_pct, 25, 60),
          f"Der Ersteller ({short(creator)}) hält noch {dev_pct:.1f} % des Supplies.")
    if initial > 0:
        sold = max(0.0, 100 * (1 - bal / initial))
        r.data["dev_sold_pct"] = round(sold, 1)
        if sold >= c.dev_sold_warn_pct:  # a dumped 0.5 % dev buy matters less than a dumped 10 % bag
            r.add(30 if 100 * initial / supply >= 3 else 12,
                  f"Der Ersteller hat {sold:.0f} % seiner Start-Token ({100 * initial / supply:.1f} % des Supplies) schon abgegeben.")
    sold_db = ctx.conn.execute("SELECT COALESCE(SUM(usd_amount),0) FROM trades WHERE wallet=? AND mint=? AND side='sell'",
                               (creator, ctx.mint)).fetchone()[0]
    if sold_db:
        r.data["dev_sold_usd_seen"] = round(sold_db, 0)
    if ctx.deep and c.dev_txs_scanned > 0:
        sigs = ctx.f.rpc("getSignaturesForAddress", [creator, {"limit": c.dev_txs_scanned}]) or []
        cex, spread = defaultdict(float), set()
        for s in sigs:
            if s.get("err"):
                continue
            tx = ctx.tx(s["signature"])
            if not tx or not tx.get("meta"):
                continue
            d = sol_deltas(tx)
            for k, v in d.items():
                if k in ctx.exchanges and v > 0 and d.get(creator, 0) < 0:
                    cex[ctx.exchanges[k]] += v
            td = token_deltas(tx, ctx.mint)
            if td.get(creator, 0) < 0 and d.get(creator, 0) <= 0.001:
                spread.update(o for o, v in td.items() if v > 0 and o != creator)
        if cex:
            txt = ", ".join(f"{v:.2f} SOL an {k}" for k, v in cex.items())
            r.add(40, f"Der Ersteller schickt Geld an Börsen ({txt}): typisches Auscashen.")
        if spread:
            r.add(35, f"Der Ersteller hat Token ohne Verkauf an {len(spread)} {'andere Wallet' if len(spread) == 1 else 'andere Wallets'} verteilt (versteckte Bags).")
        r.data.update(cex_out=dict(cex), spread_to=sorted(spread))
    if not r.reasons:
        r.positives.append(f"Dev-Wallet unauffällig (hält {dev_pct:.1f} %).")
    return r.finish(cap=85)


def check_liquidity(ctx):
    r = CheckResult("liquidity", "Liquidität")
    p = ctx.pair()
    if not p:
        r.status = "unknown"
        r.add(15, "Kein Handelspaar bei DexScreener gefunden.")
        return r.finish()
    liq = float((p.get("liquidity") or {}).get("usd") or 0)
    mcap = float(p.get("marketCap") or p.get("fdv") or 0)
    r.data.update(dex=p.get("dexId"), pair=p.get("pairAddress"), liquidity_usd=liq, mcap_usd=mcap)
    if p.get("dexId") == "pumpfun" and not liq:
        r.positives.append("Läuft noch auf der pump.fun-Bonding-Curve.")
        return r.finish()
    c = ctx.cfg
    peak = ctx.conn.execute("SELECT MAX(liquidity_usd) FROM token_prices WHERE mint=? AND ts>=?",
                            (ctx.mint, ctx.now - 7 * 86400)).fetchone()[0] or 0.0
    r.data["peak_liquidity_usd_7d"] = peak
    if peak >= c.min_liquidity_usd and liq < peak * (1 - c.liq_drop_pct / 100):
        drop = 100 * (1 - liq / peak)
        r.hard = True
        r.add(90, f"Liquidität ist um {drop:.0f} % eingebrochen ({peak:,.0f} $ → {liq:,.0f} $): "
                  f"Rug Pull oder Pool geleert.".replace(",", "."))
    if liq < c.min_liquidity_usd:
        r.add(ramp(c.min_liquidity_usd - liq, 0, c.min_liquidity_usd, 20, 50),
              f"Nur {liq:,.0f} $ Liquidität: schon kleine Verkäufe bewegen den Kurs stark.".replace(",", "."))
    if mcap and liq and liq / mcap < c.min_liq_mcap_ratio:
        r.add(30, f"Liquidität ist nur {100 * liq / mcap:.1f} % der Market Cap: der Kurs ist kaum gedeckt.")
    if not r.reasons:
        r.positives.append(f"Liquidität {liq:,.0f} $ ({100 * liq / mcap:.0f} % der Market Cap).".replace(",", ".")
                           if mcap else f"Liquidität {liq:,.0f} $.".replace(",", "."))
    return r.finish()


def check_lp(ctx):
    r = CheckResult("lp", "LP-Lock")
    p = ctx.pair() or {}
    dex = (p.get("dexId") or "").lower()
    labels = [str(x).lower() for x in p.get("labels") or []]
    r.data["dex"] = dex
    if dex == "pumpfun":
        r.positives.append("pump.fun-Bonding-Curve: die Liquidität kann niemand abziehen.")
        return r.finish()
    rc = ctx.rugcheck() or {}
    markets = rc.get("markets") or []
    locked = None
    for m in markets:
        lp = m.get("lp") or {}
        if lp.get("lpLockedPct") is None:
            continue
        v = float(lp["lpLockedPct"])
        if m.get("pubkey") == p.get("pairAddress"):
            locked = v
            break
        locked = v if locked is None else max(locked, v)
    if locked is not None:
        r.data["lp_locked_pct"] = round(locked, 1)
        if locked >= ctx.cfg.lp_locked_ok_pct:
            r.positives.append(f"LP zu {locked:.0f} % verbrannt oder gesperrt.")
        else:
            r.add(ramp(ctx.cfg.lp_locked_ok_pct - locked, 0, ctx.cfg.lp_locked_ok_pct, 25, 75),
                  f"Nur {locked:.0f} % der Pool-Liquidität sind gesperrt/verbrannt: der Rest kann abgezogen werden (Rug Pull).")
        return r.finish()
    if dex == "pumpswap":
        r.status = "unknown"
        r.add(10, "PumpSwap-Pool: LP nach pump.fun-Migration normalerweise verbrannt, aber nicht bestätigt.")
    elif dex in CONCENTRATED_DEXES or any(x in ("clmm", "dlmm", "whirlpool") for x in labels):
        r.status = "unknown"
        r.add(30, "Konzentrierte Liquidität (CLMM/DLMM): Positionen können jederzeit abgezogen werden.")
    else:
        r.status = "unknown"
        r.add(15, "LP-Lock nicht prüfbar.")
    return r.finish()


def check_wash(ctx):
    r = CheckResult("wash", "Wash-Trading")
    since = ctx.now - ctx.cfg.wash_window_min * 60
    rows = ctx.conn.execute("SELECT wallet, side, usd_amount FROM trades WHERE mint=? AND ts>=?",
                            (ctx.mint, since)).fetchall()
    if len(rows) < ctx.cfg.wash_min_trades:
        r.status = "unknown"
        r.data["n_trades"] = len(rows)
        return r.finish()
    buy, sell = defaultdict(float), defaultdict(float)
    for w, side, usd in rows:
        (buy if side == "buy" else sell)[w] += usd or 0.0
    vol = defaultdict(float)
    for w in set(buy) | set(sell):
        vol[w] = buy[w] + sell[w]
    total = sum(vol.values()) or 1.0
    roundtrip = sum(2 * min(buy[w], sell[w]) for w in vol) / total
    top3 = sum(sorted(vol.values(), reverse=True)[:3]) / total
    c = ctx.cfg
    r.data.update(n_trades=len(rows), n_wallets=len(vol), roundtrip_share=round(roundtrip, 3), top3_share=round(top3, 3),
                  window_min=c.wash_window_min)
    # SYCAMORE (Oct 2026): 37-40 % round trips scored only ~23 and the copy trader lost 81 % of its paper loss
    # there. Fake volume fakes exactly the pumps copy trading and signals react to, so it weighs heavily now.
    if roundtrip >= c.wash_block_roundtrip:
        r.hard = True
        r.add(80, f"{100 * roundtrip:.0f} % des Volumens sind Hin-und-her-Trades derselben Wallets: das Volumen ist "
                  f"überwiegend künstlich, Pumps hier sind nicht echt (gesperrt ab {100 * c.wash_block_roundtrip:.0f} %).")
    else:
        r.add(ramp(roundtrip, c.wash_roundtrip_warn, c.wash_block_roundtrip, 30, 70),
              f"{100 * roundtrip:.0f} % des Volumens sind Hin-und-her-Trades derselben Wallets (Kauf und Verkauf): "
              f"künstliches Volumen.")
    r.add(ramp(top3, c.wash_top3_warn, 0.9, 15, 50),
          f"Nur 3 Wallets machen {100 * top3:.0f} % des Volumens der letzten {c.wash_window_min} Minuten.")
    if not r.reasons:
        r.positives.append(f"Volumen echt verteilt: {len(vol)} Wallets, {len(rows)} Trades.")
    return r.finish(cap=90)


def check_socials(ctx):
    r = CheckResult("socials", "Metadaten/Socials")
    info = (ctx.pair() or {}).get("info") or {}
    sites = info.get("websites") or []
    socials = info.get("socials") or []
    r.data.update(websites=len(sites), socials=[s.get("type") for s in socials])
    if not sites and not socials:
        r.add(15, "Keine Website und keine Socials hinterlegt.")
    elif not sites:
        r.add(5, "Keine Website hinterlegt.")
    else:
        r.positives.append(f"Website und {len(socials)} Social-Links hinterlegt.")
    meta = (ctx.rugcheck() or {}).get("tokenMeta") or {}
    if meta.get("mutable"):
        r.add(15, "Name, Symbol und Bild können vom Ersteller noch geändert werden.")
    return r.finish()


_COVERED = ("mint authority", "freeze authority", "holder", "liquidity", "lp ", "lp unlocked", "mutable",
            "creator", "insider", "transfer fee", "permanent delegate")


def check_rugcheck(ctx):
    r = CheckResult("rugcheck", "RugCheck (extern)")
    if not ctx.cfg.use_rugcheck:
        r.status = "skipped"
        return r.finish()
    rc = ctx.rugcheck()
    if not rc:
        r.status = "unknown"
        return r.finish()
    if rc.get("rugged"):
        r.hard = True
        r.add(95, "RugCheck meldet: Token wurde bereits gerugged.")
    for k in rc.get("risks") or []:
        name = str(k.get("name") or "")
        if any(x in name.lower() for x in _COVERED):
            continue  # our own checks measure this directly
        lvl = str(k.get("level") or "").lower()
        risk = 25 if lvl == "danger" else 8 if lvl == "warn" else 0
        r.add(risk, f"RugCheck: {name}" + (f" ({k['description']})" if k.get("description") else ""))
    r.data.update(score_normalised=rc.get("score_normalised"), n_risks=len(rc.get("risks") or []))
    return r.finish(cap=45)


def _intel_latest(ctx):
    try:
        import intel
        return intel.latest(ctx.conn, "coin", ctx.mint)
    except Exception:  # noqa: BLE001 - intel not installed or its tables missing
        return None


def _network_features(ctx):
    """Fresh network features straight from the DB (intel.network_features, no network, ~15 ms), for coins
    the intel service has not (yet) put into intel_latest."""
    try:
        import intel
        fn = getattr(intel, "network_features", None)
        if fn is not None:
            r = fn(ctx.conn, ctx.mint)
            return (r or {}).get("features") or {}
        from intel import network
        feats, _ = network.network_features(ctx.conn, ctx.mint, int(ctx.now))
        return feats
    except Exception:  # noqa: BLE001 - intel missing or its tables not there
        return {}


def check_history(ctx):
    """Track record of the people behind the coin, from the intel service's network features (no network):
    the dev's earlier launches, the funder network's launches and launch wallets seen in earlier rugs.
    Backup measurement (4,625 launches): a dev with an earlier rug rugged 2.2x as often, a launch with a
    wallet of an earlier rug 6.7x. Rates are shrunk toward 0 while the dev has few launches."""
    r = CheckResult("history", "Rug-Vorgeschichte")
    lt = _intel_latest(ctx)
    c = ctx.cfg
    f, src_ts = {}, None
    if lt and ctx.now - (lt.get("ts") or 0) <= c.history_fresh_sec:
        f, src_ts = lt.get("features") or {}, lt.get("ts")
    else:
        f = {k: v for k, v in _network_features(ctx).items() if v is not None}
        src_ts = int(ctx.now) if f else None
        if lt and ctx.now - (lt.get("ts") or 0) <= c.history_max_age_sec:
            f = {**{k: v for k, v in (lt.get("features") or {}).items() if k.startswith("net_")}, **f}
            src_ts = src_ts or lt.get("ts")
    dn, dr, d2 = f.get("net_dev_launches"), f.get("net_dev_rug_rate"), f.get("net_dev_hit2x_rate")
    fn, fr = f.get("net_funder_launches"), f.get("net_funder_rug_rate")
    bad, good, bots = f.get("net_early_bad"), f.get("net_early_good"), f.get("net_bot_share_60m")
    if all(v is None for v in (dn, fn, bad, bots)):
        r.status = "unknown"
        return r.finish()
    r.data.update(dev_launches=dn, dev_rug_rate=dr, dev_hit2x_rate=d2, funder_launches=fn, funder_rug_rate=fr,
                  early_bad=bad, early_good=good, bot_share_60m=bots, features_ts=src_ts)
    if dn and dr is not None:
        rugs = round(dr * dn)
        shrunk = rugs / (dn + 2)
        if rugs:
            r.add(ramp(shrunk, 0.1, 0.6, 20, 60),
                  f"Der Dev hat {rugs} von {dn:.0f} früheren Launches gerugged.")
        elif dn >= 3 and (d2 or 0) >= 0.3:
            r.positives.append(f"Dev ohne Rug in {dn:.0f} Launches, {100 * d2:.0f} % davon verdoppelten sich.")
    if fn and fr is not None and fn >= c.history_min_funder_launches:
        shrunk = fr * fn / (fn + 3)
        if shrunk >= 0.15:
            r.add(ramp(shrunk, 0.15, 0.6, 15, 55),
                  f"Aus demselben Geldgeber-Netz kamen {fn:.0f} Launches, {100 * fr:.0f} % davon Rugs.")
    if bad:
        r.add(ramp(bad, 1, 4, 30, 65),
              f"{bad:.0f} {'Wallet' if bad == 1 else 'Wallets'} aus früheren Rugs {'kaufte' if bad == 1 else 'kauften'} beim Launch.")
    elif good and good >= 2:
        r.positives.append(f"{good:.0f} Launch-Wallets aus früheren 2x-Launches, keine aus Rugs.")
    if bots is not None and bots >= c.history_bot_share_warn:
        r.add(ramp(bots, c.history_bot_share_warn, 0.8, 15, 40),
              f"{100 * bots:.0f} % des Volumens der letzten Stunde kommt von Sandwich- oder Roundtrip-Bots (unechtes Volumen).")
    return r.finish(cap=85)


def check_exit(ctx):
    """Soft rug in progress, from the intel service's latest coin features (no network): insiders selling,
    liquidity leaving, smart buyers getting out. Most rugs of young coins with burned LP look like this."""
    r = CheckResult("exit", "Ausstieg Insider/Liquidität")
    lt = _intel_latest(ctx)
    if not lt or ctx.now - (lt.get("ts") or 0) > ctx.cfg.exit_max_age_sec:
        r.status = "unknown"
        return r.finish()
    f = lt.get("features") or {}
    c = ctx.cfg
    liq = f.get("liq_usd") or 0.0
    chg = f.get("liq_chg_60m")
    sells = f.get("flow_insider_sell_usd_60m")
    smart_exit = f.get("flow_smart_exit_share")
    flash = f.get("liq_flash_60m")
    r.data.update(liq_chg_60m=chg, insider_sell_usd_60m=sells, smart_exit_share=smart_exit, liq_flash_60m=flash,
                  features_ts=lt.get("ts"))
    if chg is not None and -chg >= c.exit_liq_drop_warn:
        r.add(ramp(-chg, c.exit_liq_drop_warn, 0.8, 30, 80),
              f"Liquidität ist in der letzten Stunde um {100 * -chg:.0f} % gefallen: Geld verlässt den Pool.")
    if sells and liq:
        share = sells / liq
        r.add(ramp(share, c.exit_insider_sell_warn, 0.4, 30, 75),
              f"Dev, Bundle- und Insider-Wallets haben in 60 min {sells:,.0f} $ verkauft "
              f"({100 * share:.0f} % der Liquidität): sie steigen aus.".replace(",", "."))
    if smart_exit is not None and smart_exit >= 0.5:
        r.add(ramp(smart_exit, 0.5, 1.0, 15, 40),
              f"{100 * smart_exit:.0f} % der Smart-Käufer haben schon wieder verkauft.")
    if flash is not None and flash >= 0.5:
        r.add(40, f"Liquidität kam kurz (+{100 * flash:.0f} %) und ging sofort wieder: typisch für Köder-Liquidität.")
    if not r.reasons:
        r.positives.append("Kein Ausstieg von Insidern oder Liquidität in der letzten Stunde.")
    return r.finish(cap=85)


QUICK = [check_authorities, check_sell_sim, check_liquidity, check_lp, check_socials, check_wash,
         check_rugcheck, check_holders, check_exit, check_history]
DEEP = [check_dev, check_insiders, check_bundles]
