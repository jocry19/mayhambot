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
    monkeypatch.setattr(sources.http, "post_json", lambda url, payload, **kw: {"result": 1})
    f = sources.Fetcher(cfg, rpc_url=f"https://{hel}/?api-key=x", priority=False)
    try:
        got = []
        for _ in range(5):
            try:
                got.append(f.rpc("getTokenLargestAccounts", [MINT]))
            except BudgetExceeded:
                got.append(None)
        assert got == [1, 1, 1, 1, None]   # 2 per endpoint and minute: public twice, then Helius twice
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
