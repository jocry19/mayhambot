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
