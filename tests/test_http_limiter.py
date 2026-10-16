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
