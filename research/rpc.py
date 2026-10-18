"""Cheapest-source chain access for research bulk jobs.

Order: public Solana RPC (RESEARCH_RPC_URL, costs no Helius credits, rate-limited in core.http) ->
Helius RPC at 1 credit per call while research has credits left today. The Helius Enhanced API (100 credits per
call, 100 parsed swaps) is used only for wallets that matter (tiered / user wallets). core.http books every
Helius call in its shared ledger and raises CreditsExhausted for Enhanced calls once the research share is used.
"""
import logging

from core.config import settings
from core.http import CreditsExhausted, post_json
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
    if url and credits.can("rpc"):
        return _post(url, method, params)
    if err:
        raise err
    raise RuntimeError("research helius credit cap reached for today")


def get_tx(sig):
    return call("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1}])


_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def is_pubkey(addr):
    """True if addr is base58 that decodes to exactly 32 bytes (a length check alone lets truncated pastes through)."""
    if not addr or not 32 <= len(addr) <= 44 or any(c not in _B58 for c in addr):
        return False
    n = 0
    for c in addr:
        n = n * 58 + _B58.index(c)
    lead = len(addr) - len(addr.lstrip("1"))
    return lead + (n.bit_length() + 7) // 8 == 32


def wallet_trades(wallet, limit=None, enhanced=False):
    """Swaps of a wallet. enhanced=True uses the Helius Enhanced API (100 credits, up to 100 swaps) if the
    daily cap allows; otherwise getSignaturesForAddress + getTransaction (public RPC first).
    None = not fetched or incomplete (the caller retries the wallet later instead of marking it done)."""
    if 32 <= len(wallet) <= 44 and not is_pubkey(wallet):  # truncated paste: RPC answers -32602 forever,
        # [] marks it done instead of retrying every round
        log.info("wallet history %s skipped: not a valid address", wallet[:6])
        return []
    limit = limit or rsettings.wallet_tx_limit
    if enhanced and settings.helius_api_key and credits.can("enhanced"):
        try:
            return core_wh._helius_wallet_trades(wallet, 100)
        except CreditsExhausted:
            log.info("helius credits for research used up today, %s via public RPC instead", wallet[:6])
        except Exception as e:  # noqa: BLE001
            log.warning("enhanced history %s failed (%s), via RPC instead", wallet[:6], e)
    try:
        sigs = call("getSignaturesForAddress", [wallet, {"limit": limit}]) or []
        out = []
        failed = 0
        for s in sigs:
            if s.get("err"):
                continue
            try:
                tx = get_tx(s["signature"])
            except Exception as e:  # noqa: BLE001
                log.debug("getTransaction %s failed: %s", s["signature"][:8], e)
                failed += 1
                continue
            out.extend(core_wh.parse_rpc_tx(tx, wallet, s["signature"]))
        if core_wh.too_many_failed(failed, len(sigs)):
            log.info("wallet history %s incomplete (%d/%d getTransaction failed), retried later",
                     wallet[:6], failed, len(sigs))
            return None
        return out
    except Exception as e:  # noqa: BLE001
        log.warning("wallet history %s failed: %s", wallet[:6], e)
        return None


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
