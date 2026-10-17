from core import http


def test_penalize_stretches_and_relax_shrinks(tmp_path, monkeypatch):
    monkeypatch.setattr(http, "_LOCK_DIR", str(tmp_path))
    host = "api.geckoterminal.com"
    assert http.slowdown(host) == 1.0
    http.penalize(host, 0)
    assert http.slowdown(host) == http.SLOWDOWN_ON_429
    for _ in range(3):
        http.penalize(host, 0)
    assert http.slowdown(host) == http.SLOWDOWN_MAX
    before = http.slowdown(host)
    http.relax(host)
    assert 1.0 < http.slowdown(host) < before


def test_old_lock_file_format_still_reads(tmp_path, monkeypatch):
    monkeypatch.setattr(http, "_LOCK_DIR", str(tmp_path))
    (tmp_path / "api.helius.xyz.lock").write_text("123.0")
    assert http.slowdown("api.helius.xyz") == 1.0
    http.throttle("api.helius.xyz", interval=0)


def test_endpoint_hides_keys_and_groups_addresses():
    url = "https://beta.helius-rpc.com/?api-key=SECRET"
    assert "SECRET" not in http._endpoint(url, {"json": {"method": "getTransaction"}})
    assert http._endpoint(url, {"json": {"method": "getTransaction"}}).endswith("[getTransaction]")
    gt = "https://api.geckoterminal.com/api/v2/networks/solana/pools/7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU/trades"
    assert http._endpoint(gt, {}) == "api.geckoterminal.com/api/v2/networks/solana/pools/<addr>/trades"


def test_redact_masks_telegram_bot_token_everywhere():
    import logging
    tok = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"
    msg = f"POST api.telegram.org failed (HTTPSConnectionPool url: /bot{tok}/sendMessage)"
    assert tok not in http.redact(msg) and "/bot***/sendMessage" in http.redact(msg)
    rec = logging.getLogRecordFactory()("x", logging.WARNING, __file__, 1, "%s", (msg,), None)
    assert tok not in rec.getMessage()
    assert "secret" not in http.redact("https://x/?api-key=secret&token=secret")


def test_tracebacks_are_redacted():
    import logging
    import sys
    tok = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"
    try:
        raise RuntimeError(f"url: /bot{tok}/sendMessage")
    except RuntimeError:
        rec = logging.getLogRecordFactory()("x", logging.ERROR, __file__, 1, "cycle failed", None, sys.exc_info())
    assert tok not in logging.Formatter().format(rec)


def _fresh(monkeypatch, tmp_path, service=""):
    monkeypatch.setattr(http, "_LOCK_DIR", str(tmp_path))
    for name in ("_budgets", "_recent", "_usage", "_usage_window"):
        monkeypatch.setattr(http, name, {})
    monkeypatch.setattr(http, "_service", service)


