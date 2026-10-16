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
    "api.helius.xyz": 0.6,              # free plan: DAS + Enhanced API together 2 req/s
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
            d.setdefault(group, {"calls": 0, "429": 0, "waited_s": 0.0, "credits": 0, "refused": 0})[key] += n


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
        out = {g: dict(v, budget_per_min=budget(g),
                       last_min=sum(1 for t in _recent.get(g, ()) if t > now - 60))
               for g, v in _usage.items()}
    if CREDIT_GROUP in out and _service:
        st = credits(_service) or {}
        out[CREDIT_GROUP].update({k: st.get(k) for k in ("credits_today", "credits_month", "credits_cap",
                                                         "credits_day_budget", "share_pct")})
    return out


def _credit_line():
    if not _service:
        return ""
    st = credits(_service)
    if not st or st["credits_cap"] is None:
        return ""
    return (f", credits today {st['credits_today']:.0f}/{st['credits_day_budget']:.0f}, month "
            f"{st['credits_month']:.0f}/{st['credits_cap']:.0f} ({st['share_pct']:.0f}% share)")


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
        log.info("usage %s: %d calls, %d 429, budget %s/min%s%s (last %d min, service %s)", g, v["calls"], v["429"],
                 f"{b:.0f}" if b else "-", f", waited {v['waited_s']:.0f}s" if v["waited_s"] else "",
                 _credit_line() if g == CREDIT_GROUP else "", USAGE_LOG_EVERY // 60, _service or "?")
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


# ---------------- Helius monthly credits ----------------
# helius.dev/docs/billing (checked 2026-10-16): free plan 1,000,000 credits per calendar month; standard RPC 1
# credit (incl. getTransaction, getSignaturesForAddress), getProgramAccounts 10, DAS 10, Enhanced
# Transactions / Wallet API (api.helius.xyz) 100, getTransactionsForAddress 10+.
CREDIT_GROUP = "helius"
DAS_METHODS = {"getAsset", "getAssetProof", "getAssetProofBatch", "getNftEditions", "getAssetsByOwner",
               "getAssetsByAuthority", "getAssetsByCreator", "getAssetsByGroup", "searchAssets", "getAssetBatch",
               "getSignaturesForAsset", "getTokenAccounts"}
RPC_10_CREDITS = {"getProgramAccounts", "getTransactionsForAddress", "getTransfersByAddress"}
# Share of the usable month (after the reserve) per service, percent. Override: HELIUS_SHARE_<SERVICE>.
DEFAULT_CREDIT_SHARES = {"research": 35, "sniper": 20, "signals": 15, "copytrade": 15, "safety": 15}
# Services that react to events may run ahead of their even daily share (still never past their month share).
CREDIT_BURST = {"safety": 1.5, "sniper": 1.5}
PUBLIC_RPC = "https://api.mainnet-beta.solana.com"


class CreditsExhausted(requests.RequestException):
    """This service's Helius credits for today are used up (callers treat it like any failed request)."""


def credit_cost(url, kw=None):
    """Helius credits one request costs (0 for other hosts). JSON-RPC batches cost the sum of their items."""
    host = urlparse(url).netloc
    if group_of(host) != CREDIT_GROUP:
        return 0
    if host == "api.helius.xyz":
        return 100
    payload = (kw or {}).get("json")
    items = payload if isinstance(payload, list) else [payload or {}]
    total = 0
    for it in items:
        m = it.get("method") if isinstance(it, dict) else None
        total += 10 if (m in DAS_METHODS or m in RPC_10_CREDITS) else 1
    return total


def _credit_cfg():
    def num(name, default):
        try:
            return float(os.getenv(name) or default)
        except ValueError:
            return float(default)
    shares = {svc: num(f"HELIUS_SHARE_{svc.upper()}", pct) for svc, pct in DEFAULT_CREDIT_SHARES.items()}
    if _service and _service not in shares:
        shares[_service] = num(f"HELIUS_SHARE_{_service.upper()}", 0)
    return num("HELIUS_CREDITS_MONTH", 1_000_000), num("HELIUS_CREDITS_RESERVE_PCT", 10), shares


def _ledger_path():
    return os.path.join(_LOCK_DIR, f"credits-{CREDIT_GROUP}.json")


def _now_parts(now=None):
    import calendar
    t = time.gmtime(now if now is not None else time.time())
    return f"{t.tm_year:04d}-{t.tm_mon:02d}", f"{t.tm_mday:02d}", calendar.monthrange(t.tm_year, t.tm_mon)[1], t.tm_mday


def _ledger_update(fn):
    """Read-modify-write the shared credit ledger under an exclusive lock. fn(ledger) -> result."""
    import json
    os.makedirs(_LOCK_DIR, exist_ok=True)
    with open(_ledger_path(), "a+") as f:
        try:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX)
        except ImportError:
            pass
        f.seek(0)
        try:
            led = json.loads(f.read() or "{}")
        except ValueError:
            led = {}
        month = _now_parts()[0]
        if led.get("month") != month:  # new UTC calendar month: start from zero
            led = {"month": month, "services": {}}
        res = fn(led)
        f.seek(0)
        f.truncate()
        json.dump(led, f)
        f.flush()
        return res


