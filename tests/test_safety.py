"""safety/ on fixture data (no network): a clean coin, an insider/bundle rug and hard flags."""
import json
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
    assert a.coverage > 0.7  # exit, history and block0 need data the fixture does not have
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


def test_offline_never_touches_network():
    c = conn()
    a = safety.assess(MINT, c, cfg=CFG, force=True)  # pytest run -> offline, no fetcher given
    assert a.level == "unchecked" and not a.blocked and a.error == "offline"
    assert safety.get(c, MINT) is None and safety.refresh(c, extra=[MINT], cfg=CFG) == []


def test_liquidity_collapse_blocks():
    c = conn()
    c.execute("INSERT INTO token_prices VALUES (?,?,?,?,?)", (MINT, NOW - 3600, 1.0, 2_000_000, 700_000))
    c.commit()
    a = safety.assess(MINT, c, fetcher=FakeFetcher(clean()), cfg=CFG, force=True)  # live liquidity 60k = -91 %
    assert a.blocked and any("eingebrochen" in r["text"] for r in a.reasons)


def _uniq(prefix):
    import uuid
    return f"{prefix}-{uuid.uuid4().hex[:8]}.invalid"


def _cleanup(*hosts):
    from core.http import _LOCK_DIR
    for h in hosts:
        try:
            os.remove(os.path.join(_LOCK_DIR, f"safety_{h}.budget"))
        except OSError:
            pass


def test_budget_bucket_and_reserve():
    from safety import budget
    h = _uniq("bucket")
    try:
        assert [budget.take(h, 3) for _ in range(4)] == [True, True, True, False]
        h2 = _uniq("bucket")
        assert [budget.take(h2, 5, reserve=3) for _ in range(3)] == [True, True, False]  # refresher leaves 3
        assert budget.take(h2, 5)                                                        # a live path may use them
        _cleanup(h2)
    finally:
        _cleanup(h)


def test_fetcher_overflows_to_public_rpc_then_stops(monkeypatch):
    from safety import sources
    hel, pub = _uniq("helius"), _uniq("public")
    cfg = SafetySettings()
    cfg.helius_per_min, cfg.helius_spacing_sec, cfg.helius_reserve = 2, 0, 0
    cfg.public_per_min, cfg.public_spacing_sec, cfg.public_rpc_url = 1, 0, f"https://{pub}"
    seen = []
    monkeypatch.setattr(sources.http, "post_json", lambda url, payload, **kw: seen.append(url) or {"result": 1})
    f = sources.Fetcher(cfg, rpc_url=f"https://{hel}/?api-key=x", priority=False)
    try:
        assert [f.rpc("getSlot", []) for _ in range(3)] == [1, 1, 1]
        assert sum(pub in u for u in seen) == 1 and dict(f.rpc_calls) == {hel: 2, pub: 1}
        try:
            f.rpc("getSlot", [])
            raise AssertionError("budget not enforced")
        except BudgetExceeded:
            pass
    finally:
        _cleanup(hel, pub)


def test_quick_recheck_keeps_deep_findings_and_escalates():
    c = conn()
    deep = safety.assess(MINT, c, fetcher=FakeFetcher(ruggy()), cfg=CFG, force=True)
    f = FakeFetcher(ruggy())
    quick = safety.assess(MINT, c, deep=False, fetcher=f, cfg=CFG, force=True)
    assert quick.deep_ts == deep.deep_ts and f.calls < deep.calls
    assert quick.checks["bundles"]["data"].get("carried_from") == deep.ts
    assert any(r["check"] == "bundles" for r in quick.reasons) and quick.blocked
    s = ruggy()
    s["holders"] = [(POOL, 300_000_000)] + [(f"H{i}", 10_000_000) for i in range(15)]  # top 10 share fell a lot
    c.execute("UPDATE safety_cache SET ts = ts - 3600")  # holder snapshot expired
    c.commit()
    esc = safety.assess(MINT, c, deep=False, fetcher=FakeFetcher(s), cfg=CFG, force=True)
    assert "carried_from" not in esc.checks["insiders"]["data"]


def test_refresh_deep_only_for_hot_coins():
    c = conn()
    c.execute("INSERT INTO tokens(mint, tracked, last_updated) VALUES (?, 1, ?)", (MINT, NOW))
    c.commit()
    a = safety.refresh(c, cfg=CFG, fetcher_factory=lambda cfg: FakeFetcher(ruggy()))[0]
    assert "bundles" not in a.checks and not a.deep_ts
    a = safety.refresh(c, extra=[MINT], cfg=CFG, fetcher_factory=lambda cfg: FakeFetcher(ruggy()))[0]
    assert a.checks["bundles"]["status"] == "ok" and a.deep_ts


def test_launch_analysis_resumes_across_runs():
    c = conn()
    from safety import store
    store.ensure_schema(c)
    ctx = checks.Context(c, MINT, FakeFetcher(ruggy(), max_calls=3), CFG)  # 1 page + 2 of 4 launch txs
    part = ctx.launch()
    assert part["state"] == "txs" and part["idx"] == 2
    full = checks.Context(c, MINT, FakeFetcher(ruggy()), CFG).launch()
    assert full["state"] == "done" and full["creator"] == DEV and len(full["buyers"]) == 4


