"""Extra market scans for research that core does not offer: GeckoTerminal pool lists with price changes
(to find coins that already pumped). Uses core.http, so rate limits are shared with the rest of the app."""
import logging

from core.http import get_json
from core.models import QUOTE_MINTS
from core.sources.market import GT, GT_HEADERS, _f, _iso_ts

log = logging.getLogger("research.sources")

POOL_LISTS = [
    ("/networks/solana/trending_pools", {"duration": "5m"}),
    ("/networks/solana/trending_pools", {"duration": "1h"}),
    ("/networks/solana/trending_pools", {"duration": "6h"}),
    ("/networks/solana/trending_pools", {"duration": "24h"}),
    ("/networks/solana/new_pools", {"page": 1}),
    ("/networks/solana/new_pools", {"page": 2}),
    ("/networks/solana/pools", {"sort": "h24_volume_usd_desc", "page": 1}),
    ("/networks/solana/pools", {"sort": "h24_tx_count_desc", "page": 1}),
]


def _pool(p):
    a = p.get("attributes") or {}
    rel = p.get("relationships") or {}
    bid = ((rel.get("base_token") or {}).get("data") or {}).get("id", "")
    mint = bid[len("solana_"):] if bid.startswith("solana_") else ""
    pc = a.get("price_change_percentage") or {}
    price = _f(a.get("base_token_price_usd"))
    fdv = _f(a.get("fdv_usd"))
    return dict(mint=mint, pool=a.get("address", ""), name=a.get("name", ""), price=price,
                mcap=_f(a.get("market_cap_usd")) or fdv, fdv=fdv, liquidity=_f(a.get("reserve_in_usd")),
                created_at=_iso_ts(a.get("pool_created_at")), ch_m5=_f(pc.get("m5")), ch_h1=_f(pc.get("h1")),
                ch_h6=_f(pc.get("h6")), ch_h24=_f(pc.get("h24")), vol_h24=_f((a.get("volume_usd") or {}).get("h24")),
                supply=(fdv / price) if fdv and price else 0.0)


def scan_pools(max_calls=8):
    """Normalized Solana pools from trending/new/top lists (one GeckoTerminal call each), deduped per mint
    (highest liquidity pool wins)."""
    out = {}
    for path, params in POOL_LISTS[:max_calls]:
        try:
            j = get_json(f"{GT}{path}", params=dict(params, include="base_token"), headers=GT_HEADERS)
        except Exception as e:  # noqa: BLE001
            log.warning("geckoterminal %s failed: %s", path, e)
            continue
        for p in (j or {}).get("data") or []:
            d = _pool(p)
            if not d["mint"] or d["mint"] in QUOTE_MINTS:
                continue
            if d["mint"] not in out or d["liquidity"] > out[d["mint"]]["liquidity"]:
                out[d["mint"]] = d
    return list(out.values())
