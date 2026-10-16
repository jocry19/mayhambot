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