def test_no_service_means_no_budget(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    assert http.budget("beta.helius-rpc.com") is None


def test_default_and_env_budget(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path, "safety")
    assert http.budget("api.helius.xyz") == http.DEFAULT_BUDGETS["helius"]["safety"]
    monkeypatch.setenv("HELIUS_BUDGET_SAFETY", "33")
    assert http.budget("mainnet.helius-rpc.com") == 33
    http.set_budget("helius", 7)
    assert http.budget("beta.helius-rpc.com") == 7
    assert sum(http.DEFAULT_BUDGETS["helius"].values()) < 600  # free plan: 10 req/s


def test_budget_waits_instead_of_calling(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path, "signals")
    clock = {"t": 1000.0}
    slept = []
    monkeypatch.setattr(http.time, "time", lambda: clock["t"])

    def fake_sleep(s):
        slept.append(s)
        clock["t"] += s
    monkeypatch.setattr(http.time, "sleep", fake_sleep)
    http.set_budget("helius", 3)
    for _ in range(4):
        http._take_budget("beta.helius-rpc.com")
    assert len(slept) == 1 and abs(slept[0] - 60) < 1e-6
    u = http.usage()["helius"]
    assert u["calls"] == 4 and u["budget_per_min"] == 3 and u["waited_s"] >= 59


def test_usage_counts_429_and_is_shared(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path, "research")
    http._take_budget("api.geckoterminal.com")
    http.penalize("api.geckoterminal.com", 0)
    http._write_usage_file()
    allu = http.usage(all_services=True)
    assert allu["research"]["groups"]["geckoterminal"] == {**allu["research"]["groups"]["geckoterminal"],
                                                            "calls": 1, "429": 1}


def test_credit_costs():
    rpc = "https://beta.helius-rpc.com/?api-key=x"
    assert http.credit_cost(rpc, {"json": {"method": "getTransaction"}}) == 1
    assert http.credit_cost(rpc, {"json": {"method": "getAsset"}}) == 10
    assert http.credit_cost(rpc, {"json": [{"method": "getAsset"}, {"method": "getSlot"}]}) == 11
    assert http.credit_cost("https://api.helius.xyz/v0/addresses/A/transactions", {}) == 100
    assert http.credit_cost("https://api.geckoterminal.com/api/v2/x", {}) == 0


def _credit_env(monkeypatch, tmp_path, service, day=16, month_credits=31_000):
    import calendar
    _fresh(monkeypatch, tmp_path, service)
    ts = calendar.timegm((2026, 10, day, 12, 0, 0))
    monkeypatch.setattr(http.time, "time", lambda: ts)
    monkeypatch.setenv("HELIUS_CREDITS_MONTH", str(month_credits))
    monkeypatch.setenv("HELIUS_CREDITS_RESERVE_PCT", "0")
    monkeypatch.setenv("HELIUS_SHARE_SIGNALS", "15")
    monkeypatch.setenv("HELIUS_SHARE_COPYTRADE", "15")
    monkeypatch.setenv("HELIUS_BORROW_PCT", "0")  # own share only; lending is tested on its own
    return ts


def test_daily_share_spreads_over_rest_of_month(monkeypatch, tmp_path):
    _credit_env(monkeypatch, tmp_path, "signals", day=1)  # 31 days, signals 15% of 31,000 = 4,650 -> 150/day
    st = http.credits("signals")
    assert st["credits_cap"] == 4650 and abs(st["credits_day_budget"] - 150) < 1e-6
    for _ in range(150):
        http._charge(1)
    try:
        http._charge(1)
    except http.CreditsExhausted:
        pass
    else:
        raise AssertionError("expected CreditsExhausted")
    assert http.credits("signals")["credits_today"] == 150
    assert http.usage()["helius"]["refused"] == 1


def _lend_env(monkeypatch, tmp_path, service):
    """31,000 a month, no reserve, day 1 of 31: research 50 % = 500/day, signals 10 % = 100/day."""
    _credit_env(monkeypatch, tmp_path, service, day=1)
    monkeypatch.setenv("HELIUS_BORROW_PCT", "50")
    for svc, pct in (("research", 50), ("signals", 10), ("safety", 0), ("copytrade", 0), ("sniper", 0), ("brain", 0)):
        monkeypatch.setenv(f"HELIUS_SHARE_{svc.upper()}", str(pct))


def test_used_up_share_borrows_half_of_what_others_leave(monkeypatch, tmp_path):
    _lend_env(monkeypatch, tmp_path, "signals")
    assert http.credits_left_today() == 250  # own 100 vs. half of research's unused 500
    for _ in range(350):  # own 100 + 250 lent
        http._charge(1)
    try:
        http._charge(1)
    except http.CreditsExhausted:
        pass
    else:
        raise AssertionError("lending is capped at half of the others' unused day budget")
    assert http.credits("signals")["credits_today"] == 350 and http.credits_left_today() == 0


def test_lender_keeps_its_own_share_and_lending_shrinks(monkeypatch, tmp_path):
    _lend_env(monkeypatch, tmp_path, "research")
    for _ in range(400):  # research spends most of its own day
        http._charge(1)
    monkeypatch.setattr(http, "_service", "signals")
    assert http.credits_left_today() == 100  # own share; only 50 of research's 100 unused may be lent
    for _ in range(150):
        http._charge(1)
    try:
        http._charge(1)
    except http.CreditsExhausted:
        pass
    else:
        raise AssertionError("expected CreditsExhausted")
    monkeypatch.setattr(http, "_service", "research")
    for _ in range(100):  # the lender still gets the rest of its own share
        http._charge(1)


def test_no_lending_when_plan_is_ahead_of_the_calendar(monkeypatch, tmp_path):
    _lend_env(monkeypatch, tmp_path, "research")
    monkeypatch.setenv("HELIUS_CREDITS_MONTH", "3100")  # pace on day 1 of 31: 100 credits for the whole plan
    monkeypatch.setenv("HELIUS_SHARE_RESEARCH", "300")  # 300/day
    monkeypatch.setenv("HELIUS_SHARE_SIGNALS", "0")
    for _ in range(98):
        http._charge(1)
    monkeypatch.setattr(http, "_service", "signals")
    assert http.credits_left_today() == 2  # research leaves 202 unused (101 lendable), but the plan is at 98/100


def test_unused_credits_carry_over(monkeypatch, tmp_path):
    _credit_env(monkeypatch, tmp_path, "signals", day=16)  # nothing used in 15 days: 4,650 over 16 days
    assert abs(http.credits("signals")["credits_day_budget"] - 4650 / 16) < 1e-6


def test_public_rpc_fallback_when_used_up(monkeypatch, tmp_path):
    _credit_env(monkeypatch, tmp_path, "copytrade", day=31, month_credits=100)  # 15 credits, all today
    for _ in range(15):
        http._charge(1)
    calls = []

    class R:
        status_code = 200
        headers = {}

        def json(self):
            return {"result": 1}
    monkeypatch.setattr(http._session, "request", lambda m, url, **kw: calls.append(url) or R())
    monkeypatch.setattr(http.time, "sleep", lambda s: None)
    http.post_json("https://beta.helius-rpc.com/?api-key=k", {"method": "getSlot"})
    assert len(calls) == 1 and calls[0] in http.public_rpc_urls()
    assert http.rpc_url("getSlot", "https://beta.helius-rpc.com/?api-key=k") in http.public_rpc_urls()
    try:
        http.post_json("https://beta.helius-rpc.com/?api-key=k", {"method": "getAsset"})
    except http.CreditsExhausted:
        pass
    else:
        raise AssertionError("DAS has no public fallback")


def test_unnamed_process_is_counted_not_limited(monkeypatch, tmp_path):
    _credit_env(monkeypatch, tmp_path, "", month_credits=10)
    for _ in range(50):
        http._charge(1)
    assert http.credits("other")["credits_month"] == 50


def test_default_shares_fit_the_month():
    assert sum(http.DEFAULT_CREDIT_SHARES.values()) == 100 and http.DEFAULT_CREDIT_SHARES["research"] == 45


def test_brain_has_its_own_helius_share(monkeypatch, tmp_path):
    """The rug check of every brain paper buy goes to Helius first instead of only borrowing (then mainnet-beta)."""
    _credit_env(monkeypatch, tmp_path, "brain")
    st = http.credits("brain")
    assert st["credits_day_budget"] and st["credits_day_budget"] > 0
    assert http.credits_left_today("brain") > 0


def test_das_and_enhanced_share_one_fixed_spacing(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    seen = []
    monkeypatch.setattr(http, "_shared_spacing", lambda host, interval=None: seen.append(host))
    monkeypatch.setattr(http, "_charge", lambda cost: None)
    http.throttle("api.helius.xyz")
    http.throttle("beta.helius-rpc.com", das=True)
    http.throttle("beta.helius-rpc.com")
    assert seen == [http.DAS_ENHANCED_SPACING, "api.helius.xyz", http.DAS_ENHANCED_SPACING, "beta.helius-rpc.com",
                    "beta.helius-rpc.com"]
    assert http.HOST_INTERVAL[http.DAS_ENHANCED_SPACING] >= 0.5


def _clock(monkeypatch, start=1000.0):
    clock = {"t": start}
    monkeypatch.setattr(http.time, "time", lambda: clock["t"])

    def sleep(s):
        clock["t"] += s
    monkeypatch.setattr(http.time, "sleep", sleep)
    monkeypatch.setattr(http.os.path, "getmtime", lambda p: http._marker_times.get(p, 0))
    monkeypatch.setattr(http.os, "utime", lambda p, t: http._marker_times.__setitem__(p, clock["t"]))
    return clock


http._marker_times = {}
PUB = "api.mainnet-beta.solana.com"


def test_bulk_leaves_room_for_live(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    http._marker_times.clear()
    clock = _clock(monkeypatch)
    monkeypatch.setenv("PUBLIC_RPC_LIVE_SHARE", "0.4")
    lim = http.WINDOW_LIMITS[PUB]
    for i in range(int(lim["total"] * 0.6)):  # bulk may use 60 % of the window
        http._window_wait(PUB, f"m{i % 9}", "bulk")
    t = clock["t"]
    http._window_wait(PUB, "getTransaction", "live")  # live passes at once
    assert clock["t"] == t
    http._window_wait(PUB, "m1", "bulk")  # bulk must wait for the window (and the live marker)
    assert clock["t"] > t


def test_per_method_cap(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    http._marker_times.clear()
    clock = _clock(monkeypatch)
    for _ in range(http.WINDOW_LIMITS[PUB]["per_method"]):
        http._window_wait(PUB, "getTransaction", "live")
    t = clock["t"]
    http._window_wait(PUB, "getAccountInfo", "live")  # another method is not starved
    assert clock["t"] == t
    http._window_wait(PUB, "getTransaction", "live")
    assert clock["t"] >= t + 9.9


def test_usage_splits_lanes(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    http._marker_times.clear()
    _clock(monkeypatch)
    http.throttle(PUB, priority="live", method="getTransaction")
    http.throttle(PUB, method="getSignaturesForAddress")
    u = http.usage()[PUB]
    assert u["live_calls"] == 1 and u["bulk_calls"] == 1 and "live_waited_s" in u


def test_new_helius_key_resets_ledger_and_keeps_history(monkeypatch, tmp_path):
    import json
    _credit_env(monkeypatch, tmp_path, "research", day=5, month_credits=1_000_000)
    monkeypatch.setenv("HELIUS_API_KEY", "old-key-123")
    for _ in range(3):
        http._charge(100)
    assert http.credits("research")["credits_month"] == 300
    monkeypatch.setenv("HELIUS_API_KEY", "new-key-456")
    st = http.credits_status()  # read-only view already shows the fresh quota
    assert st["used"] == 0 and st["key_changed_at"] and st["history"][-1]["used"] == 300
    http._charge(1)
    raw = (tmp_path / "credits-helius.json").read_text()
    assert "old-key-123" not in raw and "new-key-456" not in raw
    led = json.loads(raw)
    assert led["history"][-1]["reason"] == "key" and led["services"]["research"]["month"] == 1
    assert http.credits("research")["key_changed_at"] == led["key_changed_at"]


def test_warn_level(monkeypatch, tmp_path):
    _credit_env(monkeypatch, tmp_path, "", month_credits=1000)
    for _ in range(8):
        http._charge(100)
    st = http.credits_status()
    assert st["warn_level"] == 0.8 and st["warn"]
    assert http.credits("research")["warn"]


def test_stale_process_with_old_key_does_not_flip_back(monkeypatch, tmp_path):
    _credit_env(monkeypatch, tmp_path, "research", day=5, month_credits=1_000_000)
    monkeypatch.setenv("HELIUS_API_KEY", "old")
    http._charge(10)
    monkeypatch.setenv("HELIUS_API_KEY", "new")
    http._charge(5)
    monkeypatch.setenv("HELIUS_API_KEY", "old")  # another service not restarted yet
    http._charge(1)
    st = http.credits_status()
    assert st["used"] == 6 and len(st["history"]) == 1


def test_pool_probe_drops_dead_endpoints(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    monkeypatch.setenv("PUBLIC_RPC_URLS", "https://a.example,https://b.example,https://c.example")
    probe = lambda u: (u != "https://b.example", 50.0 if u.endswith("a.example") else 20.0)  # noqa: E731
    assert http._healthy(probe=probe) == ["https://a.example", "https://c.example"]
    assert http.pool_status()["b.example"]["disabled_until"] > http.time.time()
    # live takes the fastest, bulk the emptiest window
    assert http.pick_public_rpc("live", probe=probe) == "https://c.example"
    for _ in range(5):
        http._window_wait("c.example", "getTransaction", "bulk")
    assert http.pick_public_rpc("bulk", probe=probe) == "https://a.example"


def test_pool_failover_and_latency(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    monkeypatch.setenv("PUBLIC_RPC_URLS", "https://a.example,https://c.example")
    monkeypatch.setattr(http.time, "sleep", lambda s: None)
    calls = []

    class R:
        def __init__(self, code):
            self.status_code, self.headers, self.url = code, {}, "https://x"

        def json(self):
            return {"result": "ok"}

    def req(method, url, **kw):
        calls.append(url)
        return R(503 if "a.example" in url else 200)
    monkeypatch.setattr(http._session, "request", req)
    monkeypatch.setattr(http, "_latency", {"a.example": 1.0, "c.example": 99.0})
    for _ in range(3):
        assert http.post_json("https://a.example", {"method": "getSlot"}, priority="live") == {"result": "ok"}
    assert http.pool_status()["a.example"].get("disabled_until", 0) > http.time.time()  # 3 failures -> out
    assert calls[-1] == "https://c.example"
    assert http.usage()["c.example"]["latency_ms"] is not None


def test_429_backs_off_but_never_drops_endpoint(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    monkeypatch.setenv("PUBLIC_RPC_URLS", "https://a.example,https://c.example")
    monkeypatch.setattr(http, "_halved_cache", {})
    for _ in range(10):
        http._pool_result("a.example", False, status=429)
    st = http.pool_status()["a.example"]
    assert st.get("disabled_until", 0) == 0 and st["halved_until"] > http.time.time()
    assert http._halved("a.example")


def test_last_healthy_endpoint_is_kept(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    monkeypatch.setenv("PUBLIC_RPC_URLS", "https://a.example,https://c.example")
    for _ in range(3):
        http._pool_result("a.example", False, status=503)
    assert http.pool_status()["a.example"]["disabled_until"] > http.time.time()
    for _ in range(3):
        http._pool_result("c.example", False, status=503)
    assert http.pool_status()["c.example"].get("disabled_until", 0) == 0


def test_method_limit_and_preference(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    http._marker_times.clear()
    clock = _clock(monkeypatch)
    monkeypatch.setattr(http, "_halved_cache", {})
    for _ in range(6):
        http._window_wait(PUB, "getTokenLargestAccounts", "live")
    t = clock["t"]
    http._window_wait(PUB, "getTokenLargestAccounts", "live")
    assert clock["t"] > t  # 6 per 10 s on mainnet-beta
    monkeypatch.setenv("PUBLIC_RPC_URLS", "https://api.mainnet-beta.solana.com,https://solana-rpc.publicnode.com")
    ok = lambda u: (True, 10.0)  # noqa: E731
    assert http.pick_public_rpc("bulk", probe=ok, method="getTokenLargestAccounts") == \
        "https://solana-rpc.publicnode.com"


def test_retry_after_429_books_credits_once(monkeypatch, tmp_path):
    _credit_env(monkeypatch, tmp_path, "research")
    statuses = [429, 429, 200]

    class R:
        def __init__(self, code):
            self.status_code, self.url = code, "https://api.helius.xyz/v0/addresses/x/transactions?api-key=k"
            self.headers = {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"} if code == 429 else {}

        def json(self):
            return []
    monkeypatch.setattr(http._session, "request", lambda m, url, **kw: R(statuses.pop(0)))
    monkeypatch.setattr(http.time, "sleep", lambda s: None)
    url = "https://api.helius.xyz/v0/addresses/x/transactions?api-key=k"
    before = http.credits("research")["credits_month"]
    http.get_json(url)
    assert http.credits("research")["credits_month"] - before == http.credit_cost(url, {})


def test_endpoint_refusing_one_method_is_avoided_for_it(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    monkeypatch.setenv("PUBLIC_RPC_URLS", "https://a.example,https://c.example")
    monkeypatch.setattr(http.time, "sleep", lambda s: None)
    monkeypatch.setattr(http, "_method_429", {})
    monkeypatch.setattr(http, "_method_stats", {})
    monkeypatch.setattr(http, "_latency", {"a.example": 1.0, "c.example": 99.0})  # a is the fastest
    calls = []

    class R:
        def __init__(self, code):
            self.status_code, self.headers, self.url = code, {}, "https://x"

        def json(self):
            return {"result": "ok"}

    def req(method, url, json=None, **kw):
        calls.append((url, json["method"]))
        return R(429 if "a.example" in url and json["method"] == "getTransaction" else 200)
    monkeypatch.setattr(http._session, "request", req)
    tx = {"method": "getTransaction"}
    assert http.post_json("https://a.example", tx, priority="live") == {"result": "ok"}
    assert calls == [("https://a.example", "getTransaction"), ("https://c.example", "getTransaction")]
    calls.clear()
    for _ in range(3):  # a stays out for getTransaction: no more 429s, no Helius
        http.post_json("https://a.example", tx, priority="live")
    assert calls == [("https://c.example", "getTransaction")] * 3
    http.post_json("https://a.example", {"method": "getSlot"}, priority="live")  # other methods still go to a
    assert calls[-1] == ("https://a.example", "getSlot")
    st = http.method_stats()["a.example"]["getTransaction"]
    assert st["429"] == 1 and st["avoided"] == 4  # the retry plus three calls
    # backoff doubles per 429 in a row; a success does not lift the running ban
    http._pool_result("a.example", False, status=429, method="getTransaction")
    away = http.pool_status()["a.example"]["avoid"]["getTransaction"] - http.time.time()
    assert 2 * http.METHOD_AVOID_BASE - 2 < away <= 2 * http.METHOD_AVOID_BASE
    http._pool_result("a.example", True, 5.0, 200, method="getTransaction")
    assert "getTransaction" in http.pool_status()["a.example"]["avoid"]


def test_method_ban_grows_when_429s_and_successes_alternate(monkeypatch, tmp_path):
    """17.10.: mainnet-beta answered getTransaction now and then, each success reset the ban to 10 s, so it was
    banned and freed thousands of times. Successes now only wear the strike score down."""
    _fresh(monkeypatch, tmp_path)
    monkeypatch.setenv("PUBLIC_RPC_URLS", "https://a.example,https://c.example")
    monkeypatch.setattr(http, "_method_429", {})
    monkeypatch.setattr(http, "_method_stats", {})
    for _ in range(8):
        http._pool_result("a.example", True, 5.0, 200, method="getTransaction")
        http._pool_result("a.example", False, status=429, method="getTransaction")
    away = http.pool_status()["a.example"]["avoid"]["getTransaction"] - http.time.time()
    assert away > 4 * http.METHOD_AVOID_BASE
    for _ in range(30):  # a long healthy run wears it off completely
        http._pool_result("a.example", True, 5.0, 200, method="getTransaction")
    assert http._method_429[("a.example", "getTransaction")] == 0


def test_own_transactions_keep_helius_when_share_used_up(monkeypatch, tmp_path):
    _credit_env(monkeypatch, tmp_path, "copytrade", day=31, month_credits=100)  # 15 credits today
    for _ in range(15):
        http._charge(1)
    calls = []

    class R:
        status_code = 200
        headers = {}

        def json(self):
            return {"result": "sig"}
    monkeypatch.setattr(http._session, "request", lambda m, url, **kw: calls.append(url) or R())
    helius = "https://beta.helius-rpc.com/?api-key=k"
    assert http.rpc_url("sendTransaction", helius) == helius
    assert http.rpc_url("getBalance", helius) in http.public_rpc_urls()
    http.post_json(helius, {"method": "sendTransaction", "params": []})
    assert calls == [helius]  # not moved to the public node, not refused
