"""Keyless market data through core.http (own budget per service, shared rate limits):
DexScreener (pairs with buys/sells and socials, boosts, profiles) and CoinGecko public (SOL history,
Solana meme sector market cap). Zero Helius credits, no GeckoTerminal calls (that host is the scarcest one)."""
import logging
import time

from core import http
from core.models import QUOTE_MINTS

log = logging.getLogger("market")

DS = "https://api.dexscreener.com"
CG = "https://api.coingecko.com/api/v3"
MEME_CATEGORIES = ("solana-meme-coins", "pump-fun-ecosystem", "meme-token")


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def epoch_s(x):
    """Unix seconds from a timestamp in seconds or milliseconds (DexScreener's pairCreatedAt is documented as ms,
    a seconds value divided by 1000 again would land in January 1970). None when missing."""
    v = _f(x)
    if v <= 0:
        return None
    return int(v / 1000) if v > 1e11 else int(v)


def _get(url, params=None):
    try:
        return http.get_json(url, params=params)
    except Exception as e:  # noqa: BLE001 - one failed source must not stop the round
        log.warning("GET %s failed: %s", url.split("?")[0][:80], e)
        return None


# ---------------------------------------------------------------- DexScreener
def parse_pair(p):
    """One DexScreener pair -> flat market row (None for non-Solana pairs and quote tokens)."""
    if not p or p.get("chainId") != "solana":
        return None
    base = p.get("baseToken") or {}
    mint = base.get("address") or ""
    if not mint or mint in QUOTE_MINTS:
        return None
    tx = p.get("txns") or {}
    vol = p.get("volume") or {}
    pc = p.get("priceChange") or {}
    info = p.get("info") or {}
    socials = [s for s in (info.get("socials") or []) if s] + [w for w in (info.get("websites") or []) if w]
    return {
        "mint": mint, "symbol": base.get("symbol") or "", "name": base.get("name") or "", "dex": p.get("dexId") or "",
        "pool": p.get("pairAddress") or "", "price_usd": _f(p.get("priceUsd")),
        "mcap_usd": _f(p.get("marketCap") or p.get("fdv")), "liquidity_usd": _f((p.get("liquidity") or {}).get("usd")),
        "pair_created_at": epoch_s(p.get("pairCreatedAt")),
        "price_change_h1": _f(pc.get("h1")), "price_change_h6": _f(pc.get("h6")), "price_change_h24": _f(pc.get("h24")),
        "volume_h1": _f(vol.get("h1")), "volume_h6": _f(vol.get("h6")), "volume_h24": _f(vol.get("h24")),
        "buys_h1": int(_f((tx.get("h1") or {}).get("buys"))), "sells_h1": int(_f((tx.get("h1") or {}).get("sells"))),
        "buys_h24": int(_f((tx.get("h24") or {}).get("buys"))), "sells_h24": int(_f((tx.get("h24") or {}).get("sells"))),
        "n_socials": len(socials), "boosted": _f((p.get("boosts") or {}).get("active")),
    }


def best_pairs(pairs, wanted=None):
    """Highest-liquidity pair per mint."""
    out = {}
    for p in pairs or []:
        r = parse_pair(p)
        if not r or (wanted is not None and r["mint"] not in wanted):
            continue
        if r["mint"] not in out or r["liquidity_usd"] > out[r["mint"]]["liquidity_usd"]:
            out[r["mint"]] = r
    return out


def pairs_for(mints):
    """Market rows for up to len(mints) tokens, 30 per call (DexScreener tokens/v1)."""
    mints = [m for m in dict.fromkeys(mints) if m]
    out, calls = {}, 0
    for i in range(0, len(mints), 30):
        chunk = mints[i:i + 30]
        calls += 1
        out.update(best_pairs(_get(f"{DS}/tokens/v1/solana/{','.join(chunk)}"), set(chunk)))
    return out, calls


def parse_promoted(items, kind):
    """token-boosts / token-profiles list -> {mint: {description, n_socials, boosted, source}} (Solana only)."""
    out = {}
    for it in items or []:
        if not isinstance(it, dict) or it.get("chainId") != "solana" or not it.get("tokenAddress"):
            continue
        m = it["tokenAddress"]
        d = out.setdefault(m, {"mint": m, "description": "", "n_socials": 0, "boosted": 0.0, "source": kind})
        if it.get("description"):
            d["description"] = str(it["description"])[:500]
        d["n_socials"] = max(d["n_socials"], len(it.get("links") or []))
        d["boosted"] = max(d["boosted"], _f(it.get("totalAmount") or it.get("amount")))
    return out


def promoted():
    """Boosted and freshly profiled Solana tokens (3 calls): what teams are paying to promote right now."""
    out = {}
    for path, kind in (("/token-profiles/latest/v1", "profile"), ("/token-boosts/latest/v1", "boost"),
                       ("/token-boosts/top/v1", "boost_top")):
        for m, d in parse_promoted(_get(DS + path), kind).items():
            o = out.setdefault(m, d)
            if o is not d:
                o["description"] = o["description"] or d["description"]
                o["n_socials"] = max(o["n_socials"], d["n_socials"])
                o["boosted"] = max(o["boosted"], d["boosted"])
    return out, 3


# ---------------------------------------------------------------- CoinGecko (public, no key)
def parse_market_chart(data):
    """{"prices": [[ms, price], ...]} -> [(ts, price)], one point per hour at most."""
    seen = {}
    for p in (data or {}).get("prices") or []:
        try:
            ts, v = int(p[0] / 1000), float(p[1])
        except (TypeError, ValueError, IndexError):
            continue
        if v > 0:
            seen[ts - ts % 3600] = (ts, v)
    return [seen[k] for k in sorted(seen)]


def sol_history(days=7):
    return parse_market_chart(_get(f"{CG}/coins/solana/market_chart", {"vs_currency": "usd", "days": days}))


def parse_categories(data):
    """CoinGecko categories -> the Solana meme sector {id, market_cap, change_24h, volume_24h} or None."""
    by_id = {c.get("id"): c for c in data or [] if isinstance(c, dict)}
    for cid in MEME_CATEGORIES:
        c = by_id.get(cid)
        if c and c.get("market_cap"):
            return {"id": cid, "market_cap": _f(c.get("market_cap")),
                    "change_24h": _f(c.get("market_cap_change_24h")), "volume_24h": _f(c.get("volume_24h"))}
    return None


def meme_sector():
    return parse_categories(_get(f"{CG}/coins/categories"))


def sol_now():
    """Live SOL price from core (DexScreener / Jupiter, cached there)."""
    try:
        from core.sources import market as cm
        return cm.sol_price_usd() or None
    except Exception as e:  # noqa: BLE001
        log.warning("SOL price failed: %s", e)
        return None


def apply_budgets(cfg):
    """This service's own per-minute budgets, far below the hosts' limits (signals/sniper need DexScreener more)."""
    http.set_budget("api.dexscreener.com", cfg.dexscreener_per_min)
    http.set_budget("api.coingecko.com", cfg.coingecko_per_min)
    http.set_budget("geckoterminal", 1)
    http.set_budget("helius", 1)  # never needed; a stray call would be capped hard


def now():
    return int(time.time())
