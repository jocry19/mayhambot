"""Cheapest-source chain access for research bulk jobs.

Order: public Solana RPC (RESEARCH_RPC_URL, costs no Helius credits, rate-limited in core.http) ->
Helius RPC at 1 credit per call, only while research.credits has budget left today. The Helius Enhanced API
(100 credits per call, 100 parsed swaps) is used only for wallets that matter (tiered / user wallets).
"""
import logging

from core.config import settings
from core.http import post_json
from core.sources import wallet_history as core_wh
from research import credits
from research.config import rsettings

log = logging.getLogger("mayhambot.research.rpc")


def _helius_rpc_url():
    if "helius" in (settings.rpc_url or ""):
        return settings.rpc_url
    return settings.helius_rpc_url


def _post(url, method, params):
    j = post_json(url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    if "error" in j:
        raise RuntimeError(j["error"])
    return j.get("result")


def call(method, params):
    """JSON-RPC via the free public endpoint; falls back to Helius (1 credit) within the daily research cap."""
    pub = rsettings.rpc_url
    if pub and "helius" not in pub:
        try:
            return _post(pub, method, params)
        except Exception as e:  # noqa: BLE001
            log.debug("public rpc %s failed: %s", method, e)
            err = e
    else:
        err = None
    url = _helius_rpc_url()
    if url and credits.take("rpc"):
        return _post(url, method, params)
    if err:
        raise err
    raise RuntimeError("research helius credit cap reached for today")


def get_tx(sig):
    return call("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1}])


def wallet_trades(wallet, limit=None, enhanced=False):
    """Swaps of a wallet. enhanced=True uses the Helius Enhanced API (100 credits, up to 100 swaps) if the
    daily cap allows; otherwise getSignaturesForAddress + getTransaction (public RPC first)."""
    limit = limit or rsettings.wallet_tx_limit
    try:
        if enhanced and settings.helius_api_key and credits.take("enhanced"):
            return core_wh._helius_wallet_trades(wallet, 100)
        sigs = call("getSignaturesForAddress", [wallet, {"limit": limit}]) or []
        out = []
        for s in sigs:
            if s.get("err"):
                continue
            try:
                tx = get_tx(s["signature"])
            except Exception as e:  # noqa: BLE001
                log.debug("getTransaction %s failed: %s", s["signature"][:8], e)
                continue
            out.extend(core_wh.parse_rpc_tx(tx, wallet, s["signature"]))
        return out
    except Exception as e:  # noqa: BLE001
        log.warning("wallet history %s failed: %s", wallet[:6], e)
        return []


def early_buyers(mint, max_pages=8, n_tx=120):
    """Same as core early_buyers, but through call() (public RPC first, capped Helius fallback)."""
    before, oldest = None, []
    for _ in range(max_pages):
        opts = {"limit": 1000}
        if before:
            opts["before"] = before
        page = call("getSignaturesForAddress", [mint, opts]) or []
        if not page:
            break
        oldest = page
        before = page[-1]["signature"]
        if len(page) < 1000:
            break
    else:
        return []
    out = []
    for s in reversed(oldest[-n_tx:]):
        if s.get("err"):
            continue
        try:
            tx = get_tx(s["signature"])
        except Exception as e:  # noqa: BLE001
            log.debug("getTransaction %s failed: %s", s["signature"][:8], e)
            continue
        if not tx:
            continue
        keys = tx["transaction"]["message"]["accountKeys"]
        signer = keys[0]["pubkey"] if isinstance(keys[0], dict) else keys[0]
        out.extend(t for t in core_wh.parse_rpc_tx(tx, signer, s["signature"]) if t.mint == mint)
    for t in out:
        t.source = "rpc_early"
    return out
