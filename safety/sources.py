"""Network access of the safety layer. Everything goes through core.http (shared per-host limits,
retries, key redaction). One Fetcher per assessment carries a call budget, so a single coin can never
eat the RPC capacity the live loops need. Tests swap in a fixture Fetcher with the same methods."""
import logging
import time

from core import http
from core.config import settings as core_settings

log = logging.getLogger("mayhambot.safety")

DEXSCREENER = "https://api.dexscreener.com"
RUGCHECK = "https://api.rugcheck.xyz/v1"
# hosts core.http does not know: keep our own polite spacing on top of its default
EXTRA_SPACING = {"lite-api.jup.ag": 1.1, "api.jup.ag": 0.6, "api.rugcheck.xyz": 1.5}
_last_call = {}


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


class Fetcher:
    def __init__(self, cfg, max_calls=None, rpc_url=None):
        self.cfg = cfg
        self.max_calls = max_calls if max_calls is not None else cfg.max_calls_per_token
        self.calls = 0
        self.rpc_url = rpc_url or core_settings.rpc_url

    def _spend(self, n=1):
        if self.calls + n > self.max_calls:
            raise BudgetExceeded(f"call budget {self.max_calls} used up")
        self.calls += n

    # ---- Solana JSON-RPC ----
    def rpc(self, method, params):
        self._spend()
        j = http.post_json(self.rpc_url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                           retries=2)
        if "error" in j:
            raise RuntimeError(f"{method}: {j['error']}")
        return j.get("result")

    # ---- Jupiter quote (no key needed on lite-api; JUPITER_API_KEY used when set) ----
    def jupiter_quote(self, input_mint, output_mint, amount_raw, slippage_bps=300):
        self._spend()
        _space(self.cfg.jupiter_url.split("/")[2])
        headers = {"x-api-key": self.cfg.jupiter_api_key} if self.cfg.jupiter_api_key else None
        try:
            return http.get_json(f"{self.cfg.jupiter_url}/swap/v1/quote", retries=1, headers=headers, params={
                "inputMint": input_mint, "outputMint": output_mint, "amount": int(amount_raw),
                "slippageBps": slippage_bps})
        except Exception as e:  # noqa: BLE001 - 400 "no route" is an answer, not an outage
            msg = str(e)
            if msg.startswith("400") or "400 for" in msg:
                return {"error": "no_route"}
            raise

    # ---- DexScreener pairs of a token (keyless) ----
    def dexscreener_pairs(self, mint):
        self._spend()
        d = http.get_json(f"{DEXSCREENER}/tokens/v1/solana/{mint}", retries=2)
        return d if isinstance(d, list) else (d or {}).get("pairs") or []

    # ---- RugCheck full report (keyless cross-check) ----
    def rugcheck(self, mint):
        self._spend()
        _space("api.rugcheck.xyz")
        return http.get_json(f"{RUGCHECK}/tokens/{mint}/report", retries=1, timeout=25)
