"""Seed candidate winner wallets from public, keyless sources. Every source is optional: if a site blocks
automated requests (403/Cloudflare) it is skipped and reported, never worked around.

  github_lists   open-source wallet lists on GitHub (kol-quest: GMGN smart money + KOL wallets, KolScan
                 leaderboard; more via RESEARCH_SEED_URLS)
  gmgn_openapi   GMGN official OpenAPI (smart money, KOL, top traders of fresh pumps), only with GMGN_API_KEY
  gmgn_rank      GMGN public wallet leaderboard (7d / 30d PnL)
  pumpfun_coins  pump.fun coins with the highest market cap -> their earliest buyers via RPC (core early_buyers)
Wallets go to data/seed_wallets.txt with provenance; coins found go through early-buyer discovery right away.
"""
import logging
import os
import time
from pathlib import Path

from core import db
from core.http import get_json
from core.sources import wallet_source
from research.config import rsettings
from research.store import add_research_wallets, retry, upsert_research_token

log = logging.getLogger("research.public_seeds")
SEED_OUT = Path(__file__).resolve().parent.parent / "data" / "seed_wallets.txt"
BROWSER_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                                 "(KHTML, like Gecko) Chrome/126.0 Safari/537.36", "Accept": "application/json"}


def _append_seeds(rows):
    """rows: (address, label, source). Appends new addresses to data/seed_wallets.txt."""
    SEED_OUT.parent.mkdir(parents=True, exist_ok=True)
    have = set()
    if SEED_OUT.exists():
        have = {ln.split()[0] for ln in SEED_OUT.read_text().splitlines() if ln.strip() and not ln.startswith("#")}
    new = [(a, lab, src) for a, lab, src in rows if a not in have]
    with SEED_OUT.open("a") as f:
        for a, lab, src in new:
            f.write(f"{a} {lab.replace(' ', '_')[:30]} # {src}\n")
            have.add(a)
    return len(new)


# Public open-source wallet lists (raw GitHub files). More can be added with RESEARCH_SEED_URLS (comma separated).
GITHUB_LISTS = [
    ("https://raw.githubusercontent.com/nirholas/kol-quest/HEAD/solwallets.json", "kol-quest/gmgn"),
    ("https://raw.githubusercontent.com/nirholas/kol-quest/HEAD/output/kolscan-leaderboard.json", "kol-quest/kolscan"),
]
B58 = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")


def _is_sol_address(a):
    return isinstance(a, str) and 32 <= len(a) <= 44 and set(a) <= B58


def extract_wallets(obj, path=""):
    """Walk any JSON and yield (address, label, section) for objects carrying a Solana wallet address."""
    if isinstance(obj, dict):
        a = obj.get("wallet_address") or obj.get("wallet") or obj.get("address") or obj.get("owner")
        if _is_sol_address(a) and obj.get("chain", "sol") in ("sol", "solana", None):
            name = obj.get("name") or obj.get("twitter_username") or obj.get("twitter_name") or ""
            prof = obj.get("realized_profit_30d") or obj.get("realized_profit_7d") or obj.get("profit")
            try:
                lab = f"{name}_p{float(prof):.0f}" if prof is not None else str(name)
            except (TypeError, ValueError):
                lab = str(name)
            yield a, lab, path
            return
        for k, v in obj.items():
            if k in ("tokens", "walletHoldings", "interceptorRaw", "twitterUsers"):
                continue  # token lists / holdings, not wallets
            yield from extract_wallets(v, f"{path}/{k}" if path else k)
    elif isinstance(obj, list):
        for v in obj:
            yield from extract_wallets(v, path)


def github_lists(urls=None):
    urls = list(urls or GITHUB_LISTS)
    extra = os.getenv("RESEARCH_SEED_URLS", "")
    urls += [(u.strip(), "custom") for u in extra.split(",") if u.strip()]
    rows, status = [], {}
    day = time.strftime("%Y-%m-%d")
    for url, name in urls:
        try:
            j = get_json(url, headers=BROWSER_HEADERS, retries=1, timeout=60)
        except Exception as e:  # noqa: BLE001
            status[name] = f"skipped ({str(e)[:80]})"
            continue
        seen = set()
        for a, lab, sec in extract_wallets(j):
            if a in seen:
                continue
            seen.add(a)
            rows.append((a, lab or sec.split("/")[-1], f"{name}:{sec} {day}"))
        status[name] = f"{len(seen)} wallets"
    return rows, status


GMGN_API = "https://openapi.gmgn.ai"


def _gmgn_api(path, **query):
    """Read-only GMGN OpenAPI call (official, same routes as GMGN's gmgn-cli). Needs only GMGN_API_KEY from .env;
    the private key is never used (it is only needed for trading routes, which we do not call)."""
    import uuid
    key = os.getenv("GMGN_API_KEY", "")
    if not key:
        raise RuntimeError("no GMGN_API_KEY")
    time.sleep(1.4)  # free plan: leaky bucket 5/5, back-to-back calls get banned for 5 min
    q = dict(query, timestamp=int(time.time()), client_id=str(uuid.uuid4()))
    j = get_json(GMGN_API + path, params=q, headers={"X-APIKEY": key, "User-Agent": "mayhambot/1.0"}, retries=1)
    if isinstance(j, dict) and j.get("code") not in (0, None):
        raise RuntimeError(f"gmgn {path}: code {j.get('code')} {str(j.get('msg') or j.get('message'))[:60]}")
    return (j or {}).get("data", j)


