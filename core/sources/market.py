"""Keyless market data: DexScreener (discovery, prices) + GeckoTerminal (discovery, pool trades)."""
import logging
import time
from datetime import datetime

from core.http import get_json
from core.models import QUOTE_MINTS, SOL_MINT, Token, Trade

log = logging.getLogger("mayhambot.market")

DS = "https://api.dexscreener.com"
GT = "https://api.geckoterminal.com/api/v2"
GT_HEADERS = {"Accept": "application/json;version=20230302"}

_sol_price = {"ts": 0, "v": 0.0}


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _iso_ts(s):
    if not s:
        return 0
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return 0


# ---------------- DexScreener ----------------

def _ds_pair_to_token(p) -> Token:
    base = p.get("baseToken") or {}
    pc = p.get("priceChange") or {}
    return Token(
        mint=base.get("address", ""),
        symbol=base.get("symbol", ""),
        name=base.get("name", ""),
        pool_address=p.get("pairAddress", ""),
        dex=p.get("dexId", ""),
        price_usd=_f(p.get("priceUsd")),
        mcap_usd=_f(p.get("marketCap") or p.get("fdv")),
        liquidity_usd=_f((p.get("liquidity") or {}).get("usd")),
        pair_created_at=int(_f(p.get("pairCreatedAt")) / 1000),
        price_change_m5=_f(pc.get("m5")),
        price_change_h1=_f(pc.get("h1")),
        price_change_h24=_f(pc.get("h24")),
        volume_h1=_f((p.get("volume") or {}).get("h1")),
        extra={"price_native": _f(p.get("priceNative")),
               "quote": (p.get("quoteToken") or {}).get("address", "")},
    )


def token_info(mints) -> list:
    """Best (highest-liquidity) Solana pair per mint, up to 30 mints per request."""
    out = {}
    mints = [m for m in dict.fromkeys(mints) if m]
    for i in range(0, len(mints), 30):
        chunk = mints[i:i + 30]
        try:
            pairs = get_json(f"{DS}/tokens/v1/solana/{','.join(chunk)}") or []
        except Exception as e:  # noqa: BLE001
            log.warning("dexscreener token_info failed: %s", e)
            continue
        for p in pairs:
            if p.get("chainId") != "solana":
                continue
            t = _ds_pair_to_token(p)
            if t.mint not in chunk or t.mint in QUOTE_MINTS:
                continue
            if t.mint not in out or t.liquidity_usd > out[t.mint].liquidity_usd:
                out[t.mint] = t
    return list(out.values())


def sol_price_usd() -> float:
    if time.time() - _sol_price["ts"] < 120 and _sol_price["v"]:
        return _sol_price["v"]
    try:
        pairs = get_json(f"{DS}/tokens/v1/solana/{SOL_MINT}") or []
        best = max((p for p in pairs if (p.get("quoteToken") or {}).get("symbol") in ("USDC", "USDT")
                    and (p.get("baseToken") or {}).get("address") == SOL_MINT),
                   key=lambda p: _f((p.get("liquidity") or {}).get("usd")), default=None)
        if best:
            _sol_price.update(ts=time.time(), v=_f(best.get("priceUsd")))
    except Exception as e:  # noqa: BLE001
        log.warning("sol price failed: %s", e)
    return _sol_price["v"]


def _ds_candidate_mints() -> list:
    mints = []
    for path in ("/token-profiles/latest/v1", "/token-boosts/latest/v1", "/token-boosts/top/v1"):
        try:
            for it in get_json(DS + path) or []:
                if it.get("chainId") == "solana" and it.get("tokenAddress"):
                    mints.append(it["tokenAddress"])
        except Exception as e:  # noqa: BLE001
            log.warning("dexscreener %s failed: %s", path, e)
    return mints


# ---------------- GeckoTerminal ----------------

def _gt_pools(path, pages=1):
    out = []
    for page in range(1, pages + 1):
        try:
            j = get_json(f"{GT}{path}", params={"page": page, "include": "base_token"}, headers=GT_HEADERS)
        except Exception as e:  # noqa: BLE001
            log.warning("geckoterminal %s failed: %s", path, e)
            break
        out.extend(j.get("data") or [])
    return out


def _gt_candidate_mints() -> list:
    mints = []
    for path in ("/networks/solana/trending_pools", "/networks/solana/new_pools"):
        for p in _gt_pools(path):
            bid = (((p.get("relationships") or {}).get("base_token") or {}).get("data") or {}).get("id", "")
            if bid.startswith("solana_"):
                mints.append(bid[len("solana_"):])
    return mints


def discover_tokens(limit=40, min_liquidity=0.0, max_mcap=0.0) -> list:
    """Fresh/trending Solana tokens from DexScreener and GeckoTerminal, enriched with DexScreener pair data."""
    mints = [m for m in dict.fromkeys(_ds_candidate_mints() + _gt_candidate_mints()) if m not in QUOTE_MINTS]
    toks = token_info(mints)
    toks = [t for t in toks if t.liquidity_usd >= min_liquidity and (not max_mcap or t.mcap_usd <= max_mcap)]
    # most active first
    toks.sort(key=lambda t: t.volume_h1, reverse=True)
    return toks[:limit]


def pool_trades(token: Token) -> list:
    """Latest swaps (up to 300, last 24h) of the token's pool. Wallet = transaction signer."""
    if not token.pool_address:
        return []
    j = get_json(f"{GT}/networks/solana/pools/{token.pool_address}/trades", headers=GT_HEADERS)
    sol_usd = sol_price_usd()
    out = []
    for d in j.get("data") or []:
        a = d.get("attributes") or {}
        kind = a.get("kind")
        wallet = a.get("tx_from_address")
        if kind not in ("buy", "sell") or not wallet:
            continue
        frm, to = a.get("from_token_address"), a.get("to_token_address")
        if kind == "buy" and to == token.mint:
            tok_amt, quote_mint, quote_amt = _f(a.get("to_token_amount")), frm, _f(a.get("from_token_amount"))
            price = _f(a.get("price_to_in_usd"))
        elif kind == "sell" and frm == token.mint:
            tok_amt, quote_mint, quote_amt = _f(a.get("from_token_amount")), to, _f(a.get("to_token_amount"))
            price = _f(a.get("price_from_in_usd"))
        else:
            continue
        usd = _f(a.get("volume_in_usd"))
        if quote_mint == SOL_MINT:
            sol = quote_amt
        else:
            sol = usd / sol_usd if sol_usd else 0.0
        out.append(Trade(sig=a.get("tx_hash", ""), wallet=wallet, mint=token.mint, side=kind,
                         ts=_iso_ts(a.get("block_timestamp")), token_amount=tok_amt, sol_amount=sol,
                         usd_amount=usd, price_usd=price or (usd / tok_amt if tok_amt else 0.0),
                         source="geckoterminal"))
    return out


def pool_ohlcv(token: Token, timeframe="minute", aggregate=15, limit=1000) -> list:
    """[(ts, open, high, low, close, volume_usd)] in USD for the token's pool (GeckoTerminal, up to 1000 candles)."""
    if not token.pool_address:
        return []
    j = get_json(f"{GT}/networks/solana/pools/{token.pool_address}/ohlcv/{timeframe}",
                 params={"aggregate": aggregate, "limit": limit, "currency": "usd", "token": "base"},
                 headers=GT_HEADERS)
    rows = (((j or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
    return [(int(r[0]), _f(r[1]), _f(r[2]), _f(r[3]), _f(r[4]), _f(r[5])) for r in rows if len(r) >= 6]
