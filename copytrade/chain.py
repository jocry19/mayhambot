"""Solana RPC access and swap extraction from parsed transactions.

Works with any RPC (public mainnet by default). A Helius or other paid RPC in
SOLANA_RPC_URL makes wallet polling faster and avoids 429s.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import struct
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from . import http
from .config import QUOTE_MINTS, SOL_MINT, Settings

log = logging.getLogger("copytrade.chain")


PUBLIC_RPC = "https://api.mainnet-beta.solana.com"
READ_METHODS = {"getSignaturesForAddress", "getTransaction", "getTokenSupply"}

try:  # Helius credit ledger shared by all mayhambot processes (core/README.md "Helius monthly credits")
    from core.http import CreditsExhausted, rpc_url as _core_rpc_url  # type: ignore
except Exception:  # copytrade without core
    _core_rpc_url = None
try:  # pool of keyless public endpoints with probe, failover and per-endpoint lanes (core/README.md)
    from core import http as _core_http  # type: ignore

    if not hasattr(_core_http, "pick_public_rpc"):
        _core_http = None
except Exception:
    _core_http = None


def _is_pool_url(url: str) -> bool:
    if not _core_http:
        return False
    host = urlparse(url).netloc
    return host in {urlparse(u).netloc for u in _core_http.public_rpc_urls()} or url.rstrip("/") == PUBLIC_RPC

    class CreditsExhausted(Exception):  # type: ignore[no-redef]
        pass


class Rpc:
    """JSON-RPC client. Bulk reads (signatures, transactions, token supply) go to the free public RPC first and
    use the main RPC (Helius, paid in credits) only as fallback; live paths (balance, send, status) use the
    main RPC. Once copytrade's Helius credits for today are used up, core routes standard calls to public."""

    def __init__(self, settings: Settings, url: str | None = None):
        self.url = url or settings.rpc_url
        self.lim = http.limiter("rpc:" + self.url, settings.rpc_max_rps)
        self.shared = http.throttle is not None and (urlparse(self.url).hostname or "") in http.SHARED_HOSTS
        self._id = 0
        self.public: Rpc | None = None
        if url is None:
            pu = settings.poll_rpc_url or (PUBLIC_RPC if "helius" in self.url else "")
            if pu and pu != "off" and pu != self.url:
                self.public = Rpc(settings, url=pu)
        self.poller = self.public  # old name
        self.fallbacks = 0
        self.bulk_failures = 0  # public bulk reads that failed (not retried on Helius)
        self.counts: dict[str, int] = {}  # "public:getTransaction" -> n (hourly usage log)
        self.pooled = url is not None and _is_pool_url(self.url)  # rotate over core's public endpoint pool
        self.hosts: dict[str, int] = {}  # public endpoint host -> calls (hourly usage log)

    @property
    def poll_fallbacks(self) -> int:
        return self.fallbacks

    def call(self, method: str, params: list[Any], priority: str = "live") -> Any:
        """priority 'live' (default: own trades, fresh whale buys) or 'bulk' (backfill, discovery, net polls)."""
        self._id += 1
        payload = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}
        if self.pooled:  # core picks the endpoint (live: fastest, bulk: most room), books latency and failures
            url = _core_http.pick_public_rpc(priority)
            host = urlparse(url).netloc
            self.hosts[host] = self.hosts.get(host, 0) + 1
            self.counts[f"public:{method}"] = self.counts.get(f"public:{method}", 0) + 1
            live = priority == "live"
            d = _core_http.post_json(url, payload, priority=priority, retries=1 if live else 3,
                                     timeout=10 if live else 20)
            if "error" in d:
                raise RuntimeError(f"rpc {method}: {d['error']}")
            return d.get("result")
        url = _core_rpc_url(method, self.url) if _core_rpc_url and "helius" in self.url else self.url
        if not self.shared:  # Helius / public RPC: core.http.throttle() spaces calls across all processes
            self.lim.wait()
        kind = "public" if url != self.url or "helius" not in url else "helius"
        self.counts[f"{kind}:{method}"] = self.counts.get(f"{kind}:{method}", 0) + 1
        d = http.post_json(url, payload, priority=priority, rpc_method=method)
        if "error" in d:
            raise RuntimeError(f"rpc {method}: {d['error']}")
        return d.get("result")

    def read(self, method: str, params: list[Any], main_if_empty: bool = False, priority: str = "bulk") -> Any:
        """Public RPC first, main RPC on errors (or on an empty answer when main_if_empty)."""
        if self.public:
            try:
                r = self.public.call(method, params, priority)
                if r is not None or not main_if_empty:
                    return r
            except CreditsExhausted:
                raise
            except Exception as e:
                if priority != "live":  # backfill, discovery, net polls: try again later, no Helius credits
                    self.bulk_failures += 1
                    raise
                log.debug("public rpc %s failed, using main rpc: %s", method, e)
            self.fallbacks += 1
        return self.call(method, params, priority)

    def endpoints(self) -> dict[str, int]:
        """Public endpoint host -> calls since the last reset."""
        return dict(self.public.hosts) if self.public else dict(self.hosts)

    def usage(self) -> dict[str, int]:
        out = dict(self.counts)
        for k, v in (self.public.counts if self.public else {}).items():
            out[k] = out.get(k, 0) + v
        return out

    def signatures(self, address: str, limit: int = 50, until: str | None = None,
                   before: str | None = None, priority: str = "bulk") -> list[dict]:
        opts: dict[str, Any] = {"limit": limit, "commitment": "confirmed"}
        if until:
            opts["until"] = until
        if before:
            opts["before"] = before
        return self.read("getSignaturesForAddress", [address, opts], priority=priority) or []

    def poll_signatures(self, address: str, limit: int = 15, until: str | None = None,
                        priority: str = "bulk") -> list[dict]:
        return self.signatures(address, limit=limit, until=until, priority=priority)

    def transaction(self, sig: str, main: bool = False, priority: str = "bulk") -> dict | None:
        """main=True: our own live swap (straight to the main RPC, it sees fresh transactions first)."""
        params = [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0, "commitment": "confirmed"}]
        if main:
            return self.call("getTransaction", params, "live")
        return self.read("getTransaction", params, priority=priority)

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
    try:
        r = rpc.read("getTokenSupply", [mint], priority="live") if hasattr(rpc, "read") else rpc.call("getTokenSupply", [mint])
        dec = int(r["value"]["decimals"])
    except Exception as e:
        log.debug("decimals %s failed: %s", mint, e)
        return 6  # pump.fun default for now, but not cached: the next call asks again
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