def test_exhausted_helius_credits_fall_back_to_public_or_skip(monkeypatch):
    from core import http
    from safety import sources
    hel, pub = _uniq("helius"), _uniq("public")
    cfg = SafetySettings()
    cfg.helius_spacing_sec = cfg.public_spacing_sec = 0
    cfg.public_rpc_url = f"https://{pub}"

    def post(url, payload, **kw):
        if hel in url:
            raise http.CreditsExhausted("helius credits used")
        return {"result": "public"}
    monkeypatch.setattr(sources.http, "post_json", post)
    try:
        live = sources.Fetcher(cfg, rpc_url=f"https://{hel}/?api-key=x", priority=True)
        assert live.rpc("getSlot", []) == "public"   # live reserve keeps working through the public RPC
        cfg.public_per_min = 0                         # no public overflow: the check is skipped, no traceback
        try:
            sources.Fetcher(cfg, rpc_url=f"https://{hel}/?api-key=x", priority=False).rpc("getSlot", [])
            raise AssertionError("expected BudgetExceeded")
        except BudgetExceeded:
            pass
    finally:
        _cleanup(hel, pub)


def test_holder_snapshot_cached_and_largest_accounts_capped(monkeypatch):
    c = conn()
    f1 = FakeFetcher(clean())
    safety.assess(MINT, c, deep=False, fetcher=f1, cfg=CFG, force=True)
    seen = []
    f2 = FakeFetcher(clean())
    orig = f2.rpc
    f2.rpc = lambda m, p: seen.append(m) or orig(m, p)
    safety.assess(MINT, c, deep=False, fetcher=f2, cfg=CFG, force=True)
    assert "getTokenLargestAccounts" not in seen and "getMultipleAccounts" not in seen
    from safety import sources
    hel, pub = _uniq("helius"), _uniq("public")
    cfg = SafetySettings()
    cfg.helius_spacing_sec = cfg.public_spacing_sec = cfg.largest_spacing_sec = 0
    cfg.largest_per_min, cfg.public_rpc_url = 2, f"https://{pub}"
    cfg.helius_per_min, cfg.helius_reserve = 3, 0
    monkeypatch.setattr(sources.http, "post_json", lambda url, payload, **kw: {"result": 1})
    f = sources.Fetcher(cfg, rpc_url=f"https://{hel}/?api-key=x", priority=False)
    try:
        got = []
        for _ in range(6):
            try:
                got.append(f.rpc("getTokenLargestAccounts", [MINT]))
            except BudgetExceeded:
                got.append(None)
        # Helius only by its normal budget (3), the public node by the method cap (2), then nothing
        assert got == [1, 1, 1, 1, 1, None]
        assert f.rpc_calls[hel] == 3 and f.rpc_calls[pub] == 2
    finally:
        _cleanup(hel, pub, f"{hel}_getTokenLargestAccounts", f"{pub}_getTokenLargestAccounts")


def test_learning_from_outcomes_reweights_checks():
    from safety import learn, store
    c = conn()
    store.ensure_schema(c)
    t0 = NOW - 3 * 86400
    cfg = SafetySettings()
    cfg.learn_min_flagged, cfg.learn_shrink_n = 5, 0
    rows, prices = [], []
    for i in range(40):
        m = f"M{i}"
        rug = i < 10                       # 10 of 40 rug
        risks = {"bundles": 60 if rug else 0,          # bundles warned exactly on the rugs
                 "socials": 15 if i % 2 == 0 else 30}  # socials warns on every other coin, no signal
        rows.append((m, t0, 50.0, "high" if rug else "low", json.dumps(risks)))
        for k in range(6):
            p = 1.0 if k == 0 else (0.02 if rug else 1.1)
            prices.append((m, t0 - 60 + k * 3600, p, 100_000, 50_000 if not rug or k == 0 else 1_000))
    c.executemany("INSERT INTO safety_history VALUES (?,?,?,?)", [r[:4] for r in rows])
    c.executemany("INSERT INTO safety_risks VALUES (?,?,?)", [(r[0], r[1], r[4]) for r in rows])
    c.executemany("INSERT INTO token_prices VALUES (?,?,?,?,?)", prices)
    c.commit()
    rep = learn.learn(c, cfg)
    assert rep["new_labels"] == 40 and abs(rep["base_rug_rate"] - 0.25) < 1e-9
    w = store.weights(c)
    assert w["bundles"] == cfg.learn_weight_max and 0.5 <= w["socials"] <= 1.0
    assert rep["levels"]["high"]["rug_rate"] == 1.0 and rep["levels"]["low"]["rug_rate"] == 0.0
    assert "gerugged" in learn.report_text(rep)
    assert learn.label_outcomes(c, cfg) == 0           # labelled once
    # the learned weight changes the score of a new coin with a bundle warning
    res = [checks.CheckResult("bundles", "b", reasons=[(40, "x")]).finish()]
    assert engine.combine(MINT, res, cfg, weights=w).score > engine.combine(MINT, res, cfg).score