def gmgn_openapi(pumped_mints=()):
    """Smart money + KOL wallets and the top traders of fresh pumps from GMGN's OpenAPI (needs GMGN_API_KEY)."""
    rows, status = [], {}
    if not os.getenv("GMGN_API_KEY"):
        return rows, {"gmgn_openapi": "skipped (no GMGN_API_KEY)"}
    day = time.strftime("%Y-%m-%d")
    for name, path in (("smartmoney", "/v1/user/smartmoney"), ("kol", "/v1/user/kol")):
        try:
            d = _gmgn_api(path, chain="sol", limit=200)
            ws = {a: lab for a, lab, _ in extract_wallets(d)}
            rows += [(a, lab or name, f"gmgn openapi {name} {day}") for a, lab in ws.items()]
            status[name] = f"{len(ws)} wallets"
        except Exception as e:  # noqa: BLE001
            status[name] = f"failed ({str(e)[:80]})"
    n = 0
    for mint in list(pumped_mints)[:10]:
        try:
            d = _gmgn_api("/v1/market/token_top_traders", chain="sol", address=mint, order_by="profit", limit=50)
        except Exception as e:  # noqa: BLE001
            status["top_traders"] = f"failed ({str(e)[:80]})"
            break
        ws = {a for a, _, _ in extract_wallets(d)} - {mint}
        rows += [(a, "top_trader", f"gmgn openapi top_traders {mint[:8]} {day}") for a in ws]
        n += len(ws)
    status.setdefault("top_traders", f"{n} wallets")
    return rows, status


def gmgn_rank(periods=("7d", "30d"), limit=100):
    rows, status = [], {}
    for per in periods:
        url = f"https://gmgn.ai/defi/quotation/v1/rank/sol/wallets/{per}"
        try:
            j = get_json(url, params={"orderby": f"pnl_{per}", "direction": "desc"}, headers=BROWSER_HEADERS, retries=1)
        except Exception as e:  # noqa: BLE001
            status[per] = f"skipped ({str(e)[:80]})"
            continue
        items = ((j or {}).get("data") or {}).get("rank") or []
        for it in items[:limit]:
            a = it.get("wallet_address") or it.get("address")
            if not a:
                continue
            pnl = it.get(f"realized_profit_{per}") or it.get(f"pnl_{per}") or 0
            wr = it.get(f"winrate_{per}") or it.get("winrate") or 0
            rows.append((a, f"gmgn{per}_pnl{float(pnl or 0):.0f}_wr{float(wr or 0):.2f}", f"gmgn rank {per} {time.strftime('%Y-%m-%d')}"))
        status[per] = f"{len(items)} wallets"
    return rows, status


def pumpfun_coins(limit=50):
    for url in ("https://frontend-api-v3.pump.fun/coins", "https://frontend-api.pump.fun/coins"):
        try:
            j = get_json(url, params={"offset": 0, "limit": limit, "sort": "market_cap", "order": "DESC",
                                      "includeNsfw": "false"}, headers=BROWSER_HEADERS, retries=1)
        except Exception as e:  # noqa: BLE001
            log.info("pump.fun %s: %s", url, str(e)[:80])
            continue
        coins = j if isinstance(j, list) else (j or {}).get("coins") or []
        out = [(c.get("mint"), c.get("symbol", ""), int((c.get("created_timestamp") or 0) / 1000)) for c in coins if c.get("mint")]
        if out:
            return out, f"{len(out)} coins from {url.split('//')[1].split('/')[0]}"
    return [], "skipped (not reachable)"


def run(conn, early_coins=10):
    summary = {}
    rows, st = github_lists()
    summary["github_lists"] = st
    add_research_wallets(conn, [a for a, _, _ in rows], "open_source_list", 60.0)
    summary["github_new_in_seed_file"] = _append_seeds(rows)
    fresh_pumps = [r[0] for r in conn.execute(
        "SELECT mint FROM research_tokens WHERE pumped=1 ORDER BY early_fetched_at DESC LIMIT 10")]
    rows, st = gmgn_openapi(fresh_pumps)
    summary["gmgn_openapi"] = st
    add_research_wallets(conn, [a for a, _, _ in rows], "gmgn_openapi", 85.0)
    summary["gmgn_openapi_new_in_seed_file"] = _append_seeds(rows)
    rows, st = gmgn_rank()
    summary["gmgn"] = st
    for a, lab, src in rows:
        add_research_wallets(conn, [a], "gmgn_rank", 90.0)
    summary["seed_file_new"] = _append_seeds(rows)
    coins, st = pumpfun_coins()
    summary["pumpfun"] = st
    now = int(time.time())
    done = {r[0] for r in conn.execute("SELECT mint FROM research_tokens WHERE early_fetched_at > 0")}
    fresh = [c for c in coins if c[0] not in done and (not c[2] or now - c[2] <= rsettings.max_token_age_days * 86400)]
    n_tr = n_c = 0
    for mint, sym, created in fresh[:early_coins]:
        time.sleep(rsettings.pause_s)
        try:
            early = wallet_source.early_buyers(mint, max_pages=rsettings.early_max_sig_pages,
                                               n_tx=rsettings.early_tx_per_token)
        except Exception as e:  # noqa: BLE001
            log.warning("early buyers %s failed: %s", mint[:6], e)
            continue
        n_tr += retry(lambda: db.insert_trades(conn, early), conn=conn)
        buyers = list(dict.fromkeys(t.wallet for t in early if t.side == "buy"))[:40]
        add_research_wallets(conn, buyers, "pumpfun_early", 30.0)
        n_c += len(buyers)
        upsert_research_token(conn, mint, early_fetched_at=now, created_at=created or None, source="pumpfun_top")
    summary.update(pumpfun_early_coins=min(len(fresh), early_coins), early_trades=n_tr, early_candidates=n_c)
    return summary
