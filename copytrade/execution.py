"""Order execution.

PaperExecutor  fills from real market data: a Jupiter quote for the exact size
               (real route, real price impact) when reachable, otherwise a
               constant-product estimate from DexScreener liquidity; then adds
               adverse slippage for latency plus network and priority fees.
LiveExecutor   real swaps through the Jupiter swap API, signed locally with the
               key from .env / keyfile. Only used when LIVE_TRADING=true and a key
               is configured.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

from . import chain, market
from .config import SOL_MINT, Settings

log = logging.getLogger("copytrade.exec")

PAPER_ADVERSE_SLIPPAGE = float(os.getenv("CT_PAPER_ADVERSE_SLIPPAGE", "0.02"))
DEX_FEE_FALLBACK = 0.0035  # Raydium/Meteora ~0.25%, pump.fun 1% -> blended guess when no quote


@dataclass
class Fill:
    ok: bool
    tokens: float = 0.0  # UI units bought / sold
    sol: float = 0.0  # SOL spent (incl. fees) on buy, SOL received (after fees) on sell
    price_sol: float = 0.0  # effective SOL per token
    price_impact: float = 0.0
    fees_sol: float = 0.0
    tx: str = ""
    source: str = ""
    error: str = ""


def _cp_impact(size_sol: float, snap: market.PairSnapshot, sol_usd: float) -> float:
    """Price impact of a constant-product pool for a trade of size_sol."""
    pool_sol = (snap.liquidity_usd / 2) / sol_usd if sol_usd else 0
    if pool_sol <= 0:
        return 0.5
    return size_sol / (pool_sol + size_sol)


class PaperExecutor:
    mode = "paper"

    def __init__(self, settings: Settings, rpc: chain.Rpc):
        self.s = settings
        self.rpc = rpc

    def buy(self, mint: str, size_sol: float, snap: market.PairSnapshot) -> Fill:
        fees = self.s.network_fee_sol + self.s.priority_fee_sol
        spend = size_sol - fees
        dec = chain.token_decimals(self.rpc, mint)
        q = market.jupiter_quote(self.s, SOL_MINT, mint, int(spend * 1e9))
        if q and int(q.get("outAmount", 0)) > 0:
            tokens = int(q["outAmount"]) / 10 ** dec
            impact = float(q.get("priceImpactPct") or 0)
            src = "jupiter_quote"
        else:
            impact = _cp_impact(spend, snap, market.sol_usd(self.s))
            tokens = spend / snap.price_sol * (1 - impact) * (1 - DEX_FEE_FALLBACK) if snap.price_sol else 0
            src = "liquidity_estimate"
        tokens *= 1 - PAPER_ADVERSE_SLIPPAGE
        if tokens <= 0:
            return Fill(False, error="no price")
        return Fill(True, tokens=tokens, sol=size_sol, price_sol=size_sol / tokens, price_impact=impact,
                    fees_sol=fees, tx=f"paper-{int(time.time() * 1000)}", source=src)

    def sell(self, mint: str, tokens: float, decimals: int, snap: market.PairSnapshot | None,
             last_price_sol: float) -> Fill:
        fees = self.s.network_fee_sol + self.s.priority_fee_sol
        q = market.jupiter_quote(self.s, mint, SOL_MINT, int(tokens * 10 ** decimals))
        if q and int(q.get("outAmount", 0)) > 0:
            gross = int(q["outAmount"]) / 1e9
            impact = float(q.get("priceImpactPct") or 0)
            src = "jupiter_quote"
        else:
            price = snap.price_sol if snap and snap.price_sol else last_price_sol
            value = tokens * price
            impact = _cp_impact(value, snap, market.sol_usd(self.s)) if snap else 0.1
            gross = value * (1 - impact) * (1 - DEX_FEE_FALLBACK)
            src = "liquidity_estimate"
        gross *= 1 - PAPER_ADVERSE_SLIPPAGE
        net = max(0.0, gross - fees)
        return Fill(True, tokens=tokens, sol=net, price_sol=net / tokens if tokens else 0, price_impact=impact,
                    fees_sol=fees, tx=f"paper-{int(time.time() * 1000)}", source=src)

    def balance(self, conn) -> float:
        from . import store

        v = store.get_state(conn, "paper_balance_sol")
        if v is None:
            store.set_state(conn, "paper_balance_sol", self.s.paper_start_balance_sol)
            return self.s.paper_start_balance_sol
        return float(v)

    def adjust_balance(self, conn, delta: float) -> None:
        self.balance(conn)  # make sure the row exists
        conn.execute("UPDATE ct_state SET value = CAST(value AS REAL) + ? WHERE key='paper_balance_sol'", (delta,))
        conn.commit()


class LiveExecutor(PaperExecutor):
    mode = "live"

    def __init__(self, settings: Settings, rpc: chain.Rpc):
        super().__init__(settings, rpc)
        if not settings.live_ready:
            raise RuntimeError("live trading needs LIVE_TRADING=true and SOLANA_PRIVATE_KEY or SOLANA_KEYPAIR_PATH")
        self.keypair = _load_keypair(settings)
        self.pubkey = str(self.keypair.pubkey())

    def _swap(self, input_mint: str, output_mint: str, amount_raw: int) -> tuple[str, dict]:
        from solders.transaction import VersionedTransaction  # type: ignore

        q = market.jupiter_quote(self.s, input_mint, output_mint, amount_raw)
        if not q:
            raise RuntimeError("no Jupiter quote")
        raw = base64.b64decode(market.jupiter_swap_tx(self.s, q, self.pubkey))
        unsigned = VersionedTransaction.from_bytes(raw)
        signed = VersionedTransaction(unsigned.message, [self.keypair])
        sig = self.rpc.send_raw(base64.b64encode(bytes(signed)).decode())
        for _ in range(40):  # ~60 s
            time.sleep(1.5)
            st = self.rpc.signature_status(sig)
            if st and st.get("confirmationStatus") in ("confirmed", "finalized"):
                if st.get("err"):
                    raise RuntimeError(f"swap failed on chain: {st['err']}")
                return sig, q
        raise RuntimeError(f"swap {sig} not confirmed")

    def _actual(self, sig: str) -> chain.Swap | None:
        for _ in range(5):
            tx = self.rpc.transaction(sig, main=True)
            if tx:
                return chain.extract_swap(tx, self.pubkey, market.sol_usd(self.s))
            time.sleep(2)
        return None

    def buy(self, mint: str, size_sol: float, snap: market.PairSnapshot) -> Fill:
        try:
            sig, q = self._swap(SOL_MINT, mint, int(size_sol * 1e9))
        except Exception as e:
            return Fill(False, error=str(e))
        sw = self._actual(sig)
        dec = chain.token_decimals(self.rpc, mint)
        tokens = sw.token_amount if sw else int(q["outAmount"]) / 10 ** dec
        spent = sw.sol_amount if sw else size_sol
        return Fill(True, tokens=tokens, sol=spent, price_sol=spent / tokens, tx=sig,
                    price_impact=float(q.get("priceImpactPct") or 0), source="live")

    def sell(self, mint: str, tokens: float, decimals: int, snap, last_price_sol: float) -> Fill:
        try:
            sig, q = self._swap(mint, SOL_MINT, int(tokens * 10 ** decimals))
        except Exception as e:
            return Fill(False, error=str(e))
        sw = self._actual(sig)
        got = sw.sol_amount if sw else int(q["outAmount"]) / 1e9
        return Fill(True, tokens=tokens, sol=got, price_sol=got / tokens if tokens else 0, tx=sig,
                    price_impact=float(q.get("priceImpactPct") or 0), source="live")

    def balance(self, conn) -> float:
        return self.rpc.balance_sol(self.pubkey)

    def adjust_balance(self, conn, delta: float) -> None:
        pass  # the chain is the source of truth


def _load_keypair(s: Settings):
    from solders.keypair import Keypair  # type: ignore

    if s.keypair_path:
        data = json.loads(Path(os.path.expanduser(s.keypair_path)).read_text())
        return Keypair.from_bytes(bytes(data))
    key = s.private_key.strip()
    if key.startswith("["):
        return Keypair.from_bytes(bytes(json.loads(key)))
    return Keypair.from_base58_string(key)


def make_executor(settings: Settings, rpc: chain.Rpc):
    return LiveExecutor(settings, rpc) if settings.live_ready else PaperExecutor(settings, rpc)


def make_executors(settings: Settings, rpc: chain.Rpc) -> dict:
    """Paper always; live only when .env has LIVE_TRADING=true and a key (the master switch).
    Which strategy actually trades live is chosen per strategy in control (ct_control live:<strategy>)."""
    exs: dict = {"paper": PaperExecutor(settings, rpc), "live": None}
    if settings.live_ready:
        try:
            exs["live"] = LiveExecutor(settings, rpc)
        except Exception as e:
            log.error("live executor unavailable: %s", e)
    return exs
