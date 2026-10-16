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
    "api.geckoterminal.com": 4.0,      # documented 30 req/min, in practice 429s at 20/min across our processes
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


# Adaptive spacing for shared hosts: every 429 stretches the interval for all processes (x1.5, up to
# x4), every successful call shrinks it again slowly (x0.98), so the bots settle just under the real limit.
SLOWDOWN_ON_429, SLOWDOWN_MAX, SLOWDOWN_DECAY = 1.5, 4.0, 0.98
MIN_PENALTY_SECONDS = {"api.geckoterminal.com": 20.0}  # their 429 window is about a minute
DEFAULT_MIN_PENALTY = 5.0


def _read_state(f):
    """Lock file content: '<last_call_ts> <slowdown>' (older files hold only the timestamp)."""
    f.seek(0)
    parts = f.read().split()
    try:
        last = float(parts[0]) if parts else 0.0
        factor = float(parts[1]) if len(parts) > 1 else 1.0
    except ValueError:
        last, factor = 0.0, 1.0
    return last, min(max(factor, 1.0), SLOWDOWN_MAX)


def _write_state(f, last, factor):
    f.seek(0)
    f.truncate()
    f.write(f"{last!r} {factor:.4f}")
    f.flush()


def _locked_state(host):
    import fcntl
    os.makedirs(_LOCK_DIR, exist_ok=True)
    f = open(os.path.join(_LOCK_DIR, host + ".lock"), "a+")
    fcntl.flock(f, fcntl.LOCK_EX)
    return f


def _unlock(f):
    import fcntl
    try:
        fcntl.flock(f, fcntl.LOCK_UN)
    finally:
        f.close()


def throttle(host, interval=None):
    """Block until this process may call `host`. Cross-process for SHARED_HOSTS (fcntl lock file)."""
    iv = interval if interval is not None else HOST_INTERVAL.get(host, DEFAULT_INTERVAL)
    if host not in SHARED_HOSTS:
        return _wait_local(host, iv)
    try:
        f = _locked_state(host)
    except (ImportError, OSError):  # Windows / unwritable: fall back to per-process spacing
        return _wait_local(host, iv)
    try:
        last, factor = _read_state(f)
        delay = last + iv * factor - time.time()
        if delay > 0:
            time.sleep(delay)
        _write_state(f, time.time(), factor)
    finally:
        _unlock(f)


def penalize(host, seconds):
    """After a 429: push the shared next-call time back and stretch the interval for every process."""
    if host not in SHARED_HOSTS:
        return
    try:
        f = _locked_state(host)
    except (ImportError, OSError):
        return
    try:
        last, factor = _read_state(f)
        _write_state(f, max(last, time.time() + seconds), min(factor * SLOWDOWN_ON_429, SLOWDOWN_MAX))
    finally:
        _unlock(f)


def relax(host):
    """After a successful call: let the shared interval drift back toward its base value."""
    if host not in SHARED_HOSTS:
        return
    try:
        f = _locked_state(host)
    except (ImportError, OSError):
        return
    try:
        last, factor = _read_state(f)
        if factor > 1.0:
            _write_state(f, last, max(1.0, factor * SLOWDOWN_DECAY))
    finally:
        _unlock(f)


def slowdown(host):
    """Current shared slowdown factor of a host (1.0 = base interval). For status pages."""
    try:
        with open(os.path.join(_LOCK_DIR, host + ".lock")) as f:
            return _read_state(f)[1]
    except OSError:
        return 1.0


def _endpoint(url, kw):
    """'host/path' plus the JSON-RPC method, without query string (keys live there)."""
    u = urlparse(url)
    path = re.sub(r"/[1-9A-HJ-NP-Za-km-z]{32,44}(?=/|$)", "/<addr>", u.path)  # mints/pools -> one bucket
    method = (kw.get("json") or {}).get("method") if isinstance(kw.get("json"), dict) else None
    return f"{u.netloc}{path}" + (f" [{method}]" if method else "")


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
                delay = max(delay, MIN_PENALTY_SECONDS.get(host, DEFAULT_MIN_PENALTY))
                penalize(host, delay)
                log.warning("429 %s %s (slowdown x%.2f), sleeping %.1fs", method, _endpoint(url, kw),
                            slowdown(host), delay)
            else:
                log.warning("%s %s -> %s, sleeping %.1fs", method, _endpoint(url, kw), r.status_code, delay)
            time.sleep(min(delay, 60))
            continue
        if r.status_code >= 400:
            raise requests.HTTPError(f"{r.status_code} for {redact(r.url)}")
        relax(host)
        return r.json()


def get_json(url, params=None, headers=None, **kw):
    return _request("GET", url, params=params, headers=headers, **kw)


def post_json(url, payload, headers=None, **kw):
    return _request("POST", url, json=payload, headers=headers, **kw)
