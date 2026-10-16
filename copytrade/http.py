"""Small HTTP helpers: retry with backoff and a per-host rate limiter."""
from __future__ import annotations

import logging
import threading
import time
from typing import Any
from urllib.parse import urlparse

import requests

log = logging.getLogger("copytrade.http")

try:  # core.http installs a log-record factory that strips api keys from every log line
    from core.http import redact  # type: ignore
except Exception:  # copytrade without core: same rule locally
    import re

    _SECRET_RE = re.compile(r"((?:api[-_]?key|apikey|token)=)[^&\s'\"]+", re.I)

    def redact(text) -> str:
        return _SECRET_RE.sub(r"\1***", str(text))

    _old_factory = logging.getLogRecordFactory()

    def _redacting_factory(*a, **kw):
        rec = _old_factory(*a, **kw)
        if rec.args:
            rec.msg = rec.getMessage()
            rec.args = None
        rec.msg = redact(rec.msg)
        return rec

    logging.setLogRecordFactory(_redacting_factory)

try:  # cross-process limiter shared with signals/ and research/ (GeckoTerminal's free budget is per IP)
    from core.http import SHARED_HOSTS, penalize, throttle  # type: ignore
except Exception:
    SHARED_HOSTS, penalize, throttle = set(), None, None
try:  # Helius credits per request (1 for standard RPC, 100 for the Enhanced API) and the "used up" signal
    from core.http import CreditsExhausted, credit_cost  # type: ignore
except Exception:
    credit_cost = None

    class CreditsExhausted(requests.RequestException):  # type: ignore[no-redef]
        pass

_session = requests.Session()
_session.headers["User-Agent"] = "mayhambot-copytrade/1.0"


class RateLimiter:
    def __init__(self, per_second: float):
        self.interval = 1.0 / max(per_second, 0.01)
        self._next = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            if self._next > now:
                time.sleep(self._next - now)
                now = self._next
            self._next = now + self.interval


_limiters: dict[str, RateLimiter] = {
    "dexscreener": RateLimiter(4),  # 300/min documented
    "geckoterminal": RateLimiter(0.45),  # ~30/min free
    "jupiter": RateLimiter(1.0),  # lite tier
}


def limiter(name: str, per_second: float | None = None) -> RateLimiter:
    if name not in _limiters:
        _limiters[name] = RateLimiter(per_second or 2)
    return _limiters[name]


def request_json(
    method: str,
    url: str,
    *,
    limiter_name: str | None = None,
    retries: int = 4,
    timeout: float = 20,
    priority: str = "bulk",
    rpc_method: str | None = None,
    **kw: Any,
) -> Any:
    """priority: 'live' (fresh whale buy, own trade) or 'bulk' (backfill, discovery, safety-net polls) lane of
    core.http's public-RPC window; rpc_method lets core apply its per-method limit."""
    last: Exception | None = None
    host = urlparse(url).hostname or ""
    shared = throttle is not None and host in SHARED_HOSTS
    for attempt in range(retries):
        if shared:
            # one budget for all mayhambot processes; raises CreditsExhausted when today's Helius share is gone
            # (not retried: the caller falls back to the public RPC)
            _throttle(host, credit_cost(url, kw) if credit_cost else None, priority, rpc_method)
        elif limiter_name:
            limiter(limiter_name).wait()
        try:
            r = _session.request(method, url, timeout=timeout, **kw)
            if r.status_code == 429 and shared:
                try:
                    wait = float(r.headers.get("Retry-After") or 0)
                except ValueError:
                    wait = 0
                penalize(host, max(wait, 15 * (attempt + 1)))
            if r.status_code == 429 or r.status_code >= 500:
                raise requests.HTTPError(f"{r.status_code} {r.text[:200]}", response=r)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            last = e
            resp = getattr(e, "response", None)
            if resp is not None and 400 <= resp.status_code < 500 and resp.status_code != 429:
                break
            if not shared:  # shared hosts wait in throttle() (incl. the 429 penalty)
                time.sleep(min(2 ** attempt, 16))
    # requests puts the full URL (with any api-key) into its exception text: never pass it on raw
    raise RuntimeError(redact(f"{method} {url.split('?')[0]} failed: {last}"))


def _throttle(host: str, cost, priority: str, method: str | None) -> None:
    try:
        throttle(host, cost=cost, priority=priority, method=method)
    except TypeError:  # older core.http without lanes
        throttle(host, cost=cost)


def get_json(url: str, **kw: Any) -> Any:
    return request_json("GET", url, **kw)


def post_json(url: str, payload: Any, **kw: Any) -> Any:
    return request_json("POST", url, json=payload, **kw)
