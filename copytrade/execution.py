"""Order execution.

PaperExecutor  fills from real market data: a Jupiter quote for the exact size
               (real route, real price impact) when reachable, otherwise a
               constant-product estimate from DexScreener liquidity; then adds
               adverse slippage for latency plus network and priority fees.
live executor  live.GuardedLiveExecutor (live/README.md): real swaps through Jupiter, signed
               locally, behind hard limits and a kill switch. Only with LIVE_TRADING=true and
               a key; LIVE_DRY_RUN (default on) rehearses without sending.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass

from . import chain, market
from .config import SOL_MINT, Settings

log = logging.getLogger("copytrade.exec")

PAPER_ADVERSE_SLIPPAGE = float(os.getenv("CT_PAPER_ADVERSE_SLIPPAGE", "0.02"))
_adverse = PAPER_ADVERSE_SLIPPAGE  # replaced by the measured value from learn.landing_drift unless .env pins it


def paper_adverse() -> float:
    return _adverse


def set_paper_adverse(x: float) -> None:
    """Adverse slippage per side for paper fills (price move between quote and landing). Ignored if .env sets it."""
    global _adverse
    if "CT_PAPER_ADVERSE_SLIPPAGE" not in os.environ:
        _adverse = x
NO_ROUTE_MIN_LIQ_USD = 1000  # paper sell without a Jupiter quote: below this liquidity the sale books 0
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
    decimals: int | None = None  # token decimals the amounts were computed with (book these, not a guess)


def decimals(rpc, mint: str) -> int | None:
    """Token decimals, None when unknown: never guess 6 for a buy (a 9-decimal coin would be booked 1000x off).
    Uses chain.token_decimals(strict=True) where available."""
    try:
        return chain.token_decimals(rpc, mint, strict=True)
    except TypeError:  # chain.py without strict (or a test stub): plain call; implausible() still guards the fill
        return chain.token_decimals(rpc, mint)
    except Exception:
        return None


def implausible(price_sol: float, snap) -> str | None:
    """A fill price 20x away from the market price is a units error (decimals), not a market move."""
    ref = getattr(snap, "price_sol", 0) or 0
    if ref > 0 and price_sol > 0 and not (1 / 20 < price_sol / ref < 20):
        return f"Fill-Preis {price_sol:.3g} passt nicht zum Marktpreis {ref:.3g} (Einheiten/Dezimalen?)"
    return None


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
        dec = decimals(self.rpc, mint)
        if dec is None:
            return Fill(False, error="Token-Dezimalen unbekannt (kein Kauf auf Verdacht)")
        q = market.jupiter_quote(self.s, SOL_MINT, mint, int(spend * 1e9))
        if q and int(q.get("outAmount", 0)) > 0:
            tokens = int(q["outAmount"]) / 10 ** dec
            impact = float(q.get("priceImpactPct") or 0)
            src = "jupiter_quote"
        else:
            impact = _cp_impact(spend, snap, market.sol_usd(self.s))
            tokens = spend / snap.price_sol * (1 - impact) * (1 - DEX_FEE_FALLBACK) if snap.price_sol else 0
            src = "liquidity_estimate"
        tokens *= 1 - _adverse
        if tokens <= 0:
            return Fill(False, error="no price")
        bad = implausible(size_sol / tokens, snap)
        if bad:
            return Fill(False, error=bad)
        return Fill(True, tokens=tokens, sol=size_sol, price_sol=size_sol / tokens, price_impact=impact,
                    fees_sol=fees, tx=f"paper-{int(time.time() * 1000)}", source=src, decimals=dec)

    def sell(self, mint: str, tokens: float, decimals: int, snap: market.PairSnapshot | None,
             last_price_sol: float) -> Fill:
        fees = self.s.network_fee_sol + self.s.priority_fee_sol
        q = market.jupiter_quote(self.s, mint, SOL_MINT, int(tokens * 10 ** decimals))
        if q and int(q.get("outAmount", 0)) > 0:
            gross = int(q["outAmount"]) / 1e9
            impact = float(q.get("priceImpactPct") or 0)
            src = "jupiter_quote"
        elif snap and snap.price_sol and snap.liquidity_usd >= NO_ROUTE_MIN_LIQ_USD:
            value = tokens * snap.price_sol
            impact = _cp_impact(value, snap, market.sol_usd(self.s))
            gross = value * (1 - impact) * (1 - DEX_FEE_FALLBACK)
            src = "liquidity_estimate"
        else:  # no route and no market with liquidity (pulled LP, delisted pair): the tokens sell for nothing.
            # 10/2026 audit: booking last_price x 0.9 here turned rugs into -12 % instead of about -100 %
            impact, gross, src = 1.0, 0.0, "no_route"
        gross *= 1 - _adverse
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

    def adjust_balance(self, conn, delta: float, commit: bool = True) -> None:
        """commit=False: part of the caller's transaction (the position row changes in the same commit)."""
        from . import store

        if store.get_state(conn, "paper_balance_sol") is None:
            conn.execute("INSERT OR IGNORE INTO ct_state(key, value) VALUES ('paper_balance_sol', ?)",
                         (str(self.s.paper_start_balance_sol),))
        conn.execute("UPDATE ct_state SET value = CAST(value AS REAL) + ? WHERE key='paper_balance_sol'", (delta,))
        if commit:
            conn.commit()


def _live(settings: Settings, rpc: chain.Rpc):
    """The guarded live executor from live/ (hard limits, kill switch, dry run, landing checks), or None.
    live/ also returns a sell-only executor while real positions are open after live was switched off."""
    try:
        from live import make_executor as make_live  # type: ignore

        return make_live(settings, rpc)
    except Exception as e:
        log.error("live executor unavailable: %s", e)
        return None


def make_executor(settings: Settings, rpc: chain.Rpc):
    return _live(settings, rpc) or PaperExecutor(settings, rpc)


def make_executors(settings: Settings, rpc: chain.Rpc) -> dict:
    """Paper always; live only when .env has LIVE_TRADING=true and a key (the master switch).
    Which strategy actually trades live is chosen per strategy in control (ct_control live:<strategy>).
    While LIVE_DRY_RUN is on (default) the live executor has mode 'dry': everything up to the signature,
    nothing sent, positions booked as mode 'dry' (live/README.md)."""
    exs: dict = {"paper": PaperExecutor(settings, rpc), "live": None}
    ex = _live(settings, rpc)
    if ex is not None:
        if not (ex.mode == "dry" and getattr(ex, "exit_only", "")):  # dry leftovers only: new buys stay paper
            exs["live"] = ex
        exs[ex.mode] = ex  # 'dry' positions find their executor for the sell
        if getattr(ex, "dry_seller", None) is not None:
            exs["dry"] = ex.dry_seller  # never sell a dry position with the real wallet
    return exs
