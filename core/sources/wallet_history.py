"""A wallet's recent swaps. Helius enhanced API if HELIUS_API_KEY is set, else plain Solana JSON-RPC.

USD values of historical trades use the current SOL price (approximation; memecoin trades are SOL-quoted).
"""
import logging
from collections import defaultdict

from core.config import settings
from core.http import PUBLIC_RPC, get_json, post_json
from core.models import SOL_MINT, STABLE_MINTS, Trade
from core.sources.market import sol_price_usd

log = logging.getLogger("mayhambot.wallet_history")
MIN_SOL = 0.005  # ignore dust / rent-only movements


def _net_to_trades(sig, ts, wallet, token_delta, sol_delta, stable_delta, source):
    """token_delta: {mint: amount}, sol_delta/stable_delta from the wallet's perspective."""
    moved = {m: d for m, d in token_delta.items() if abs(d) > 0}
    if len(moved) != 1:
        return []  # transfers, multi-hop or LP actions: skip
    mint, d = next(iter(moved.items()))
    sol_usd = sol_price_usd() or 0.0
    if d > 0 and (sol_delta < -MIN_SOL or stable_delta < -1):
        side = "buy"
        usd = (-sol_delta * sol_usd) if sol_delta < -MIN_SOL else -stable_delta
    elif d < 0 and (sol_delta > MIN_SOL or stable_delta > 1):
        side = "sell"
        usd = (sol_delta * sol_usd) if sol_delta > MIN_SOL else stable_delta
    else:
        return []
    sol = abs(sol_delta) if abs(sol_delta) > MIN_SOL else (usd / sol_usd if sol_usd else 0.0)
    amt = abs(d)
    return [Trade(sig=sig, wallet=wallet, mint=mint, side=side, ts=int(ts or 0), token_amount=amt,
                  sol_amount=sol, usd_amount=usd, price_usd=usd / amt if amt else 0.0, source=source)]


# ---------------- plain RPC ----------------

def _rpc(method, params, url=None):
    j = post_json(url or settings.rpc_url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    if "error" in j:
        raise RuntimeError(j["error"])
    return j.get("result")


def parse_rpc_tx(tx, wallet, sig=None):
    if not tx or not tx.get("meta") or tx["meta"].get("err"):
        return []
    meta = tx["meta"]
    keys = [k["pubkey"] if isinstance(k, dict) else k for k in tx["transaction"]["message"]["accountKeys"]]
    if wallet not in keys:
        return []
    idx = keys.index(wallet)
    sol = (meta["postBalances"][idx] - meta["preBalances"][idx]) / 1e9
    if idx == 0:
        sol += meta.get("fee", 0) / 1e9
    tok = defaultdict(float)

    def amt(b):
        u = b.get("uiTokenAmount") or {}
        return float(u.get("uiAmountString") or u.get("uiAmount") or 0)

    for b in meta.get("preTokenBalances") or []:
        if b.get("owner") == wallet:
            tok[b["mint"]] -= amt(b)
    for b in meta.get("postTokenBalances") or []:
        if b.get("owner") == wallet:
            tok[b["mint"]] += amt(b)
    sol += tok.pop(SOL_MINT, 0.0)
    stable = sum(tok.pop(m, 0.0) for m in list(tok) if m in STABLE_MINTS)
    return _net_to_trades(sig or tx["transaction"]["signatures"][0], tx.get("blockTime"), wallet,
                          tok, sol, stable, "rpc")


def _get_tx(sig, url=None):
    return _rpc("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1}], url)


def early_buyers(mint, max_pages=8, n_tx=120):
    """Trades from the first transactions of a token (its earliest buyers), via RPC.

    Pages getSignaturesForAddress(mint) back to the oldest signature. Returns [] if the token has more
    than max_pages*1000 transactions (too old/busy to reach its start cheaply)."""
    before, oldest = None, []
    for _ in range(max_pages):
        opts = {"limit": 1000}
        if before:
            opts["before"] = before
        page = _rpc("getSignaturesForAddress", [mint, opts]) or []
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
            tx = _get_tx(s["signature"])
        except Exception as e:  # noqa: BLE001
            log.debug("getTransaction %s failed: %s", s["signature"], e)
            continue
        if not tx:
            continue
        keys = tx["transaction"]["message"]["accountKeys"]
        signer = keys[0]["pubkey"] if isinstance(keys[0], dict) else keys[0]
        out.extend(t for t in parse_rpc_tx(tx, signer, s["signature"]) if t.mint == mint)
    for t in out:
        t.source = "rpc_early"
    return out


def _rpc_wallet_trades(wallet, limit, url=None):
    sigs = _rpc("getSignaturesForAddress", [wallet, {"limit": limit}], url) or []
    out = []
    for s in sigs:
        if s.get("err"):
            continue
        try:
            tx = _get_tx(s["signature"], url)
        except Exception as e:  # noqa: BLE001
            log.debug("getTransaction %s failed: %s", s["signature"], e)
            continue
        out.extend(parse_rpc_tx(tx, wallet, s["signature"]))
    return out


# ---------------- Helius enhanced ----------------

def parse_helius_tx(tx, wallet):
    tok = defaultdict(float)
    sol = 0.0
    for t in tx.get("tokenTransfers") or []:
        a = float(t.get("tokenAmount") or 0)
        if t.get("toUserAccount") == wallet:
            tok[t["mint"]] += a
        if t.get("fromUserAccount") == wallet:
            tok[t["mint"]] -= a
    for n in tx.get("nativeTransfers") or []:
        a = float(n.get("amount") or 0) / 1e9
        if n.get("toUserAccount") == wallet:
            sol += a
        if n.get("fromUserAccount") == wallet:
            sol -= a
    sol += tok.pop(SOL_MINT, 0.0)
    stable = sum(tok.pop(m, 0.0) for m in list(tok) if m in STABLE_MINTS)
    return _net_to_trades(tx.get("signature", ""), tx.get("timestamp"), wallet, tok, sol, stable, "helius")


def _helius_wallet_trades(wallet, limit):
    txs = get_json(f"https://api.helius.xyz/v0/addresses/{wallet}/transactions",
                   params={"api-key": settings.helius_api_key, "type": "SWAP", "limit": min(limit, 100)}) or []
    out = []
    for tx in txs:
        if tx.get("transactionError"):
            continue
        out.extend(parse_helius_tx(tx, wallet))
    return out


def wallet_trades(wallet, limit=60, enhanced_fallback=True) -> list:
    """A wallet's recent swaps. Public RPC first (0 Helius credits); only if that fails, Helius Enhanced
    (100 credits, within this service's daily credit share) or the configured RPC."""
    try:
        return _rpc_wallet_trades(wallet, limit, PUBLIC_RPC)
    except Exception as e:  # noqa: BLE001
        log.info("wallet history %s via public RPC failed (%s), trying Helius", wallet[:6], e)
    try:
        if settings.helius_api_key and enhanced_fallback:
            return _helius_wallet_trades(wallet, limit)
        if settings.rpc_url != PUBLIC_RPC:
            return _rpc_wallet_trades(wallet, limit)
    except Exception as e:  # noqa: BLE001 - incl. core.http.CreditsExhausted
        log.warning("wallet history %s failed: %s", wallet[:6], e)
    return []