def test_wash_like_sycamore_is_penalised_and_heavy_wash_blocks():
    for share, blocked in ((0.38, False), (0.6, True)):
        c = conn()
        trades, n = [], 40
        for i in range(n):  # wallet A trades back and forth (share of volume), the rest are one-way buyers
            if i < int(n * share):
                trades.append(Trade(f"a{i}", "A", MINT, "buy" if i % 2 else "sell", NOW - 60 * i, 1, 0.1, 100, 1, "t"))
            else:
                trades.append(Trade(f"b{i}", f"B{i}", MINT, "buy", NOW - 60 * i, 1, 0.1, 100, 1, "t"))
        db.insert_trades(c, trades)
        a = safety.assess(MINT, c, fetcher=FakeFetcher(clean()), cfg=CFG, force=True)
        assert a.blocked == blocked, (share, a.reasons)
        assert safety.signal_penalty(c, MINT, CFG)["factor"] < 0.8


def test_sweep_and_launch_evaluation():
    from safety import learn
    c = conn()
    c.execute("CREATE TABLE sniper_launches (mint TEXT, done INTEGER, safety_risk REAL, rugged INTEGER, max_ret_60m REAL)")
    rows = [(f"r{i}", 1, 80, 1, -0.95) for i in range(8)] + [(f"m{i}", 1, 30, 1, -0.9) for i in range(2)] \
        + [(f"d{i}", 1, 10, 0, 1.5) for i in range(9)] + [("dx", 1, 80, 0, 2.0)]
    c.executemany("INSERT INTO sniper_launches VALUES (?,?,?,?,?)", rows)
    c.commit()
    ev = learn.evaluate_launches(c, CFG)
    blk = ev["thresholds"]["gesperrt"]
    assert ev["n"] == 20 and blk["rugs_caught"] == 8 and blk["recall"] == 0.8
    assert blk["doublers_flagged"] == 1 and blk["false_alarm_rate"] == 0.1
    rep = learn.learn(c, CFG)
    assert "Sniper-Launches (20, davon 10 Rugs)" in learn.report_text(rep)


def test_exit_check_from_intel_features_and_young_coins_get_deep():
    import intel
    from intel import store as istore
    c = conn()
    istore.ensure_schema(c)
    feats = {"liq_usd": 20000, "liq_chg_60m": -0.6, "flow_insider_sell_usd_60m": 5000, "flow_smart_exit_share": 0.8}
    c.execute("INSERT OR REPLACE INTO intel_latest(kind, key, ts, coverage, features_json, meta_json) VALUES (?,?,?,?,?,?)",
              ("coin", MINT, NOW, 1.0, json.dumps(feats), "{}"))
    c.commit()
    assert intel.latest(c, "coin", MINT)
    a = safety.assess(MINT, c, deep=False, fetcher=FakeFetcher(clean()), cfg=CFG, force=True)
    text = " ".join(r["text"] for r in a.reasons)
    assert a.checks["exit"]["risk"] >= 60 and "steigen aus" in text and "gefallen" in text
    assert a.level in ("high", "critical")
    # young tracked coin without deep check: the refresher runs the deep checks once
    c2 = conn()
    c2.execute("INSERT INTO tokens(mint, tracked, last_updated, pair_created_at) VALUES (?, 1, ?, ?)", (MINT, NOW, NOW - 1800))
    c2.commit()
    a2 = safety.refresh(c2, cfg=CFG, fetcher_factory=lambda cfg: FakeFetcher(ruggy()))[0]
    assert a2.deep_ts and a2.checks["bundles"]["status"] == "ok"


def test_owner_without_sol_account_counts_as_wallet():
    s = clean()
    s["holders"] = [(POOL, 300_000_000), ("DRAINED", 200_000_000)] + [(f"H{i}", 5_000_000) for i in range(10)]
    f = FakeFetcher(s)
    orig = f.rpc

    def rpc(m, p):
        r = orig(m, p)
        if m == "getMultipleAccounts" and p[1].get("encoding") == "base64":
            r["value"] = [None if o == "DRAINED" else v for o, v in zip(p[0], r["value"])]
        return r
    f.rpc = rpc
    a = safety.assess(MINT, conn(), deep=False, fetcher=f, cfg=CFG, force=True)
    assert a.checks["holders"]["data"]["top1_pct"] == 20.0


class _DeadFetcher(FakeFetcher):
    """Every RPC and API call fails (endpoint down)."""

    def rpc(self, method, params):
        raise RuntimeError("rpc down")

    def jupiter_quote(self, *a, **k):
        raise RuntimeError("jupiter down")

    def dexscreener_pairs(self, *a, **k):
        raise RuntimeError("dexscreener down")

    def rugcheck(self, *a, **k):
        raise RuntimeError("rugcheck down")


