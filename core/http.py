"""Shared HTTP helper: per-host rate limit, retries on 429/5xx, JSON in/out."""
import logging
import os
import re
import threading
import time
from urllib.parse import urlparse

import requests

log = logging.getLogger("mayhambot.http")

# API keys travel in URLs (Helius, RPC providers). Redact them in every log record and exception text.
_SECRET_RE = re.compile(r"((?:api[-_]?key|apikey|token)=)[^&\s'\"]+", re.I)


def redact(text):
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

# minimum seconds between calls per host (keyless public limits)
HOST_INTERVAL = {
    "api.geckoterminal.com": 3.0,      # documented 30 req/min, in practice 429s below ~20/min
    "api.dexscreener.com": 0.25,       # 300 req/min on pair endpoints, 60 on profiles
    "api.mainnet-beta.solana.com": 0.25,
    "api.helius.xyz": 0.15,             # shared by all processes, see SHARED_HOSTS
    "mainnet.helius-rpc.com": 0.12,
    "beta.helius-rpc.com": 0.12,
    "api.telegram.org": 0.05,
}
DEFAULT_INTERVAL = 0.2

_session = requests.Session()
_session.headers.update({"User-Agent": "mayhambot/1.0", "Accept": "application/json"})
_last = {}
_lock = threading.Lock()


# Hosts whose limit is shared by every mayhambot process (signals, copytrade, research run separately).
# Their spacing is coordinated through a lock file, so together they stay under the free-tier limit.
SHARED_HOSTS = {"api.geckoterminal.com", "api.helius.xyz", "mainnet.helius-rpc.com", "beta.helius-rpc.com",
                "api.mainnet-beta.solana.com"}
_LOCK_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "ratelimit")


def throttle(host, interval=None):
    """Block until this process may call `host`. Cross-process for SHARED_HOSTS (fcntl lock file)."""
    iv = interval if interval is not None else HOST_INTERVAL.get(host, DEFAULT_INTERVAL)
    if host not in SHARED_HOSTS:
        return _wait_local(host, iv)
    try:
        import fcntl
    except ImportError:  # Windows: fall back to per-process spacing
        return _wait_local(host, iv)
    os.makedirs(_LOCK_DIR, exist_ok=True)
    with open(os.path.join(_LOCK_DIR, host + ".lock"), "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.seek(0)
            try:
                last = float(f.read().strip() or 0)
            except ValueError:
                last = 0.0
            delay = last + iv - time.time()
            if delay > 0:
                time.sleep(delay)
            f.seek(0)
            f.truncate()
            f.write(repr(time.time()))
            f.flush()
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def penalize(host, seconds):
    """After a 429: push the shared next-call time back for every process."""
    if host not in SHARED_HOSTS:
        return
    try:
        import fcntl
        os.makedirs(_LOCK_DIR, exist_ok=True)
        with open(os.path.join(_LOCK_DIR, host + ".lock"), "a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.seek(0)
            f.truncate()
            f.write(repr(time.time() + seconds))
            fcntl.flock(f, fcntl.LOCK_UN)
    except (ImportError, OSError):
        pass


def _wait(host):
    throttle(host)


def _wait_local(host, iv):
    with _lock:
        now = time.monotonic()
        nxt = _last.get(host, 0) + iv
        if nxt > now:
            time.sleep(nxt - now)
        _last[host] = time.monotonic()


def _request(method, url, retries=4, timeout=20, **kw):
    host = urlparse(url).netloc
    for attempt in range(retries + 1):
        _wait(host)
        try:
            r = _session.request(method, url, timeout=timeout, **kw)
        except requests.RequestException as e:
            if attempt == retries:
                raise requests.RequestException(redact(e)) from None
            log.warning("%s %s failed (%s), retry", method, host, e)
            time.sleep(2 ** attempt)
            continue
        if r.status_code == 429 or r.status_code >= 500:
            if attempt == retries:
                raise requests.HTTPError(f"{r.status_code} for {redact(r.url)}")
            delay = float(r.headers.get("Retry-After") or 0) or (2 ** attempt) * 2
            if r.status_code == 429:
                penalize(host, delay)
            log.warning("%s %s -> %s, sleeping %.1fs", method, host, r.status_code, delay)
            time.sleep(min(delay, 60))
            continue
        if r.status_code >= 400:
            raise requests.HTTPError(f"{r.status_code} for {redact(r.url)}")
        return r.json()


def get_json(url, params=None, headers=None, **kw):
    return _request("GET", url, params=params, headers=headers, **kw)


def post_json(url, payload, headers=None, **kw):
    return _request("POST", url, json=payload, headers=headers, **kw)
