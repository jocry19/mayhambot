"""Solana RPC access and swap extraction from parsed transactions.

Works with any RPC (public mainnet by default). A Helius or other paid RPC in
SOLANA_RPC_URL makes wallet polling faster and avoids 429s.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from . import http
from .config import QUOTE_MINTS, SOL_MINT, Settings

log = logging.getLogger("copytrade.chain")


PUBLIC_RPC = "https://api.mainnet-beta.solana.com"


class Rpc:
    def __init__(self, settings: Settings, url: str | None = None):
        self.url = url or settings.rpc_url
        self.lim = http.limiter("rpc:" + self.url, settings.rpc_max_rps)
        self.shared = http.throttle is not None and (urlparse(self.url).hostname or "") in http.SHARED_HOSTS
        self._id = 0
        self.poller: Rpc | None = None
        if url is None:
            pu = settings.poll_rpc_url or (PUBLIC_RPC if "helius" in self.url else "")
            if pu and pu != "off" and pu != self.url:
                self.poller = Rpc(settings, url=pu)
        self.poll_fallbacks = 0

    def poll_signatures(self, address: str, limit: int = 15, until: str | None = None) -> list[dict]:
        """Cheap "anything new?" check on the free poll RPC; falls back to the main RPC on errors."""
        if self.poller:
            try:
                return self.poller.signatures(address, limit=limit, until=until)
            except Exception as e:
                self.poll_fallbacks += 1
                log.debug("poll rpc failed, using main rpc: %s", e)
        return self.signatures(address, limit=limit, until=until)

    def call(self, method: str, params: list[Any]) -> Any:
        self._id += 1
        if not self.shared:  # Helius / public RPC: core.http.throttle() spaces calls across all processes
            self.lim.wait()
        d = http.post_json(self.url, {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params})
        if "error" in d:
            raise RuntimeError(f"rpc {method}: {d['error']}")
        return d.get("result")

    def signatures(self, address: str, limit: int = 50, until: str | None = None,
                   before: str | None = None) -> list[dict]:
        opts: dict[str, Any] = {"limit": limit, "commitment": "confirmed"}
        if until:
            opts["until"] = until
        if before:
            opts["before"] = before
        return self.call("getSignaturesForAddress", [address, opts]) or []

    def transaction(self, sig: str) -> dict | None:
        return self.call(
            "getTransaction",
            [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0, "commitment": "confirmed"}],
        )

    def balance_sol(self, address: str) -> float:
        r = self.call("getBalance", [address, {"commitment": "confirmed"}])
        return (r or {}).get("value", 0) / 1e9

    def send_raw(self, b64_tx: str) -> str:
        return self.call("sendTransaction", [b64_tx, {"encoding": "base64", "skipPreflight": True, "maxRetries": 3}])

    def signature_status(self, sig: str) -> dict | None:
        r = self.call("getSignatureStatuses", [[sig], {"searchTransactionHistory": True}])
        return ((r or {}).get("value") or [None])[0]


_decimals_cache: dict[str, int] = {}


def token_decimals(rpc: Rpc, mint: str) -> int:
    if mint in _decimals_cache:
        return _decimals_cache[mint]
    dec = 6
    try:
        r = rpc.call("getTokenSupply", [mint])
        dec = int(r["value"]["decimals"])
    except Exception as e:
        log.debug("decimals %s failed: %s", mint, e)
    _decimals_cache[mint] = dec
    return dec


@dataclass
class Swap:
    signature: str
    wallet: str
    block_time: int
    side: str  # buy | sell
    mint: str
    token_amount: float  # UI units, positive
    sol_amount: float  # SOL spent (buy) / received (sell), positive
    price_sol: float  # SOL per token


def _acct_key(k: Any) -> str:
    return k["pubkey"] if isinstance(k, dict) else k


def extract_swap(tx: dict, wallet: str, sol_usd: float = 0.0) -> Swap | None:
    """Turn a parsed transaction into a buy/sell of one token by `wallet`.

    Uses balance deltas, so it works for every DEX/aggregator (Raydium, Orca,
    Meteora, pump.fun, Jupiter...). USDC/USDT legs are converted to SOL.
    """
    if not tx or (tx.get("meta") or {}).get("err"):
        return None
    meta = tx["meta"]
    msg = tx["transaction"]["message"]
    keys = [_acct_key(k) for k in msg["accountKeys"]]
    deltas: dict[str, float] = {}

    def add(entries: list, sign: int) -> None:
        for b in entries or []:
            if b.get("owner") != wallet:
                continue
            amt = float(b["uiTokenAmount"].get("uiAmount") or 0)
            deltas[b["mint"]] = deltas.get(b["mint"], 0.0) + sign * amt

    add(meta.get("postTokenBalances"), +1)
    add(meta.get("preTokenBalances"), -1)

    sol_delta = 0.0
    if wallet in keys:
        i = keys.index(wallet)
        sol_delta = (meta["postBalances"][i] - meta["preBalances"][i]) / 1e9
        if i == 0:  # fee payer: fees are not part of the trade
            sol_delta += meta.get("fee", 0) / 1e9
    sol_delta += deltas.pop(SOL_MINT, 0.0)  # wrapped SOL
    for stable in QUOTE_MINTS - {SOL_MINT}:
        d = deltas.pop(stable, 0.0)
        if d and sol_usd:
            sol_delta += d / sol_usd

    candidates = {m: d for m, d in deltas.items() if abs(d) > 0}
    if not candidates:
        return None
    mint, tok = max(candidates.items(), key=lambda kv: abs(kv[1]))
    # ignore tiny SOL moves (rent only) — need a real quote leg in the other direction
    if tok > 0 and sol_delta < -0.001:
        side, sol_amt = "buy", -sol_delta
    elif tok < 0 and sol_delta > 0.001:
        side, sol_amt = "sell", sol_delta
    else:
        return None
    return Swap(
        signature=tx["transaction"]["signatures"][0],
        wallet=wallet,
        block_time=int(tx.get("blockTime") or 0),
        side=side,
        mint=mint,
        token_amount=abs(tok),
        sol_amount=sol_amt,
        price_sol=sol_amt / abs(tok),
    )


def wallet_swaps(rpc: Rpc, wallet: str, limit: int = 50, until: str | None = None,
                 sol_usd: float = 0.0) -> tuple[list[Swap], str | None]:
    """Recent swaps of a wallet (newest first) and the newest signature seen."""
    sigs = rpc.signatures(wallet, limit=limit, until=until)
    newest = sigs[0]["signature"] if sigs else until
    out: list[Swap] = []
    for s in sigs:
        if s.get("err"):
            continue
        try:
            tx = rpc.transaction(s["signature"])
        except Exception as e:
            log.debug("tx %s failed: %s", s["signature"], e)
            continue
        sw = extract_swap(tx, wallet, sol_usd)
        if sw:
            out.append(sw)
    return out, newest


def pool_buyers(rpc: Rpc, pool_or_mint: str, limit: int = 100, sol_usd: float = 0.0,
                before: str | None = None) -> list[Swap]:
    """Buys that touched a pool/mint address: used to discover whales."""
    out: list[Swap] = []
    for s in rpc.signatures(pool_or_mint, limit=limit, before=before):
        if s.get("err"):
            continue
        try:
            tx = rpc.transaction(s["signature"])
        except Exception:
            continue
        if not tx:
            continue
        signer = _acct_key(tx["transaction"]["message"]["accountKeys"][0])
        sw = extract_swap(tx, signer, sol_usd)
        if sw and sw.side == "buy":
            out.append(sw)
    return out
