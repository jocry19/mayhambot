"""Seed candidate winner wallets from public, keyless sources. Every source is optional: if a site blocks
automated requests (403/Cloudflare) it is skipped and reported, never worked around.

  gmgn_rank      GMGN public wallet leaderboard (7d / 30d PnL)
  pumpfun_coins  pump.fun coins with the highest market cap -> their earliest buyers via RPC (core early_buyers)
Wallets go to data/seed_wallets.txt with provenance; coins found go through early-buyer discovery right away.
"""
import logging
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
