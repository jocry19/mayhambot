"""safety/ on fixture data (no network): a clean coin, an insider/bundle rug and hard flags."""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import db  # noqa: E402
from core.models import SOL_MINT, Trade  # noqa: E402
import safety  # noqa: E402
from safety import checks, engine  # noqa: E402
from safety.config import SafetySettings  # noqa: E402
from safety.sources import BudgetExceeded  # noqa: E402

MINT = "MiNTxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx1"
POOL = "PooLxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx1"
DEV, W1, W2, W3, W4, OLD = "DEVw", "W1ww", "W2ww", "W3ww", "W4ww", "OLDw"
FUNDER = "FUNDERxxxxx"
BINANCE = "5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9"
NOW = int(time.time())
SUPPLY = 1_000_000_000


def tx(signer, sol=None, tokens=None, ts=NOW - 600):
    """sol: {key: delta SOL}, tokens: {owner: (pre, post)} of MINT."""
    sol = dict(sol or {})
    sol.setdefault(signer, 0.0)
    keys = [signer] + [k for k in sol if k != signer]
    pre = [10_000_000_000 for _ in keys]
    post = [int(p + sol[k] * 1e9) for p, k in zip(pre, keys)]
    tb = lambda which: [{"mint": MINT, "owner": o, "uiTokenAmount": {"uiAmountString": str(v[which])}}  # noqa: E731
                        for o, v in (tokens or {}).items()]
    return {"blockTime": ts, "transaction": {"message": {"accountKeys": [{"pubkey": k} for k in keys]}, "signatures": ["x"]},
            "meta": {"err": None, "fee": 5000, "preBalances": pre, "postBalances": post,
                     "preTokenBalances": tb(0), "postTokenBalances": tb(1)}}


class FakeFetcher:
    def __init__(self, scenario, max_calls=500):
        self.s, self.calls, self.max_calls = scenario, 0, max_calls

    def _spend(self):
        if self.calls >= self.max_calls:
            raise BudgetExceeded("x")
        self.calls += 1

    def rpc(self, method, params):
        self._spend()
        s = self.s
        if method == "getAccountInfo":
            return {"value": {"owner": s.get("program", "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"),
                              "data": {"parsed": {"info": s["mint_info"]}}}}
        if method == "getTokenLargestAccounts":
            return {"value": [{"address": "ta_" + o, "uiAmountString": str(a)} for o, a in s["holders"]]}
        if method == "getMultipleAccounts":
            if params[1].get("encoding") == "jsonParsed":
                return {"value": [{"data": {"parsed": {"info": {"owner": a[3:]}}}} for a in params[0]]}
            return {"value": [{"owner": s.get("programs", {}).get(o, checks.SYSTEM_PROGRAM)} for o in params[0]]}
        if method == "getSignaturesForAddress":
            addr = params[0]
            if addr == MINT:
                return s["mint_sigs"]
            return s["wallet_sigs"].get(addr, [{"signature": "old" + addr + str(i), "blockTime": NOW - 400 * 86400 + i}
                                                for i in range(40)])[: params[1]["limit"]]
        if method == "getTransaction":
            return s["txs"].get(params[0])
        if method == "getTokenAccountsByOwner":
            return {"value": []}
        raise AssertionError(method)

    def jupiter_quote(self, i, o, amount, slippage_bps=300):
        self._spend()
        if i == SOL_MINT:
            return {"outAmount": str(amount * 1000), "priceImpactPct": "0.005"}
        if self.s.get("honeypot"):
            return {"error": "no_route"}
        return {"outAmount": str(int(amount / 1000 * (1 - self.s.get("loss", 0.03)))), "priceImpactPct": "0.005"}

    def dexscreener_pairs(self, mint):
        self._spend()
        return [{"baseToken": {"address": MINT}, "pairAddress": POOL, "dexId": self.s.get("dex", "raydium"),
                 "liquidity": {"usd": 60000}, "marketCap": 400000, "pairCreatedAt": (NOW - 3600) * 1000,
                 "info": self.s.get("info", {"websites": [{"url": "x"}], "socials": [{"type": "twitter"}]})}]

    def rugcheck(self, mint):
        self._spend()
        return self.s.get("rugcheck")


def clean():
    return {"mint_info": {"mintAuthority": None, "freezeAuthority": None, "decimals": 6, "supply": str(SUPPLY * 10**6)},
            "holders": [(POOL, 300_000_000)] + [(f"H{i}", 10_000_000) for i in range(15)],
            "mint_sigs": [{"signature": f"s{i}", "slot": 1000 + i, "blockTime": NOW - 7200 + i} for i in range(30, -1, -1)],
            "txs": {"s0": tx(DEV, {DEV: -1.0}, {DEV: (0, 10_000_000)})},
            "wallet_sigs": {},
            "rugcheck": {"markets": [{"pubkey": POOL, "lp": {"lpLockedPct": 100}}], "risks": [], "topHolders": []}}


