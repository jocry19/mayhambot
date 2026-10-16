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


_BOT_TOKEN_RE = re.compile(r"(/bot)\d+:[A-Za-z0-9_-]+")  # Telegram: api.telegram.org/bot<id>:<secret>/...


def redact(text):
    return _BOT_TOKEN_RE.sub(r"\1***", _SECRET_RE.sub(r"\1***", str(text)))


_old_factory = logging.getLogRecordFactory()


def _redacting_factory(*a, **kw):
    rec = _old_factory(*a, **kw)
    if rec.args:
        rec.msg = rec.getMessage()
        rec.args = None
    rec.msg = redact(rec.msg)
    return rec


logging.setLogRecordFactory(_redacting_factory)

# Tracebacks (log.exception) are formatted after the record exists, so redact the final text as well.
if not getattr(logging.Formatter.format, "_redacting", False):
    _old_format = logging.Formatter.format

    def _redacting_format(self, record):
        return redact(_old_format(self, record))
    _redacting_format._redacting = True
    logging.Formatter.format = _redacting_format

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


# ---------------- per-service budgets and usage ----------------
# One API plan can sit behind several hosts (Helius: REST + RPC). Budgets and usage count per group.
HOST_GROUP = {"api.helius.xyz": "helius", "mainnet.helius-rpc.com": "helius", "beta.helius-rpc.com": "helius",
              "api.geckoterminal.com": "geckoterminal"}
# Calls per minute per service and group, used when the process knows its service name (MAYHAM_SERVICE,
# set by mayham.py, or set_service()). Override with <GROUP>_BUDGET_<SERVICE>, e.g. HELIUS_BUDGET_SAFETY=90.
# Helius free plan = 10 req/s = 600/min; these add up to 480/min, and the shared interval smooths peaks.
DEFAULT_BUDGETS = {
    "helius": {"signals": 60, "research": 120, "copytrade": 120, "safety": 60, "sniper": 120},
}
USAGE_LOG_EVERY = 600

_service = os.getenv("MAYHAM_SERVICE", "")
_budgets = {}      # group -> calls per minute (this process)
_recent = {}       # group -> deque of call times in the last minute
_usage = {}        # group -> {"calls", "429", "waited_s"} since start
_usage_window = {}  # same, since the last usage log line
_last_usage_log = [time.time()]
_last_usage_file = [0.0]
_budget_lock = threading.Lock()


def group_of(host):
    return HOST_GROUP.get(host, host)


def set_service(name):
    """Name this process (signals, research, copytrade, safety, sniper ...) so its default budgets apply."""
    global _service
    _service = name or ""


def set_budget(host_or_group, per_minute):
    """Cap this process at `per_minute` calls per minute to a host or group ('helius'); None/0 removes it."""
    g = group_of(host_or_group)
    with _budget_lock:
        if per_minute:
            _budgets[g] = float(per_minute)
        else:
            _budgets.pop(g, None)
            _budgets[g + ":off"] = True


def budget(host_or_group):
    """Calls per minute this process may make to a host/group, or None (unlimited, only the shared spacing)."""
    g = group_of(host_or_group)
    if g in _budgets:
        return _budgets[g]
    if _budgets.get(g + ":off") or not _service:
        return None
    env = os.getenv(f"{g.upper().replace('.', '_').replace('-', '_')}_BUDGET_{_service.upper()}")
    if env:
        try:
            return float(env) or None
        except ValueError:
            pass
    return DEFAULT_BUDGETS.get(g, {}).get(_service)


def _count(group, key, n=1):
    with _budget_lock:
        for d in (_usage, _usage_window):
            d.setdefault(group, {"calls": 0, "429": 0, "waited_s": 0.0})[key] += n


def _take_budget(host):
    """Wait until this process has budget left for `host` in the last 60 s, then book one call."""
    from collections import deque
    g = group_of(host)
    limit = budget(g)
    if limit:
        while True:
            with _budget_lock:
                q = _recent.setdefault(g, deque())
                now = time.time()
                while q and q[0] <= now - 60:
                    q.popleft()
                if len(q) < limit:
                    q.append(now)
                    break
                wait = q[0] + 60 - now
            _count(g, "waited_s", wait)
            time.sleep(max(wait, 0.05))
    _count(g, "calls")
    _maybe_log_usage()


def usage(all_services=False):
    """This process: {group: {calls, 429, waited_s, budget_per_min, last_min}} since start.
    all_services=True: {service: that dict} for every mayhambot process (from data/ratelimit/usage-*.json)."""
    if all_services:
        import glob
        import json
        out = {}
        for path in glob.glob(os.path.join(_LOCK_DIR, "usage-*.json")):
            try:
                with open(path) as f:
                    d = json.load(f)
                out[d.get("service") or os.path.basename(path)[6:-5]] = d
            except (OSError, ValueError):
                pass
        return out
    with _budget_lock:
        now = time.time()
        return {g: dict(v, budget_per_min=budget(g),
                        last_min=sum(1 for t in _recent.get(g, ()) if t > now - 60))
                for g, v in _usage.items()}


def _maybe_log_usage(force=False):
    if time.time() - _last_usage_file[0] >= 60:
        _write_usage_file()
    if not force and time.time() - _last_usage_log[0] < USAGE_LOG_EVERY:
        return
    with _budget_lock:
        window = dict(_usage_window)
        _usage_window.clear()
        _last_usage_log[0] = time.time()
    for g, v in sorted(window.items()):
        b = budget(g)
        log.info("usage %s: %d calls, %d 429, budget %s/min%s (last %d min, service %s)", g, v["calls"], v["429"],
                 f"{b:.0f}" if b else "-", f", waited {v['waited_s']:.0f}s" if v["waited_s"] else "",
                 USAGE_LOG_EVERY // 60, _service or "?")
    _write_usage_file()


def _write_usage_file():
    """Share this process's totals with the dashboard / TUI (usage(all_services=True))."""
    _last_usage_file[0] = time.time()
    if not _service:
        return
    import json
    try:
        os.makedirs(_LOCK_DIR, exist_ok=True)
        tmp = os.path.join(_LOCK_DIR, f".usage-{_service}.tmp")
        with open(tmp, "w") as f:
            json.dump({"service": _service, "pid": os.getpid(), "ts": int(time.time()), "groups": usage()}, f)
        os.replace(tmp, os.path.join(_LOCK_DIR, f"usage-{_service}.json"))
    except OSError:
        pass


def throttle(host, interval=None):
    """Block until this process may call `host`: first its own per-minute budget (see budget()), then the
    shared cross-process spacing for SHARED_HOSTS (fcntl lock file)."""
    _take_budget(host)
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
    _count(group_of(host), "429")
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
