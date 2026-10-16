"""Network sources of the sniper (no keys): PumpPortal websocket, DexScreener prices, GeckoTerminal new
pools, Jupiter quotes for paper fills. HTTP goes through core.http (shared limits, 429 back-off)."""
import json
import logging
import time

from core import http
from core.models import QUOTE_MINTS
from sniper.state import PUMP_DECIMALS, PUMP_SUPPLY

log = logging.getLogger("sniper.feed")
SOL_MINT = "So11111111111111111111111111111111111111112"
DEXSCREENER = "https://api.dexscreener.com"
GECKO = "https://api.geckoterminal.com/api/v2"


# ---------------------------------------------------------------- PumpPortal (one connection, free data)
def sub_msgs(subscribe, unsubscribe):
    """PumpPortal wants every subscription on the one open connection; keys in chunks."""
    out = []
    for method, keys in (("subscribeTokenTrade", sorted(subscribe)), ("unsubscribeTokenTrade", sorted(unsubscribe))):
        for i in range(0, len(keys), 100):
            out.append(json.dumps({"method": method, "keys": keys[i:i + 100]}))
    return out


def parse(raw):
    try:
        m = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(m, dict) or not m.get("mint"):
        return None  # subscription acks ({"message": ...}) and errors
    if not m.get("txType") and (m.get("pool") in ("pump-amm", "raydium") or "migrat" in json.dumps(m)[:200].lower()):
        m["txType"] = "migrate"
    return m


# ---------------------------------------------------------------- DexScreener: mcap in SOL for many mints
def dex_prices(mints, sol_usd):
    """{mint: mcap_sol} for up to 30 mints per call; mints without a pair are left out."""
    out = {}
    for i in range(0, len(mints), 30):
        chunk = mints[i:i + 30]
        try:
            pairs = http.get_json(f"{DEXSCREENER}/tokens/v1/solana/{','.join(chunk)}", retries=1)
        except Exception as e:  # noqa: BLE001
            log.debug("dexscreener batch failed: %s", e)
            continue
        if isinstance(pairs, dict):
            pairs = pairs.get("pairs") or []
        best = {}
        for p in pairs or []:
            m = (p.get("baseToken") or {}).get("address")
            if m in chunk and ((p.get("liquidity") or {}).get("usd") or 0) >= (best.get(m, {}).get("liq") or 0):
                quote = (p.get("quoteToken") or {}).get("address")
                mcap = None
                # fdv first, the same measure GeckoTerminal's new pools give as base (fdv_usd): mixing fdv with a
                # circulating market cap produced returns of up to 286,700x
                if sol_usd and (p.get("fdv") or p.get("marketCap")):
                    mcap = float(p.get("fdv") or p.get("marketCap")) / sol_usd
                elif quote == SOL_MINT and p.get("priceNative"):
                    mcap = float(p["priceNative"]) * PUMP_SUPPLY
                if mcap:
                    best[m] = {"liq": (p.get("liquidity") or {}).get("usd") or 0, "mcap": mcap}
        out.update({m: v["mcap"] for m, v in best.items()})
    return out


# ---------------------------------------------------------------- GeckoTerminal: new pools outside pump.fun
SKIP_DEX = {"pump-fun", "pumpswap", "pump-swap"}


def new_pools(sol_usd):
    try:
        d = http.get_json(f"{GECKO}/networks/solana/new_pools", params={"page": 1}, retries=1)
    except Exception as e:  # noqa: BLE001
        log.debug("geckoterminal new_pools failed: %s", e)
        return []
    out = []
    for p in (d or {}).get("data") or []:
        at, rel = p.get("attributes") or {}, p.get("relationships") or {}
        dex = ((rel.get("dex") or {}).get("data") or {}).get("id") or ""
        base = ((rel.get("base_token") or {}).get("data") or {}).get("id") or ""
        mint = base.split("_", 1)[1] if "_" in base else base
        if not mint or dex in SKIP_DEX or mint in QUOTE_MINTS:
            continue   # stablecoins and SOL appear as the base token of some pools; they are no launches
        created = at.get("pool_created_at")
        try:
            from datetime import datetime
            created = int(datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()) if created else None
        except (ValueError, AttributeError):
            created = None
        m5 = ((at.get("transactions") or {}).get("m5") or {})
        vol = float(((at.get("volume_usd") or {}).get("m5")) or 0)
        mcap_usd = float(at.get("fdv_usd") or at.get("market_cap_usd") or 0)
        buyers = int(m5.get("buyers") or 0)
        momentum = min(100.0, 40 * min(1.0, buyers / 40) + 40 * min(1.0, vol / 20000)
                       + 20 * (m5.get("buys", 0) / max(1, m5.get("buys", 0) + m5.get("sells", 0))))
        name = at.get("name") or ""
        out.append({"mint": mint, "created_ts": created, "symbol": name.split(" / ")[0], "name": name, "pool": dex,
                    "mcap_sol": mcap_usd / sol_usd if sol_usd else 0.0,
                    "info": {"momentum": round(momentum, 1), "buyers_m5": buyers, "volume_m5": vol,
                             "reserve_usd": float(at.get("reserve_in_usd") or 0)}})
    return out


# ---------------------------------------------------------------- Jupiter quote (paper fills)
_last_jup = [0.0]


def make_quote_fn(cfg):
    if not cfg.use_jupiter:
        return None
    headers = {"x-api-key": cfg.jupiter_api_key} if cfg.jupiter_api_key else None

    def quote(mint, amount, side):
        wait = _last_jup[0] + 1.1 - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_jup[0] = time.monotonic()
        if side == "buy":
            inp, out, raw, out_dec = SOL_MINT, mint, int(amount * 1e9), PUMP_DECIMALS
        else:
            inp, out, raw, out_dec = mint, SOL_MINT, int(amount * 10 ** PUMP_DECIMALS), 9
        try:
            q = http.get_json(f"{cfg.jupiter_url}/swap/v1/quote", headers=headers, retries=1, timeout=8, params={
                "inputMint": inp, "outputMint": out, "amount": raw, "slippageBps": cfg.slippage_bps})
        except Exception as e:  # noqa: BLE001 - no route on a fresh curve is normal: curve math takes over
            log.debug("jupiter quote %s %s: %s", side, mint[:6], e)
            return None
        if not q or not int(q.get("outAmount") or 0):
            return None
        return {"out": int(q["outAmount"]) / 10 ** out_dec, "impact": float(q.get("priceImpactPct") or 0)}
    return quote


def sol_usd():
    try:
        from core.sources import market
        return float(market.sol_price_usd() or 0)
    except Exception as e:  # noqa: BLE001
        log.debug("sol price failed: %s", e)
        return 0.0