# ---------------------------------------------------------------- pump.fun trades straight from the logs
PUMP_PROGRAM = "6EF8rrDYGrSquN2vUbW2kH7JMLtQFZyD3J4bLy1F2uY3"
_TRADE_DISC = hashlib.sha256(b"event:TradeEvent").digest()[:8]
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _b58(b: bytes) -> str:
    n, out = int.from_bytes(b, "big"), ""
    while n:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    return "1" * (len(b) - len(b.lstrip(b"\0"))) + out


def decode_pump_trade(data_b64: str) -> dict | None:
    """pump.fun TradeEvent (Anchor event in a 'Program data:' log line) -> dict, else None. Layout: disc 8,
    mint 32, sol_amount u64, token_amount u64, is_buy bool, user 32, timestamp i64, virtual reserves 2x u64."""
    try:
        raw = base64.b64decode(data_b64)
    except (ValueError, TypeError):
        return None
    if len(raw) < 113 or raw[:8] != _TRADE_DISC:
        return None
    sol, tok = struct.unpack_from("<QQ", raw, 40)
    ts, vsol, vtok = struct.unpack_from("<qQQ", raw, 89)
    return {"mint": _b58(raw[8:40]), "sol": sol / 1e9, "tokens": tok / 1e6, "is_buy": raw[56] == 1,
            "user": _b58(raw[57:89]), "ts": ts, "v_sol": vsol / 1e9, "v_tok": vtok / 1e6}


def swap_from_logs(logs: list | None, wallet: str, signature: str) -> Swap | None:
    """The wallet's pump.fun buy/sell read from the websocket notification's own logs: no getTransaction, so a
    whale buy is known well under a second after the notification. Only the clear case (all of the wallet's
    trade events in one coin and one direction, logs complete); anything else returns None and goes the normal
    getTransaction way. Amounts are the curve's (without pump.fun's ~1 % fee); the full read follows later."""
    if not logs or any("Log truncated" in (ln or "") for ln in logs):
        return None
    evs = []
    for ln in logs:
        if isinstance(ln, str) and ln.startswith("Program data: "):
            ev = decode_pump_trade(ln[14:].strip())
            if ev and ev["user"] == wallet:
                evs.append(ev)
    if not evs or len({(e["mint"], e["is_buy"]) for e in evs}) != 1:
        return None
    sol, tok = sum(e["sol"] for e in evs), sum(e["tokens"] for e in evs)
    if tok <= 0 or sol < 0.001:
        return None
    return Swap(signature=signature, wallet=wallet, block_time=int(evs[-1]["ts"] or 0),
                side="buy" if evs[0]["is_buy"] else "sell", mint=evs[0]["mint"], token_amount=tok,
                sol_amount=sol, price_sol=sol / tok)


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
