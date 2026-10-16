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
    assert calls == [http.PUBLIC_RPC]
    assert http.rpc_url("getSlot", "https://beta.helius-rpc.com/?api-key=k") == http.PUBLIC_RPC
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
    assert sum(http.DEFAULT_CREDIT_SHARES.values()) == 100 and http.DEFAULT_CREDIT_SHARES["research"] == 50


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
