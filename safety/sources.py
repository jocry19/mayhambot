"""Network access of the safety layer. Everything goes through core.http (shared per-host limits,
retries, key redaction). One Fetcher per assessment carries a call budget, so a single coin can never
eat the RPC capacity the live loops need. Tests swap in a fixture Fetcher with the same methods."""
import logging
import time
from collections import defaultdict
from urllib.parse import urlparse

from core import http
from core.config import settings as core_settings
from safety import budget

log = logging.getLogger("mayhambot.safety")

DEXSCREENER = "https://api.dexscreener.com"
RUGCHECK = "https://api.rugcheck.xyz/v1"
# hosts core.http does not know: keep our own polite spacing on top of its default
EXTRA_SPACING = {"lite-api.jup.ag": 1.1, "api.jup.ag": 0.6, "api.rugcheck.xyz": 1.5}
_last_call = {}
# methods with a much tighter per-method limit on public nodes: extra bucket per endpoint and method
SCARCE_METHODS = {"getTokenLargestAccounts"}


class BudgetExceeded(Exception):
    pass


def _space(host):
    iv = EXTRA_SPACING.get(host)
    if not iv:
        return
    wait = _last_call.get(host, 0) + iv - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_call[host] = time.monotonic()


def _host(url):
    return urlparse(url).netloc


PAIRS_TTL = 60
PAIRS_BATCH = 30            # DexScreener tokens/v1 takes up to 30 addresses per request
_PAIRS = {}                 # mint -> (ts, [pairs where the mint is the base token])


def prefetch_pairs(mints, priority="bulk"):
    """One DexScreener request per 30 mints for a whole refresh pass instead of one per coin. Mints without a
    pair are stored as [] (answered). Failures leave the cache alone; the per-coin call is the fallback."""
    mints = [m for m in dict.fromkeys(mints) if m]
    for i in range(0, len(mints), PAIRS_BATCH):
        chunk = mints[i:i + PAIRS_BATCH]
        try:
            d = http.get_json(f"{DEXSCREENER}/tokens/v1/solana/{','.join(chunk)}", priority=priority, retries=2)
        except Exception as e:  # noqa: BLE001
            log.warning("safety: DexScreener batch failed (%s), per-coin requests instead", e)
            continue
        pairs = d if isinstance(d, list) else (d or {}).get("pairs") or []
        now = time.time()
        by = {m: [] for m in chunk}
        for p in pairs:
            base = (p.get("baseToken") or {}).get("address")
            if base in by:
                by[base].append(p)
        for m, ps in by.items():
            _PAIRS[m] = (now, ps)
    for m in [m for m, (ts, _) in _PAIRS.items() if time.time() - ts > PAIRS_TTL]:
        _PAIRS.pop(m, None)


