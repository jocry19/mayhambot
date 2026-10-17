"""Market data without API keys: DexScreener (live pairs), GeckoTerminal (minute
candles), Jupiter (quotes and SOL price). Birdeye is used for candles when
BIRDEYE_API_KEY is set, because it covers more young tokens.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Iterable

from . import http
from .config import SOL_MINT, USDC_MINT, Settings

log = logging.getLogger("copytrade.market")

DS = "https://api.dexscreener.com"
GT = "https://api.geckoterminal.com/api/v2"


@dataclass
class PairSnapshot:
    mint: str
    symbol: str
    pair_address: str
    dex_id: str
    price_usd: float
    price_sol: float
    liquidity_usd: float
    fdv: float
    volume_m5: float
    volume_h1: float
    buys_m5: int
    sells_m5: int
    buys_h1: int
    change_m5: float
    change_h1: float
    pair_created_ms: int
    ts: float
    curve_liq_usd: float = 0.0  # pump.fun bonding curve depth (DexScreener gives curves no liquidity_usd)

    @property
    def depth_usd(self) -> float:
        """Liquidity a trade meets: the pool's, else the bonding curve's."""
        return self.liquidity_usd or self.curve_liq_usd

    @property
    def age_hours(self) -> float:
        if not self.pair_created_ms:
            return 1e9
        return (time.time() * 1000 - self.pair_created_ms) / 3.6e6


_sol_cache: tuple[float, float] = (0.0, 0.0)


def sol_usd(settings: Settings | None = None) -> float:
    """SOL price in USD, cached 30 s. Jupiter first, DexScreener as fallback."""
    global _sol_cache
    if time.time() - _sol_cache[1] < 30 and _sol_cache[0] > 0:
        return _sol_cache[0]
    price = 0.0
    try:
        base = settings.jupiter_url if settings else "https://lite-api.jup.ag"
        d = http.get_json(f"{base}/price/v3?ids={SOL_MINT}", limiter_name="jupiter", headers=_jup_headers(settings))
        price = float(d[SOL_MINT]["usdPrice"])
    except Exception as e:
        log.debug("jupiter price failed: %s", e)
    if price <= 0:
        pairs = http.get_json(f"{DS}/tokens/v1/solana/{SOL_MINT}", limiter_name="dexscreener")
        best = max(
            (p for p in pairs if p.get("quoteToken", {}).get("address") == USDC_MINT),
            key=lambda p: (p.get("liquidity") or {}).get("usd") or 0,
            default=None,
        )
        price = float(best["priceUsd"]) if best else 0.0
    if price > 0:
        _sol_cache = (price, time.time())
    return price or _sol_cache[0]


def _jup_headers(settings: Settings | None) -> dict:
    if settings and settings.jupiter_api_key:
        return {"x-api-key": settings.jupiter_api_key}
    return {}


PUMP_CURVE_K = 30 * 1.073e9  # pump.fun curve: virtual SOL x virtual tokens (whole units) at launch


def curve_liquidity_usd(dex_id: str, price_sol: float, solusd: float) -> float:
    """Depth of a pump.fun bonding curve from its price: the curve is constant product on virtual reserves, so
    v_sol = sqrt(price x k) and a trade meets 2 x v_sol. DexScreener reports no liquidity for curve pairs (17.10.:
    5209 of 5209 pumpfun tokens at $0), which read as 'Liquidität zu klein ($0)'."""
    if dex_id != "pumpfun" or price_sol <= 0 or solusd <= 0:
        return 0.0
    return 2 * (price_sol * PUMP_CURVE_K) ** 0.5 * solusd


def _snapshot(p: dict, solusd: float) -> PairSnapshot:
    tx = p.get("txns") or {}
    vol = p.get("volume") or {}
    ch = p.get("priceChange") or {}
    price_usd = float(p.get("priceUsd") or 0)
    if p.get("quoteToken", {}).get("address") == SOL_MINT:
        price_sol = float(p.get("priceNative") or 0)
    else:
        price_sol = price_usd / solusd if solusd else 0.0
    return PairSnapshot(
        mint=p["baseToken"]["address"],
        symbol=p["baseToken"].get("symbol", "?"),
        pair_address=p.get("pairAddress", ""),
        dex_id=p.get("dexId", ""),
        price_usd=price_usd,
        price_sol=price_sol,
        liquidity_usd=float((p.get("liquidity") or {}).get("usd") or 0),
        fdv=float(p.get("fdv") or 0),
        volume_m5=float(vol.get("m5") or 0),
        volume_h1=float(vol.get("h1") or 0),
        buys_m5=int((tx.get("m5") or {}).get("buys") or 0),
        sells_m5=int((tx.get("m5") or {}).get("sells") or 0),
        buys_h1=int((tx.get("h1") or {}).get("buys") or 0),
        change_m5=float(ch.get("m5") or 0),
        change_h1=float(ch.get("h1") or 0),
        pair_created_ms=int(p.get("pairCreatedAt") or 0),
        ts=time.time(),
        curve_liq_usd=curve_liquidity_usd(p.get("dexId", ""), price_sol, solusd),
    )


def snapshots(mints: Iterable[str], settings: Settings | None = None) -> dict[str, PairSnapshot]:
    """Best (most liquid) Solana pair per token mint, batched 30 per call."""
    return snapshots_checked(mints, settings)[0]


def snapshots_checked(mints: Iterable[str], settings: Settings | None = None
                      ) -> tuple[dict[str, PairSnapshot], set[str]]:
    """(snapshots, mints whose batch failed). A failed batch says nothing about the coin, a missing pair does."""
    mints = [m for m in dict.fromkeys(mints) if m]
    out: dict[str, PairSnapshot] = {}
    failed: set[str] = set()
    if not mints:
        return out, failed
    solusd = sol_usd(settings)
    for i in range(0, len(mints), 30):
        chunk = mints[i : i + 30]
        try:
            pairs = http.get_json(f"{DS}/tokens/v1/solana/{','.join(chunk)}", limiter_name="dexscreener")
        except Exception as e:
            log.warning("dexscreener batch failed: %s", e)
            failed.update(chunk)
            continue
        for p in pairs or []:
            mint = p.get("baseToken", {}).get("address")
            if mint not in chunk:
                continue
            liq = (p.get("liquidity") or {}).get("usd") or 0
            if mint not in out or liq > out[mint].liquidity_usd:
                out[mint] = _snapshot(p, solusd)
    return out, failed


def snapshot(mint: str, settings: Settings | None = None) -> PairSnapshot | None:
    return snapshots([mint], settings).get(mint)


def trending_solana_tokens(limit: int = 40) -> list[str]:
    """Token mints that are hot right now (boosts + new profiles + GeckoTerminal trending)."""
    mints: list[str] = []
    for path in ("/token-boosts/top/v1", "/token-boosts/latest/v1", "/token-profiles/latest/v1"):
        try:
            for t in http.get_json(DS + path, limiter_name="dexscreener") or []:
                if t.get("chainId") == "solana":
                    mints.append(t["tokenAddress"])
        except Exception as e:
            log.warning("dexscreener %s failed: %s", path, e)
    try:
        d = http.get_json(f"{GT}/networks/solana/trending_pools?page=1", limiter_name="geckoterminal",
                          headers={"Accept": "application/json"})
        for pool in d.get("data", []):
            base = pool["relationships"]["base_token"]["data"]["id"]  # "solana_<mint>"
            mints.append(base.split("_", 1)[1])
    except Exception as e:
        log.warning("geckoterminal trending failed: %s", e)
    return [m for m in dict.fromkeys(mints) if m != SOL_MINT][:limit]


# ---------------------------------------------------------------------------
# candles


def minute_candles(pool: str, start_ts: int, end_ts: int, settings: Settings | None = None,
                   mint: str | None = None) -> list[tuple[int, float, float, float, float, float]]:
    """1-minute OHLCV (ts, o, h, l, c, volume_usd) ascending, prices in USD.

    Birdeye when a key is set (needs the mint), else GeckoTerminal (needs the pool).
    """
    if settings and settings.birdeye_api_key and mint:
        try:
            d = http.get_json(
                "https://public-api.birdeye.so/defi/ohlcv",
                params={"address": mint, "type": "1m", "time_from": start_ts, "time_to": end_ts},
                headers={"X-API-KEY": settings.birdeye_api_key, "x-chain": "solana"},
                limiter_name="birdeye",
            )
            items = d.get("data", {}).get("items", [])
            if items:
                return [(int(i["unixTime"]), i["o"], i["h"], i["l"], i["c"], i.get("v", 0) * i["c"]) for i in items]
        except Exception as e:
            log.debug("birdeye candles failed: %s", e)
    if not pool:
        return []
    rows: list = []
    before = end_ts
    while before > start_ts:
        limit = min(1000, int((before - start_ts) / 60) + 2)
        d = http.get_json(
            f"{GT}/networks/solana/pools/{pool}/ohlcv/minute",
            params={"aggregate": 1, "before_timestamp": before, "limit": limit, "currency": "usd", "token": "base"},
            headers={"Accept": "application/json"},
            limiter_name="geckoterminal",
        )
        chunk = d.get("data", {}).get("attributes", {}).get("ohlcv_list", []) or []
        if not chunk:
            break
        rows.extend(chunk)
        oldest = min(int(c[0]) for c in chunk)
        if oldest >= before or len(chunk) < limit:
            break
        before = oldest
    uniq = {int(c[0]): c for c in rows if start_ts <= int(c[0]) <= end_ts}
    return [(t, float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5])) for t, c in sorted(uniq.items())]


def best_pool(mint: str) -> str:
    """Most liquid Solana pool address for a mint (DexScreener)."""
    try:
        pairs = http.get_json(f"{DS}/token-pairs/v1/solana/{mint}", limiter_name="dexscreener") or []
        pairs = [p for p in pairs if p.get("baseToken", {}).get("address") == mint]
        if pairs:
            return max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)["pairAddress"]
    except Exception as e:
        log.debug("best_pool failed: %s", e)
    return ""


# ---------------------------------------------------------------------------
# Jupiter


def jupiter_quote(settings: Settings, input_mint: str, output_mint: str, amount_raw: int,
                  slippage_bps: int | None = None) -> dict | None:
    return jupiter_quote_status(settings, input_mint, output_mint, amount_raw, slippage_bps)[0]


def _transient(e: Exception) -> bool:
    """True for a network error, timeout, 429 or 5xx (try again), False for Jupiter's own 4xx answer (no route,
    token not tradable): only that one may count as 'the tokens sell for nothing'."""
    txt = str(e)
    return not ("Client Error" in txt and "429" not in txt)


def jupiter_quote_status(settings: Settings, input_mint: str, output_mint: str, amount_raw: int,
                         slippage_bps: int | None = None) -> tuple[dict | None, bool]:
    """(quote or None, transient): transient=True when the quote failed for a reason that says nothing about the
    token (network down, rate limit, Jupiter 5xx)."""
    try:
        return http.get_json(
            f"{settings.jupiter_url}/swap/v1/quote",
            params={
                "inputMint": input_mint,
                "outputMint": output_mint,
                "amount": int(amount_raw),
                "slippageBps": slippage_bps if slippage_bps is not None else settings.slippage_bps,
            },
            headers=_jup_headers(settings),
            limiter_name="jupiter",
            retries=2,
        ), False
    except Exception as e:
        log.debug("jupiter quote failed: %s", e)
        return None, _transient(e)


def jupiter_swap_tx(settings: Settings, quote: dict, user_pubkey: str) -> str:
    d = http.post_json(
        f"{settings.jupiter_url}/swap/v1/swap",
        {
            "quoteResponse": quote,
            "userPublicKey": user_pubkey,
            "wrapAndUnwrapSol": True,
            "dynamicComputeUnitLimit": True,
            "dynamicSlippage": True,
            "prioritizationFeeLamports": {
                "priorityLevelWithMaxLamports": {
                    "maxLamports": int(settings.priority_fee_sol * 1e9 * 4),
                    "priorityLevel": "veryHigh",
                }
            },
        },
        headers=_jup_headers(settings),
        limiter_name="jupiter",
        retries=2,
    )
    return d["swapTransaction"]