def _svc_stats(led, svc):
    month, day, days_in_month, mday = _now_parts()
    d = led.get("services", {}).get(svc, {})
    used_month = d.get("month", 0)
    used_today = (d.get("days") or {}).get(day, 0)
    total_month = sum(v.get("month", 0) for v in led.get("services", {}).values())
    credits_month_total, reserve_pct, shares = _credit_cfg()
    usable = credits_month_total * (1 - reserve_pct / 100)
    share = shares.get(svc)
    cap = usable * share / 100 if share is not None else None
    day_budget = None
    if cap is not None:
        days_left = days_in_month - mday + 1  # leftovers of earlier days spread over the rest of the month
        day_budget = max(0.0, (cap - (used_month - used_today)) / days_left)
    return dict(credits_today=used_today, credits_month=used_month, credits_cap=cap, credits_day_budget=day_budget,
                share_pct=share, credits_month_all=total_month, credits_month_limit=credits_month_total)


def credits(service=None):
    """Helius credit ledger: {service: {credits_today, credits_month, credits_cap, credits_day_budget, share_pct,
    ...}} for every service (or one dict for `service`). Shared by all processes, survives restarts."""
    def read(led):
        svcs = set(led.get("services", {})) | set(_credit_cfg()[2])
        return {s: _svc_stats(led, s) for s in sorted(svcs)}
    try:
        allc = _ledger_update(read)
    except OSError:
        return {} if service is None else None
    return allc.get(service) if service else allc


def credits_left_today(service=None):
    """Credits this service may still spend today (None = no limit, e.g. a process without a service name)."""
    svc = service if service is not None else _service
    if not svc:
        return None
    st = credits(svc)
    if not st or st["credits_day_budget"] is None:
        return None
    day_left = st["credits_day_budget"] * CREDIT_BURST.get(svc, 1.0) - st["credits_today"]
    month_left = st["credits_cap"] - st["credits_month"]
    return max(0.0, min(day_left, month_left))


_credit_warned = set()


def _charge(cost):
    """Book `cost` credits for this service; raise CreditsExhausted when today's share is used up."""
    if cost <= 0:
        return
    svc = _service or "other"

    def book(led):
        st = _svc_stats(led, svc)
        if _service and st["credits_day_budget"] is not None:
            day_left = st["credits_day_budget"] * CREDIT_BURST.get(svc, 1.0) - st["credits_today"]
            if cost > min(day_left, st["credits_cap"] - st["credits_month"]):
                return st, False
        day = _now_parts()[1]
        d = led.setdefault("services", {}).setdefault(svc, {"month": 0, "days": {}})
        d["month"] = d.get("month", 0) + cost
        d.setdefault("days", {})[day] = d["days"].get(day, 0) + cost
        st["credits_month_all"] += cost
        return st, True
    try:
        st, ok = _ledger_update(book)
    except OSError:
        return
    _count(CREDIT_GROUP, "credits", cost) if ok else _count(CREDIT_GROUP, "refused", 1)
    month = _now_parts()[0]
    if st["credits_month_all"] >= 0.8 * st["credits_month_limit"] and month not in _credit_warned:
        _credit_warned.add(month)
        log.warning("Helius credits: %.0f of %.0f this month used (>= 80%%)", st["credits_month_all"],
                    st["credits_month_limit"])
    if not ok:
        raise CreditsExhausted(f"Helius credits for {svc} used up today ({st['credits_today']:.0f} of "
                               f"{(st['credits_day_budget'] or 0):.0f}); retry tomorrow or use the public RPC")


def rpc_url(method="", helius_url=None):
    """RPC endpoint for a JSON-RPC `method`: the Helius URL while this service has credits left today,
    else the public RPC for standard methods (DAS / Helius-only methods keep Helius and may raise)."""
    url = helius_url or os.getenv("SOLANA_RPC_URL") or ""
    if group_of(urlparse(url).netloc) != CREDIT_GROUP:
        return url or PUBLIC_RPC
    cost = credit_cost(url, {"json": {"method": method}})
    left = credits_left_today()
    if left is not None and left < cost and cost == 1:
        return PUBLIC_RPC
    return url


def throttle(host, interval=None, cost=None):
    """Block until this process may call `host`: Helius credits (cost, default = the host's base cost),
    then its own per-minute budget (see budget()), then the shared cross-process spacing for SHARED_HOSTS."""
    _charge(credit_cost(f"https://{host}/") if cost is None else cost)
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
    cost = credit_cost(url, kw)
    if cost == 1 and method == "POST" and kw.get("public_fallback", True):
        left = credits_left_today()
        if left is not None and left < cost:  # today's Helius share used up: standard RPC goes to the public node
            url, host, cost = PUBLIC_RPC, urlparse(PUBLIC_RPC).netloc, 0
    kw.pop("public_fallback", None)
    for attempt in range(retries + 1):
        throttle(host, cost=cost)
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