class Fetcher:
    """priority=True: a live path (signal about to fire, real-money buy) may wait for the shared RPC budget
    and use its reserve; the background refresher (priority=False) never waits and never touches the reserve."""

    def __init__(self, cfg, max_calls=None, rpc_url=None, priority=True):
        self.cfg = cfg
        self.max_calls = max_calls if max_calls is not None else cfg.max_calls_per_token
        self.calls = 0
        self.rpc_calls = defaultdict(int)
        self.rpc_url = rpc_url or core_settings.rpc_url
        self.priority = priority
        self.lane = "live" if priority else "bulk"   # core.http lane on the shared public-RPC window

    def _spend(self, n=1):
        if self.calls + n > self.max_calls:
            raise BudgetExceeded(f"call budget {self.max_calls} used up")
        self.calls += n

    def _targets(self, method=None):
        """RPC endpoints in order of preference, each with its own per-minute budget: the configured (usually
        Helius) RPC first, the public node as overflow (since 18.10. for the refresher too: Helius may be used as
        needed; the refresher still leaves helius_reserve to live paths). Only standard 1-credit methods."""
        c = self.cfg
        own = (self.rpc_url, _host(self.rpc_url))
        pub_url = c.public_rpc_url
        if pub_url == getattr(http, "PUBLIC_RPC", None) and hasattr(http, "pick_public_rpc"):
            # the default public node is mainnet-beta, which refuses getTokenLargestAccounts in long 429 runs:
            # take core's pool pick for this method instead (publicnode first, healthy endpoints only)
            try:
                pub_url = http.pick_public_rpc(self.lane, method=method)
            except Exception:  # noqa: BLE001 - pool state unreadable: keep the configured node
                pub_url = c.public_rpc_url
        pub = (pub_url, _host(pub_url))
        if own[1] == pub[1] or c.public_per_min <= 0:
            return [own]
        return [own, pub]

    def _take(self, host, method=None):
        c = self.cfg
        helius = "helius" in host
        if method in SCARCE_METHODS and not budget.take(
                f"{host}_{method}", c.largest_per_min, spacing=c.largest_spacing_sec,
                wait=c.priority_wait_sec if self.priority else 0):
            return False
        return budget.take(host, c.helius_per_min if helius else c.public_per_min,
                           spacing=c.helius_spacing_sec if helius else c.public_spacing_sec,
                           reserve=c.helius_reserve if helius and not self.priority else 0,
                           wait=c.priority_wait_sec if self.priority else 0)

    # ---- Solana JSON-RPC ----
    def rpc(self, method, params):
        self._spend()
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        last, exhausted = None, False
        for url, host in self._targets(method):
            if not self._take(host, method):
                continue
            self.rpc_calls[host] += 1
            try:
                j = http.post_json(url, payload, retries=2, priority=self.lane)  # RPC method is read from payload
            except getattr(http, "CreditsExhausted", ()):  # Helius monthly/daily credits used: next endpoint
                exhausted = True
                continue
            except Exception as e:  # noqa: BLE001 - e.g. public RPC refuses a heavy method: try the next one
                last = e
                continue
            if "error" in j:
                last = RuntimeError(f"{method}: {j['error']}")
                continue
            return j.get("result")
        if last is not None and not exhausted:
            raise last
        raise BudgetExceeded(f"RPC budget used up for this minute ({method})")

    # ---- Jupiter quote (no key needed on lite-api; JUPITER_API_KEY used when set) ----
    def jupiter_quote(self, input_mint, output_mint, amount_raw, slippage_bps=300):
        self._spend()
        _space(self.cfg.jupiter_url.split("/")[2])
        headers = {"x-api-key": self.cfg.jupiter_api_key} if self.cfg.jupiter_api_key else None
        try:
            # core.http backs off on 429/5xx (Retry-After, host penalty); live paths may wait one round more
            return http.get_json(f"{self.cfg.jupiter_url}/swap/v1/quote", priority=self.lane,
                                 retries=2 if self.lane == "live" else 1, headers=headers, params={
                "inputMint": input_mint, "outputMint": output_mint, "amount": int(amount_raw),
                "slippageBps": slippage_bps})
        except Exception as e:  # noqa: BLE001 - 400 "no route" is an answer, not an outage
            msg = str(e)
            if msg.startswith("400") or "400 for" in msg:
                return {"error": "no_route"}
            raise

    # ---- DexScreener pairs of a token (keyless) ----
    def dexscreener_pairs(self, mint):
        hit = _PAIRS.get(mint)
        if hit and time.time() - hit[0] < PAIRS_TTL:
            return hit[1]           # from the pass's batch request, costs nothing
        self._spend()
        d = http.get_json(f"{DEXSCREENER}/tokens/v1/solana/{mint}", priority=self.lane, retries=2)
        return d if isinstance(d, list) else (d or {}).get("pairs") or []

    # ---- RugCheck full report (keyless cross-check) ----
    def rugcheck(self, mint):
        self._spend()
        _space("api.rugcheck.xyz")
        return http.get_json(f"{RUGCHECK}/tokens/{mint}/report", priority=self.lane, retries=1, timeout=25)
