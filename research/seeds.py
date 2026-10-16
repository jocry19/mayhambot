"""Seed the wallet registry with known winners instead of waiting for discovery.

Sources (provenance kept in research_wallets.discovered_via):
  seed_file    research/seed_wallets.txt and data/seed_wallets.txt: "<address> [label] [# source]" per line,
               e.g. wallets copied from GMGN / Birdeye / Cielo leaderboards (they have no free API)
  user         the user's own lists (manual wallets, copytrade/wallets.txt) -> handled in discovery
  birdeye_top  Birdeye top traders of recently pumped coins, only when BIRDEYE_API_KEY is set
Seeded wallets get high priority, so the next discovery runs backfill them first.
"""
import logging
import os
from pathlib import Path

from core.http import get_json
from research.store import add_research_wallets

log = logging.getLogger("research.seeds")
ROOT = Path(__file__).resolve().parent.parent
SEED_FILES = [ROOT / "research" / "seed_wallets.txt", ROOT / "data" / "seed_wallets.txt"]


def _valid(a):
    return 32 <= len(a) <= 44 and a.isalnum()


def from_files(conn):
    n = 0
    for f in SEED_FILES:
        if not f.exists():
            continue
        for line in f.read_text().splitlines():
            src = line.split("#", 1)[1].strip() if "#" in line else ""
            parts = line.split("#")[0].split()
            if not parts or not _valid(parts[0]):
                continue
            add_research_wallets(conn, [parts[0]], f"seed_file:{src or f.name}"[:60], 80.0)
            if len(parts) > 1:
                from core import wallets
                wallets.add_wallet(conn, parts[0], label=" ".join(parts[1:])[:40], source="seed")
            n += 1
    return n


def birdeye_top_traders(conn, mints, per_token=20):
    """Top traders (by volume, last 24 h) of the given coins from Birdeye. Needs BIRDEYE_API_KEY."""
    key = os.getenv("BIRDEYE_API_KEY", "")
    if not key:
        return 0
    n = 0
    for mint in mints:
        try:
            j = get_json("https://public-api.birdeye.so/defi/v2/tokens/top_traders",
                         params={"address": mint, "time_frame": "24h", "sort_type": "desc", "sort_by": "volume",
                                 "offset": 0, "limit": per_token},
                         headers={"X-API-KEY": key, "x-chain": "solana"})
        except Exception as e:  # noqa: BLE001
            log.warning("birdeye top traders %s failed: %s", mint[:6], e)
            continue
        items = ((j or {}).get("data") or {}).get("items") or []
        ws = [it.get("owner") or it.get("address") for it in items]
        ws = [w for w in ws if w and _valid(w)]
        add_research_wallets(conn, ws, "birdeye_top", 40.0)
        n += len(ws)
    return n
