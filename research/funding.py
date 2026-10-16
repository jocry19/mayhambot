"""Funding source of a wallet: the address that first sent it SOL.

Wallets funded by the same address are very likely one person (or one group) splitting money over many
wallets. The model then counts them as one smart-money group instead of several independent ones.
Funders that funded many of our wallets are exchanges or hubs and are ignored (see hubs()).

Cost per wallet: up to RESEARCH_FUNDING_MAX_SIG_PAGES getSignaturesForAddress pages plus up to 4
getTransaction calls, all through core's rate-limited RPC client. Wallets whose history is longer than
the page budget get status 'deep' and are retried after a week.
"""
import logging
import time
from collections import Counter

from core.sources.wallet_history import _get_tx, _rpc
from research.config import rsettings
from research.store import retry

log = logging.getLogger("mayhambot.research.funding")
MIN_LAMPORTS = 10_000_000  # 0.01 SOL: smaller transfers are spam/dust, not funding
SYSTEM = "11111111111111111111111111111111"


def _instructions(tx):
    msg = tx["transaction"]["message"]
    yield from msg.get("instructions") or []
    for inner in (tx.get("meta") or {}).get("innerInstructions") or []:
        yield from inner.get("instructions") or []


def funder_in_tx(tx, wallet):
    """(funder, lamports) of the first SOL transfer into `wallet` in a jsonParsed transaction, else None."""
    if not tx or not tx.get("meta") or tx["meta"].get("err"):
        return None
    for ins in _instructions(tx):
        if ins.get("program") != "system" and ins.get("programId") != SYSTEM:
            continue
        p = ins.get("parsed")
        if not isinstance(p, dict):
            continue
        info = p.get("info") or {}
        typ = p.get("type")
        if typ in ("transfer", "transferWithSeed"):
            dst, lam = info.get("destination"), int(info.get("lamports") or 0)
        elif typ in ("createAccount", "createAccountWithSeed"):
            dst, lam = info.get("newAccount"), int(info.get("lamports") or 0)
        else:
            continue
        src = info.get("source")
        if dst == wallet and src and src != wallet and lam >= MIN_LAMPORTS:
            return src, lam
    return None


def find_funder(wallet, max_pages=None):
    """-> dict(funder, ts, sol, status). status: found | none (no SOL inflow in the first txs) | deep."""
    max_pages = max_pages or rsettings.funding_max_sig_pages
    before, page = None, []
    for _ in range(max_pages):
        opts = {"limit": 1000}
        if before:
            opts["before"] = before
        page = _rpc("getSignaturesForAddress", [wallet, opts]) or []
        if len(page) < 1000:
            break
        before = page[-1]["signature"]
    else:
        return dict(funder=None, ts=None, sol=None, status="deep")
    for s in list(reversed(page))[:4]:
        try:
            tx = _get_tx(s["signature"])
        except Exception as e:  # noqa: BLE001
            log.debug("funding tx %s failed: %s", s["signature"][:8], e)
            continue
        hit = funder_in_tx(tx, wallet)
        if hit:
            return dict(funder=hit[0], ts=int(tx.get("blockTime") or s.get("blockTime") or 0), sol=hit[1] / 1e9,
                        status="found")
    return dict(funder=None, ts=None, sol=None, status="none")


def due_wallets(conn, n, mine=lambda w: True):
    """Tiered / high-score wallets first, then research wallets by priority; never-checked before stale 'deep'."""
    now = int(time.time())
    done = {r[0] for r in conn.execute("SELECT wallet FROM research_funding WHERE status != 'deep' OR checked_at > ?",
                                       (now - 7 * 86400,))}
    order = [r[0] for r in conn.execute(
        "SELECT address FROM model_wallet_stats ORDER BY (tier IS NOT NULL) DESC, score DESC LIMIT 400")]
    order += [r[0] for r in conn.execute(
        "SELECT address FROM research_wallets WHERE backfilled_at > 0 ORDER BY priority DESC LIMIT 400")]
    out, seen = [], set()
    for w in order:
        if w in done or w in seen or not mine(w):
            continue
        seen.add(w)
        out.append(w)
        if len(out) >= n:
            break
    return out


def run_once(conn, n=None, mine=lambda w: True):
    n = rsettings.funding_per_run if n is None else n
    found = 0
    for w in due_wallets(conn, n, mine):
        try:
            r = find_funder(w)
        except Exception as e:  # noqa: BLE001
            log.warning("funding %s failed: %s", w[:6], e)
            continue
        retry(lambda: (conn.execute(
            "INSERT OR REPLACE INTO research_funding(wallet, funder, ts, sol, status, checked_at) VALUES (?,?,?,?,?,?)",
            (w, r["funder"], r["ts"], r["sol"], r["status"], int(time.time()))), conn.commit()), conn=conn)
        found += r["status"] == "found"
        time.sleep(rsettings.pause_s)
    return found


def hubs(funding, min_funded=None):
    """Funders that funded at least `min_funded` wallets: exchanges, bridges, faucets. Not a person."""
    min_funded = min_funded or rsettings.funding_hub_min
    c = Counter(f for f, _ in funding.values())
    return {f for f, k in c.items() if k >= min_funded}