def test_coin_the_checks_could_not_see_is_unchecked_not_safe():
    c = conn()
    a = safety.assess(MINT, c, fetcher=_DeadFetcher(clean()), cfg=CFG, force=True)
    assert a.level == "unchecked" and a.blocked and a.reasons[0]["check"] == "coverage"
    assert safety.is_blocked(c, MINT)
    p = safety.signal_penalty(c, MINT, CFG)
    assert p["checked"] is False and p["factor"] == CFG.unchecked_penalty_factor
    a.ts -= CFG.ttl_young_sec // 3 + 1
    assert engine.is_due(a, CFG)                       # retried soon, not after the 6 h blocked TTL
    # once the checks get through, the coin is judged on its findings
    ok = safety.assess(MINT, c, fetcher=FakeFetcher(clean()), cfg=CFG, force=True)
    assert ok.level == "low" and not ok.blocked and "coverage" not in ok.checks
    # an outage right after a good run keeps the fresh findings; stale ones no longer count
    a = safety.assess(MINT, c, fetcher=_DeadFetcher(clean()), cfg=CFG, force=True)
    assert a.level == "low" and not a.blocked and a.checks["sell_sim"]["data"]["carried_from"]
    c.execute("UPDATE safety_tokens SET checks_json=replace(checks_json, ?, ?)",
              (f'"carried_from":{a.checks["sell_sim"]["data"]["carried_from"]}', f'"carried_from":{NOW - 3 * 86400}'))
    c.commit()
    assert safety.assess(MINT, c, fetcher=_DeadFetcher(clean()), cfg=CFG, force=True).level == "unchecked"
    # manual allow still wins
    other = "MiNTyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy2"
    safety.allow(c, other)
    a = safety.assess(other, c, fetcher=_DeadFetcher(clean()), cfg=CFG, force=True)
    assert a.level == "unchecked" and not safety.is_blocked(c, other)