def ruggy():
    s = clean()
    s["holders"] = [(POOL, 300_000_000), (W1, 120_000_000), (W2, 90_000_000), (W3, 60_000_000), (DEV, 40_000_000),
                    (W4, 30_000_000)] + [(f"H{i}", 5_000_000) for i in range(10)]
    # creation slot 1000: dev creates + W1, W2 buy in the same block, W3 one slot later
    s["mint_sigs"] = [{"signature": f"s{i}", "slot": 1010 + i, "blockTime": NOW - 7000} for i in range(10, 0, -1)] + [
        {"signature": "b3", "slot": 1001, "blockTime": NOW - 7199}, {"signature": "b2", "slot": 1000, "blockTime": NOW - 7200},
        {"signature": "b1", "slot": 1000, "blockTime": NOW - 7200}, {"signature": "c0", "slot": 1000, "blockTime": NOW - 7200}]
    s["txs"] = {
        "c0": tx(DEV, {DEV: -2.0}, {DEV: (0, 80_000_000)}),
        "b1": tx(W1, {W1: -10.0}, {W1: (0, 120_000_000)}),
        "b2": tx(W2, {W2: -8.0}, {W2: (0, 90_000_000)}),
        "b3": tx(W3, {W3: -5.0}, {W3: (0, 60_000_000)}),
        "f1": tx(FUNDER, {FUNDER: -10.5, W1: 10.5}), "f2": tx(FUNDER, {FUNDER: -8.5, W2: 8.5}),
        "f3": tx(DEV, {DEV: -5.5, W3: 5.5}),
        "d1": tx(DEV, {DEV: -20.0, BINANCE: 20.0}), "d2": tx(DEV, {}, {DEV: (80_000_000, 40_000_000), W4: (0, 40_000_000)}),
    }
    fresh = lambda w, f: [{"signature": "x" + w, "blockTime": NOW - 7000}, {"signature": f, "blockTime": NOW - 8000}]  # noqa: E731
    s["wallet_sigs"] = {W1: fresh(W1, "f1"), W2: fresh(W2, "f2"), W3: fresh(W3, "f3"),
                        DEV: [{"signature": "d1"}, {"signature": "d2"}]}
    s["info"] = {}
    s["rugcheck"] = {"markets": [{"pubkey": POOL, "lp": {"lpLockedPct": 30}}], "tokenMeta": {"mutable": True},
                     "risks": [{"name": "Copycat token", "level": "warn"}], "topHolders": []}
    return s


def conn():
    return db.connect(os.path.join(tempfile.mkdtemp(), "t.db"))


CFG = SafetySettings()
CFG.exchanges_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "safety", "exchanges.txt")


def test_clean_coin_is_low_risk():
    c = conn()
    a = safety.assess(MINT, c, fetcher=FakeFetcher(clean()), cfg=CFG, force=True)
    assert a.level == "low" and not a.blocked, a.reasons
    assert a.coverage > 0.8
    assert any("Mint-Authority abgegeben" in p for p in a.positives)
    assert safety.signal_penalty(c, MINT, CFG)["factor"] == 1.0
    assert not safety.is_blocked(c, MINT)


def test_insider_bundle_rug_is_blocked_with_reasons():
    c = conn()
    a = safety.assess(MINT, c, fetcher=FakeFetcher(ruggy()), cfg=CFG, force=True)
    text = " ".join(r["text"] for r in a.reasons)
    assert a.blocked and a.score >= CFG.block_score, (a.score, text)
    assert "selben Block" in text                      # bundle in the creation slot
    assert "derselben Wallet" in text                  # W1+W2 share a funder
    assert "Ersteller finanziert" in text or "vom Ersteller" in text  # W3 funded by dev
    assert "Binance" in text                           # dev cash-out
    assert "verteilt" in text                          # dev spread tokens to W4
    assert "gesperrt" in text                          # only 30 % LP locked
    assert a.checks["bundles"]["data"]["n_same_block"] == 2
    assert MINT in safety.blocked_mints(c)
    assert safety.signal_penalty(c, MINT, CFG)["factor"] == 0.0
    # launch and wallet profiles are cached: a re-check needs far fewer calls
    f2 = FakeFetcher(ruggy())
    safety.assess(MINT, c, fetcher=f2, cfg=CFG, force=True)
    assert f2.calls < a.calls


