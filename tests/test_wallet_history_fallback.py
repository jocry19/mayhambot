import pytest

from core.sources import wallet_history as wh


def test_429_skips_instead_of_enhanced(monkeypatch):
    def rpc(*a, **k):
        raise RuntimeError("429 for https://api.mainnet-beta.solana.com")
    monkeypatch.setattr(wh, "_rpc_wallet_trades", rpc)
    monkeypatch.setattr(wh, "_helius_wallet_trades", lambda *a: pytest.fail("no Enhanced on 429"))
    monkeypatch.setattr(wh.settings, "helius_api_key", "k", raising=False)
    with pytest.raises(wh.SkipForNow):
        wh.wallet_trades("W" * 44)


def test_enhanced_fallback_capped_per_hour(monkeypatch):
    monkeypatch.setattr(wh, "_rpc_wallet_trades", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("timeout")))
    used = []
    monkeypatch.setattr(wh, "_helius_wallet_trades", lambda w, l: used.append(w) or [])
    monkeypatch.setattr(wh.settings, "helius_api_key", "k", raising=False)
    monkeypatch.setattr(wh, "_enhanced_used", [])
    for i in range(wh.ENHANCED_FALLBACKS_PER_HOUR):
        assert wh.wallet_trades(f"W{i}") == []
    with pytest.raises(wh.SkipForNow):
        wh.wallet_trades("Wlast")
    assert len(used) == wh.ENHANCED_FALLBACKS_PER_HOUR


def test_incomplete_history_is_skipped_not_marked(monkeypatch):
    sigs = [{"signature": f"s{i}"} for i in range(10)]
    monkeypatch.setattr(wh, "_rpc", lambda m, p, url=None: sigs)

    def get_tx(sig, url=None):
        if sig in ("s1", "s2", "s3"):
            raise RuntimeError("429")
        return None
    monkeypatch.setattr(wh, "_get_tx", get_tx)
    with pytest.raises(wh.SkipForNow):
        wh._rpc_wallet_trades("W", 10)
    monkeypatch.setattr(wh, "_get_tx", lambda sig, url=None: (_ for _ in ()).throw(RuntimeError("x"))
                        if sig == "s1" else None)
    assert wh._rpc_wallet_trades("W", 10) == []  # one lost tx of ten is tolerated


def test_no_source_left_is_skip_not_empty(monkeypatch):
    monkeypatch.setattr(wh, "_rpc_wallet_trades", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("timeout")))
    monkeypatch.setattr(wh.settings, "helius_api_key", "", raising=False)
    monkeypatch.setattr(wh.settings, "rpc_url", wh.PUBLIC_RPC, raising=False)
    with pytest.raises(wh.SkipForNow):
        wh.wallet_trades("W" * 44)


def test_research_incomplete_history_is_none(monkeypatch):
    from research import rpc
    monkeypatch.setattr(rpc, "call", lambda m, p: [{"signature": f"s{i}"} for i in range(5)])
    monkeypatch.setattr(rpc, "get_tx", lambda sig: (_ for _ in ()).throw(RuntimeError("429")))
    assert rpc.wallet_trades("W") is None