def test_launch_backtest_walk_forward():
    import random
    from safety import backtest, learn
    rnd = random.Random(7)
    c = conn()
    c.execute("CREATE TABLE sniper_launches (mint TEXT, decided_ts INTEGER, done INTEGER, rugged INTEGER, "
              "max_ret_60m REAL, risk REAL, bundle_pct REAL, top10_pct REAL, unseen_pct REAL, dev_buy_pct REAL)")
    rows = []
    for i in range(1200):
        bundle = rnd.choice([0, 0, 0, 0, 10, 60])
        unseen = rnd.choice([0, 0, 0, 20, 60])
        p = 0.02 + (0.7 if bundle >= 50 else 0) + (0.2 if unseen >= 50 else 0)
        rug = int(rnd.random() < min(p, 0.95))
        ret = -0.95 if rug else rnd.choice([0.1, 0.3, 1.5])
        rows.append((f"m{i}", NOW - 86400 + i * 60, 1, rug, ret, rnd.random() * 100, bundle, rnd.random() * 40,
                     unseen, rnd.random() * 10))
    c.executemany("INSERT INTO sniper_launches VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    c.commit()
    rep = backtest.run(c, CFG)
    assert rep["train"]["to"] < rep["test"]["from"]                       # judged only on newer launches
    learned, sniper = rep["scores"]["learned"], rep["scores"]["sniper"]
    assert learned["auc"] > 0.85 and learned["auc"] > sniper["auc"]       # random sniper risk ~ 0.5
    assert learned["brier"] < learned["brier_base"]
    top = learned["calibration"][-1]
    assert top["actual"] > 0.5                                           # high predicted risk really rugs
    assert backtest.rug_probability(c, {"bundle_pct": 70, "unseen_pct": 60}) > 0.7
    assert backtest.rug_probability(c, {"bundle_pct": 0, "unseen_pct": 0}) < 0.1
    assert "Rückrechnung auf 1200 Sniper-Launches" in backtest.report_text(rep)
    rep2 = learn.learn(c, CFG)
    assert rep2["launch_backtest"]["scores"]["learned"]["auc"] == learned["auc"]
    assert "Rückrechnung Sniper-Launches" in learn.report_text(rep2)


def test_history_check_uses_intel_network_features():
    from intel import store as istore
    c = conn()
    istore.ensure_schema(c)

    def put(feats):
        c.execute("INSERT OR REPLACE INTO intel_latest(kind, key, ts, coverage, features_json, meta_json) VALUES (?,?,?,?,?,?)",
                  ("coin", MINT, NOW, 1.0, json.dumps(feats), "{}"))
        c.commit()
        return safety.assess(MINT, c, deep=False, fetcher=FakeFetcher(clean()), cfg=CFG, force=True)

    a = put({"net_dev_launches": 4, "net_dev_rug_rate": 0.75, "net_early_bad": 3, "net_bot_share_60m": 0.6})
    h = a.checks["history"]
    text = " ".join(r["text"] for r in a.reasons)
    assert h["status"] == "ok" and h["risk"] >= 60 and "3 von 4 früheren Launches" in text and "früheren Rugs" in text
    assert a.level in ("high", "critical")
    one = put({"net_dev_launches": 1, "net_dev_rug_rate": 1.0}).checks["history"]["risk"]
    assert 20 <= one < 45                                   # one earlier rug of a new dev: a warning, no block
    g = put({"net_dev_launches": 5, "net_dev_rug_rate": 0.0, "net_dev_hit2x_rate": 0.4, "net_early_good": 3})
    assert g.checks["history"]["risk"] == 0 and any("2x" in p for p in g.positives)
    c.execute("DELETE FROM intel_latest")
    c.commit()
    assert safety.assess(MINT, c, deep=False, fetcher=FakeFetcher(clean()), cfg=CFG, force=True).checks["history"]["status"] == "unknown"


def test_audit_finds_buys_of_blocked_coins_and_results_per_level():
    from safety import audit, store
    c = conn()
    store.ensure_schema(c)
    c.execute("CREATE TABLE ct_positions (id INTEGER PRIMARY KEY, mint TEXT, opened_at INTEGER, status TEXT, "
              "pnl_pct REAL, safety_level TEXT)")
    c.executemany("INSERT INTO safety_history VALUES (?,?,?,?)",
                  [("BAD", NOW - 900, 95, "critical"), ("OK", NOW - 900, 5, "low")])
    c.executemany("INSERT INTO ct_positions(mint, opened_at, status, pnl_pct, safety_level) VALUES (?,?,?,?,?)",
                  [("BAD", NOW - 600, "closed", -0.8, None),      # level from safety_history: a leak
                   ("OK", NOW - 600, "closed", 0.3, None),
                   ("NEW", NOW - 600, "closed", -0.6, None),      # never checked: blind
                   ("OK", NOW - 600, "open", None, "low")])       # open positions are not judged yet
    c.commit()
    a = audit.audit(c)["copytrade"]
    assert a["n"] == 3 and a["leaks"] == 1 and a["leak_mints"] == ["BAD"] and a["blind"] == 1
    assert a["levels"]["critical"]["heavy_loss_share"] == 1.0 and a["levels"]["low"]["avg_pnl"] == 0.3
    assert "1 trotz Sperre gekauft" in audit.report_text(audit.audit(c))


def test_history_falls_back_to_network_features_for_fresh_launches():
    from sniper import store as sstore
    c = conn()
    sstore.ensure_schema(c)
    rows = [(f"OLD{i}", DEV, NOW - 6 * 3600 + i, 1, -0.95) for i in range(3)] + [(MINT, DEV, NOW - 60, None, None)]
    c.executemany("INSERT INTO sniper_launches(mint, creator, created_ts, rugged, max_ret_60m) VALUES (?,?,?,?,?)", rows)
    c.commit()                                    # nothing in intel_latest: the new launch is not in the universe
    h = safety.assess(MINT, c, deep=False, fetcher=FakeFetcher(clean()), cfg=CFG, force=True).checks["history"]
    assert h["status"] == "ok" and h["data"]["dev_launches"] == 3 and h["risk"] >= 50


class _JupiterDown(FakeFetcher):
    def jupiter_quote(self, *a, **k):
        self._spend()
        raise RuntimeError("429 for https://lite-api.jup.ag/swap/v1/quote")


def test_sell_sim_survives_jupiter_rate_limit_only_for_structurally_safe_tokens():
    c = conn()
    a = safety.assess(MINT, c, deep=False, fetcher=_JupiterDown(clean()), cfg=CFG, force=True)
    sim = a.checks["sell_sim"]
    assert sim["status"] == "ok" and sim["data"]["fallback"] == "structural" and a.level == "low"
    # Token-2022 (fees, hooks, pause possible) or a freeze authority: no answer -> unchecked, not safe
    s = clean()
    s["program"] = checks.TOKEN_2022
    a = safety.assess(MINT, conn(), deep=False, fetcher=_JupiterDown(s), cfg=CFG, force=True)
    assert a.checks["sell_sim"]["status"] == "error" and a.level == "unchecked" and a.blocked
    s = clean()
    s["dex"] = "someclmm"
    a = safety.assess(MINT, conn(), deep=False, fetcher=_JupiterDown(s), cfg=CFG, force=True)
    assert a.checks["sell_sim"]["status"] == "error" and a.blocked


def test_dexscreener_batch_serves_the_whole_pass(monkeypatch):
    from safety import sources
    calls = []

    def fake_get(url, **k):
        calls.append(url)
        return [{"baseToken": {"address": "A1"}, "dexId": "raydium", "liquidity": {"usd": 5}},
                {"baseToken": {"address": "B2"}, "dexId": "pumpswap", "liquidity": {"usd": 7}}]
    monkeypatch.setattr(sources.http, "get_json", fake_get)
    sources._PAIRS.clear()
    sources.prefetch_pairs([f"M{i}" for i in range(31)] + ["A1", "B2"])
    assert len(calls) == 2 and calls[0].count(",") == 29                  # 33 mints -> 2 requests
    f = sources.Fetcher(CFG, priority=False)
    assert f.dexscreener_pairs("B2")[0]["dexId"] == "pumpswap" and f.dexscreener_pairs("M3") == []
    assert f.calls == 0 and len(calls) == 2                                # answered from the batch
    sources._PAIRS.clear()


def test_unknown_checks_do_not_score_and_active_coins_are_prechecked():
    cfg = SafetySettings()
    unk = checks.CheckResult("liquidity", "l", status="unknown", reasons=[(15, "kein Paar auf DexScreener")]).finish()
    soc = checks.CheckResult("socials", "s", reasons=[(15, "keine Socials")]).finish()
    a = engine.combine(MINT, [unk, soc], cfg)
    assert a.score == 15.0 and a.level == "low"        # was 27.8 'erhöht' for every fresh launch
    c = conn()
    c.executemany("INSERT INTO trades(sig, wallet, mint, side, ts, usd_amount) VALUES (?,?,?,?,?,?)",
                  [(f"s{i}", f"w{i}", "HOTCOIN", "buy", NOW - 60, 100) for i in range(3)]
                  + [("x1", "w1", "LONELY", "buy", NOW - 60, 100), ("x2", "w9", "OLDCOIN", "buy", NOW - 7200, 100),
                     ("x3", "w8", "OLDCOIN", "buy", NOW - 7200, 100)])
    c.commit()
    assert engine.active_mints(c, cfg) == ["HOTCOIN"]
    assert "HOTCOIN" in engine.candidates(c, cfg=cfg)


def test_backtest_treats_blind_launches_as_unseen_and_calibration_stays_bounded():
    from safety import backtest
    blind = backtest._clean({"bundle_pct": 0, "n_buyers": 0, "fast_buyers": 0, "top10_pct": 3.0})
    seen = backtest._clean({"bundle_pct": 0, "n_buyers": 12, "fast_buyers": 0, "top10_pct": 3.0})
    assert blind["bundle_pct"] is None and blind["top10_pct"] == 3.0 and seen["bundle_pct"] == 0
    # perfectly separable hold-out: Newton must not run away
    a, b = backtest._platt([0.0] * 50 + [1.0] * 50, [0] * 50 + [1] * 50, -2.0)
    assert -6 <= a <= 6 and 0 <= b <= 3


def test_known_rug_wallets_warn_or_block_and_exchanges_never_count(tmp_path):
    from safety import store, watchlist
    cfg = SafetySettings()
    cfg.exchanges_file = CFG.exchanges_file
    cfg.bad_wallets_file = str(tmp_path / "bad.txt")
    (tmp_path / "bad.txt").write_text(f"H3  bundle  test\nH4  insider\nDEVw  launcher\nFUNDx  funder\n{BINANCE}  funder  falsch\n")
    assert BINANCE not in watchlist.load(None, cfg) and watchlist.load(None, cfg)["H3"]["role"] == "bundle"
    c = conn()
    # one listed holder: warning, no block
    s = clean()
    s["holders"] = [(POOL, 300_000_000), ("H3", 10_000_000)] + [(f"Z{i}", 10_000_000) for i in range(10)]
    a = safety.assess(MINT, c, deep=False, fetcher=FakeFetcher(s), cfg=cfg, force=True)
    w = a.checks["watchlist"]
    assert w["status"] == "ok" and not w["hard"] and 40 <= w["risk"] < 75 and not a.blocked
    # second listed wallet (funds a holder): block
    store.save_wallet_profile(c, {"address": "Z1", "n_txs": 5, "complete": 1, "first_ts": NOW, "last_ts": NOW,
                                  "funder": "FUNDx", "funded_sol": 1.0})
    a = safety.assess(MINT, c, deep=False, fetcher=FakeFetcher(s), cfg=cfg, force=True)
    assert a.checks["watchlist"]["hard"] and a.blocked
    # listed creator alone blocks; a runtime addition counts too
    c2 = conn()
    c2.execute("CREATE TABLE sniper_launch_wallets (mint TEXT, wallet TEXT, role TEXT)")
    c2.execute("INSERT INTO sniper_launch_wallets VALUES (?, 'NEWDEV', 'dev')", (MINT,))
    c2.commit()
    watchlist.add(c2, "NEWDEV", "launcher", "Serien-Launcher")
    a = safety.assess(MINT, c2, deep=False, fetcher=FakeFetcher(clean()), cfg=cfg, force=True)
    assert a.blocked and "Ersteller" in a.reasons[0]["text"] + " ".join(r["text"] for r in a.reasons)
    # the shipped list loads and never contains an exchange
    shipped = watchlist.load(None, CFG)
    assert len(shipped) >= 30 and BINANCE not in shipped


def test_block0_dust_buy_signature_warns_only_when_we_saw_the_first_moments():
    c = conn()
    c.execute("CREATE TABLE sniper_launches (mint TEXT, created_ts INTEGER)")
    c.execute("INSERT INTO sniper_launches VALUES (?, ?)", (MINT, NOW - 600))
    t0 = NOW - 590
    c.executemany("INSERT INTO trades(sig, wallet, mint, side, ts, sol_amount, usd_amount) VALUES (?,?,?,?,?,?,?)",
                  [(f"d{i}", f"DUST{i}", MINT, "buy", t0 + i, 0.005, 1.0) for i in range(4)]
                  + [("big", "WHALE", MINT, "buy", t0 + 1, 3.0, 600.0), ("late", "DUST9", MINT, "buy", t0 + 60, 0.005, 1.0)])
    c.commit()
    a = safety.assess(MINT, c, deep=False, fetcher=FakeFetcher(clean()), cfg=CFG, force=True)
    b0 = a.checks["block0"]
    assert b0["status"] == "ok" and b0["data"]["n_wallets"] == 4 and 40 <= b0["risk"] <= 60 and not a.blocked
    # a coin we only started watching long after its launch: no verdict
    c.execute("UPDATE sniper_launches SET created_ts=?", (NOW - 86400,))
    c.commit()
    assert safety.assess(MINT, c, deep=False, fetcher=FakeFetcher(clean()), cfg=CFG, force=True).checks["block0"]["status"] == "unknown"


def test_staged_pump_warns_on_fast_confirmed_mcap_and_fast_migration():
    c = conn()
    c.execute("CREATE TABLE sniper_launches (mint TEXT, created_ts INTEGER, migrated_ts INTEGER)")
    c.execute("INSERT INTO sniper_launches VALUES (?, ?, NULL)", (MINT, NOW - 3600))
    c.executemany("INSERT INTO token_prices(mint, ts, price_usd, mcap_usd, liquidity_usd) VALUES (?,?,?,?,?)",
                  [(MINT, NOW - 3600 + t, 0.0004, 400000, 52000) for t in (180, 400, 700)])
    c.commit()
    a = safety.assess(MINT, c, deep=False, fetcher=FakeFetcher(clean()), cfg=CFG, force=True)
    st = a.checks["staged"]
    assert st["status"] == "ok" and st["risk"] == 50 and st["data"]["fast_after_sec"] == 180 and not a.blocked
    assert st["data"]["pattern"] is True
    # same speed but deep liquidity (an old pair, a tokenised stock): not the pattern
    c.execute("UPDATE token_prices SET liquidity_usd=300000")
    c.commit()
    st = safety.assess(MINT, c, deep=False, fetcher=FakeFetcher(clean()), cfg=CFG, force=True).checks["staged"]
    assert st["risk"] == 0 and st["data"]["pattern"] is False
    c.execute("UPDATE token_prices SET liquidity_usd=52000")
    c.commit()
    # one unconfirmed snapshot only: weaker; a snapshot with too little liquidity does not count at all
    c.execute("DELETE FROM token_prices WHERE ts > ?", (NOW - 3600 + 180,))
    c.execute("INSERT INTO token_prices VALUES (?,?,?,?,?)", (MINT, NOW - 3600 + 500, 0.0004, 400000, 3000))
    c.commit()
    assert safety.assess(MINT, c, deep=False, fetcher=FakeFetcher(clean()), cfg=CFG, force=True).checks["staged"]["risk"] == 30
    # slow coin, but migrated 2 min after launch
    c.execute("DELETE FROM token_prices")
    c.execute("UPDATE sniper_launches SET migrated_ts=?", (NOW - 3600 + 120,))
    c.commit()
    st = safety.assess(MINT, c, deep=False, fetcher=FakeFetcher(clean()), cfg=CFG, force=True).checks["staged"]
    assert st["risk"] == 35 and st["data"]["migrated_after_sec"] == 120
    c.execute("UPDATE sniper_launches SET migrated_ts=NULL")
    c.commit()
    st = safety.assess(MINT, c, deep=False, fetcher=FakeFetcher(clean()), cfg=CFG, force=True).checks["staged"]
    assert st["risk"] == 0 and st["status"] == "ok"


def test_rug_snapshot_falls_back_to_the_pool_pick_not_mainnet_beta(monkeypatch):
    """Helius first; when it cannot take the call, the method's preferred public node (publicnode for
    getTokenLargestAccounts), not the fixed mainnet-beta that refuses it in long 429 runs."""
    from core import http
    from safety import sources
    picked = []
    monkeypatch.setattr(http, "pick_public_rpc",
                        lambda lane="bulk", probe=None, method=None: picked.append(method)
                        or "https://solana-rpc.publicnode.com")
    cfg = type("C", (), {"public_rpc_url": http.PUBLIC_RPC, "public_per_min": 30, "max_calls_per_token": 9})()
    f = sources.Fetcher(cfg, rpc_url="https://mainnet.helius-rpc.com/?api-key=x", priority=True)
    urls = [u for u, _ in f._targets("getTokenLargestAccounts")]
    assert urls[0].startswith("https://mainnet.helius-rpc.com") and urls[1] == "https://solana-rpc.publicnode.com"
    assert picked == ["getTokenLargestAccounts"]
    cfg.public_rpc_url = "https://my.own.node"          # an explicitly configured node is kept
    assert [u for u, _ in f._targets("getTokenLargestAccounts")][1] == "https://my.own.node"


def test_lp_zero_on_pumpswap_counts_only_unless_the_coin_graduated(monkeypatch):
    from types import SimpleNamespace
    c = conn()
    s = clean()
    s["dex"], s["rugcheck"] = "pumpswap", {"markets": [{"pubkey": POOL, "lp": {"lpLockedPct": 0}}], "risks": []}
    lp = safety.assess(MINT, c, deep=False, fetcher=FakeFetcher(s), cfg=CFG, force=True).checks["lp"]
    assert lp["risk"] >= 70          # a pool set up by hand: the LP can be pulled
    monkeypatch.setattr(checks, "_pump_migrated", lambda ctx: True)
    lp = safety.assess(MINT, c, deep=False, fetcher=FakeFetcher(s), cfg=CFG, force=True).checks["lp"]
    assert lp["risk"] == 0 and lp["data"]["pump_migrated"] and lp["status"] == "ok"
    monkeypatch.undo()
    c.execute("CREATE TABLE sniper_launches (mint TEXT, migrated INTEGER)")
    c.executemany("INSERT INTO sniper_launches VALUES (?, ?)", [("Abcpump", 1), ("Xyzpump", 0), ("Abc", 1)])
    ctx = lambda m: SimpleNamespace(mint=m, conn=c)  # noqa: E731
    assert checks._pump_migrated(ctx("Abcpump")) and not checks._pump_migrated(ctx("Xyzpump"))
    assert not checks._pump_migrated(ctx("Abc")) and not checks._pump_migrated(ctx("Newpump"))


def test_brand_scam_whole_curve_bought_at_create_is_vetoed_and_spread_holders_get_no_bonus():
    c = conn()
    c.execute("CREATE TABLE sniper_launches (mint TEXT, created_ts INTEGER, migrated INTEGER, migrated_ts INTEGER, "
              "dev_buy_pct REAL)")
    c.execute("INSERT INTO sniper_launches VALUES (?, ?, 1, ?, 79.31)", (MINT, NOW - 3600, NOW - 3599))
    c.commit()
    a = safety.assess(MINT, c, deep=False, fetcher=FakeFetcher(clean()), cfg=CFG, force=True)
    m = a.checks["masche"]
    assert m["hard"] and a.blocked and m["data"]["dev_buy_pct"] == 79.31 and m["data"]["migrated_after_sec"] == 1
    h = a.checks["holders"]
    assert h["data"]["spread_by_creator"] and h["risk"] >= 40
    assert not any("Breit verteilt" in p for p in a.positives)
    # an ordinary launch: no veto, the spread counts as a plus again
    c.execute("UPDATE sniper_launches SET dev_buy_pct=3.5, migrated=0, migrated_ts=NULL")
    c.commit()
    a = safety.assess(MINT, c, deep=False, fetcher=FakeFetcher(clean()), cfg=CFG, force=True)
    assert not a.checks["masche"]["hard"] and not a.checks["holders"]["data"].get("spread_by_creator")


def test_setup_wallet_of_the_brand_scam_blocks_alone(tmp_path):
    from safety import watchlist
    cfg = SafetySettings()
    cfg.exchanges_file = CFG.exchanges_file
    cfg.bad_wallets_file = str(tmp_path / "bad.txt")
    (tmp_path / "bad.txt").write_text("SETUP1  setup  160 SOL im Launch-Block\n")
    s = clean()
    s["holders"] = [(POOL, 300_000_000), ("SETUP1", 10_000_000)] + [(f"Z{i}", 10_000_000) for i in range(10)]
    a = safety.assess(MINT, conn(), deep=False, fetcher=FakeFetcher(s), cfg=cfg, force=True)
    assert a.checks["watchlist"]["hard"] and a.blocked
    shipped = watchlist.load(None, CFG)
    assert shipped["4G9JAzftaydKjB2558MLnkYw175KMN6NGGgyxkeRuwwA"]["role"] == "setup"
    assert "7FPqg7MpJwvGneBPvkBrCDjAvZhFnAvoiUw9LF91nsN" not in shipped   # the copycat is a victim, not a scammer


def test_helius_call_waits_out_the_spacing_instead_of_spilling_to_public(monkeypatch):
    from safety import sources
    hel, pub = _uniq("helius"), _uniq("public")
    cfg = SafetySettings()
    cfg.helius_spacing_sec, cfg.helius_overflow_wait_sec, cfg.helius_reserve = 0.3, 2.0, 0
    cfg.public_spacing_sec, cfg.public_rpc_url = 0, f"https://{pub}"
    monkeypatch.setattr(sources.http, "post_json", lambda url, payload, **kw: {"result": 1})
    f = sources.Fetcher(cfg, rpc_url=f"https://{hel}/?api-key=x", priority=False)
    try:
        for _ in range(3):
            f.rpc("getAccountInfo", [MINT])
        assert f.rpc_calls[hel] == 3 and not f.rpc_calls.get(pub)
    finally:
        _cleanup(hel, pub)