def test_hard_flags_block():
    for mod in ({"mint_info": {"freezeAuthority": "X"}}, {"honeypot": True}, {"loss": 0.4}):
        s = clean()
        for k, v in mod.items():
            s[k] = {**s[k], **v} if isinstance(v, dict) else v
        a = safety.assess(MINT, conn(), fetcher=FakeFetcher(s), cfg=CFG, force=True)
        assert a.blocked and a.score >= CFG.hard_flag_floor and a.hard_flags, (mod, a.reasons)


def test_token2022_transfer_fee():
    s = clean()
    s["program"] = checks.TOKEN_2022
    s["mint_info"]["extensions"] = [{"extension": "transferFeeConfig",
                                     "state": {"newerTransferFee": {"transferFeeBasisPoints": 2000}}}]
    a = safety.assess(MINT, conn(), fetcher=FakeFetcher(s), cfg=CFG, force=True)
    assert a.blocked and any("Transfer-Gebühr" in r["text"] for r in a.reasons)


def test_budget_marks_checks_skipped_and_overrides():
    c = conn()
    a = safety.assess(MINT, c, fetcher=FakeFetcher(ruggy(), max_calls=4), cfg=CFG, force=True)
    assert any(ch["status"] == "skipped" for ch in a.checks.values())
    a.ts -= CFG.ttl_young_sec // 3 + 1
    assert engine.is_due(a, CFG)  # incomplete results are re-checked after a third of the TTL
    safety.allow(c, MINT)
    assert not safety.is_blocked(c, MINT)
    safety.block(c, MINT, "test")
    assert safety.is_blocked(c, MINT) and safety.signal_penalty(c, MINT, CFG)["factor"] == 0.0
    safety.clear_override(c, MINT)


def test_wash_trading_from_pool_trades():
    c = conn()
    trades = []
    for i in range(30):
        w = ["A", "B"][i % 2]
        trades.append(Trade(f"w{i}", w, MINT, "buy" if i % 4 < 2 else "sell", NOW - 60 * i, 1, 0.1, 500, 1, "test"))
    db.insert_trades(c, trades)
    a = safety.assess(MINT, c, fetcher=FakeFetcher(clean()), cfg=CFG, force=True)
    assert a.checks["wash"]["risk"] >= 40 and "Hin-und-her" in " ".join(r["text"] for r in a.reasons)


def test_refresh_and_cache():
    c = conn()
    db.upsert_tokens(c, [])
    c.execute("INSERT INTO tokens(mint, tracked, last_updated) VALUES (?, 1, ?)", (MINT, NOW))
    c.commit()
    assert safety.candidates(c, cfg=CFG) == [MINT]
    done = safety.refresh(c, cfg=CFG, fetcher_factory=lambda cfg: FakeFetcher(clean()))
    assert len(done) == 1 and safety.candidates(c, cfg=CFG) == []
    assert safety.assess(MINT, c, cfg=CFG).ts == done[0].ts  # fresh result comes from the table, no network
    assert safety.signal_penalty(c, "unknown", CFG)["checked"] is False


def test_penalty_curve():
    cfg = SafetySettings()
    res = [checks.CheckResult("x", "x", reasons=[(cfg.caution_score + (cfg.block_score - cfg.caution_score) / 2, "mittel")])]
    for r in res:
        r.finish()
    c = conn()
    from safety import store
    store.ensure_schema(c)
    store.save_assessment(c, engine.combine(MINT, res, cfg))
    f = safety.signal_penalty(c, MINT, cfg)["factor"]
    assert abs(f - (1 - (1 - cfg.min_penalty_factor) / 2)) < 0.01


def test_open_copytrade_positions_are_watched():
    c = conn()
    c.execute("CREATE TABLE ct_positions (id INTEGER PRIMARY KEY, mint TEXT, status TEXT)")
    c.execute("INSERT INTO ct_positions(mint, status) VALUES (?, 'open'), ('CLOSEDxx', 'closed')", (MINT,))
    c.commit()
    assert safety.candidates(c, cfg=CFG) == [MINT]
    a = safety.assess(MINT, c, fetcher=FakeFetcher(clean()), cfg=CFG, force=True)
    assert safety.candidates(c, cfg=CFG) == []
    c.execute("UPDATE safety_tokens SET ts=? WHERE mint=?", (a.ts - CFG.ttl_young_sec, MINT))
    c.execute("UPDATE safety_tokens SET pair_created_at=0")  # old coin: normal TTL would be 2 h
    c.commit()
    assert safety.candidates(c, cfg=CFG) == [MINT]
