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
    "api.mainnet-beta.solana.com": 0.11,  # plus WINDOW_LIMITS (100 / 10 s per IP, 40 / 10 s per method)
    "api.helius.xyz": 0.6,              # free plan: DAS + Enhanced API together 2 req/s, see DAS_ENHANCED_SPACING
    "helius-das-enhanced": 0.5,         # virtual: every DAS (any Helius host) and api.helius.xyz call, 2 req/s
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
DAS_ENHANCED_SPACING = "helius-das-enhanced"
SHARED_HOSTS = {DAS_ENHANCED_SPACING, "api.geckoterminal.com", "api.helius.xyz", "mainnet.helius-rpc.com", "beta.helius-rpc.com",
                "api.mainnet-beta.solana.com"}
_LOCK_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "ratelimit")


# Adaptive spacing for shared hosts: every 429 stretches the interval for all processes (x1.5, up to
# x4), every successful call shrinks it again slowly (x0.98), so the bots settle just under the real limit.
SLOWDOWN_ON_429, SLOWDOWN_MAX, SLOWDOWN_DECAY = 1.5, 4.0, 0.98
POOL_SLOWDOWN_MAX = 2.0     # public RPC pool: the windows already limit; a 429 must not stall all services
POOL_MAX_SLEEP_429 = 3.0    # public RPC pool: sleep after a 429 at most this long (then retry, maybe elsewhere)
POOL_TIMEOUT = 8            # seconds per public RPC call (a slow endpoint fails over instead of blocking 20 s)
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


def _max_slowdown(host):
    return POOL_SLOWDOWN_MAX if host in WINDOW_LIMITS else SLOWDOWN_MAX


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
            d.setdefault(group, {"calls": 0, "429": 0, "waited_s": 0.0, "credits": 0, "refused": 0,
                                 "live_calls": 0, "normal_calls": 0, "bulk_calls": 0, "live_waited_s": 0.0,
                                 "normal_waited_s": 0.0, "bulk_waited_s": 0.0, "latency_ms_sum": 0.0})[key] += n


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
                       last_min=sum(1 for t in _recent.get(g, ()) if t > now - 60),
                       latency_ms=round(_latency[g], 1) if g in _latency else None)
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
DEFAULT_CREDIT_SHARES = {"research": 50, "signals": 12.5, "safety": 12.5, "copytrade": 12.5, "sniper": 12.5}
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
        led = _roll(led, write=True)
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
    led = _ledger_read()
    svcs = set(led.get("services", {})) | set(_credit_cfg()[2])
    status = _status(led)
    allc = {s: dict(_svc_stats(led, s), key_changed_at=status["key_changed_at"], warn_level=status["warn_level"],
                    warn=status["warn"]) for s in sorted(svcs)}
    return allc.get(service) if service else allc


WARN_LEVEL = 0.8


def _status(led):
    used = sum(v.get("month", 0) for v in led.get("services", {}).values())
    limit = _credit_cfg()[0]
    level = round(used / limit, 4) if limit else 0.0
    return dict(month=led.get("month"), key_fp=led.get("key_fp"), key_changed_at=led.get("key_changed_at"),
                used=used, limit=limit, warn_level=level, warn=level >= WARN_LEVEL,
                history=led.get("history", []))


def credits_status():
    """Whole-plan view: {month, key_fp (8 hex of sha256, never the key), key_changed_at, used, limit,
    warn_level (share of the month used, 0..1), warn (>= 80 %), history [{month, key_fp, used, until}]}."""
    return _status(_ledger_read())


def _key_fp():
    key = os.getenv("HELIUS_API_KEY", "")
    if not key:
        try:
            from core.config import settings
            key = settings.helius_api_key or ""
        except Exception:  # noqa: BLE001
            key = ""
    if not key:
        return ""
    import hashlib
    return hashlib.sha256(key.encode()).hexdigest()[:8]


def _roll(led, write=False):
    """New UTC month or a new Helius key (fresh quota): start counting from zero, keep the old totals in
    led['history']. write=False only computes the view (credits() never writes)."""
    month = _now_parts()[0]
    fp = _key_fp()
    services = led.get("services", {})
    used = sum(v.get("month", 0) for v in services.values())
    history = list(led.get("history", []))
    new = dict(led)
    if led.get("month") != month:
        if led.get("month") and used:
            history.append({"month": led.get("month"), "key_fp": led.get("key_fp"), "used": used,
                            "until": int(time.time()), "reason": "month"})
        new = {"month": month, "services": {}, "key_fp": fp or led.get("key_fp"),
               "key_changed_at": led.get("key_changed_at")}
    elif fp and led.get("key_fp") and fp != led.get("key_fp") and fp not in {h.get("key_fp") for h in history}:
        # (a key seen before = a process still running with the old .env: never switch back)
        history.append({"month": month, "key_fp": led.get("key_fp"), "used": used, "until": int(time.time()),
                        "reason": "key"})
        new = {"month": month, "services": {}, "key_fp": fp, "key_changed_at": int(time.time())}
        if write:
            log.warning("Helius key changed, credit ledger reset (old key used %d credits this month)", used)
    elif fp and not led.get("key_fp"):
        new["key_fp"] = fp
    new["history"] = history[-24:]
    return new


def _ledger_read():
    """Read-only, no lock: writers replace the content under the lock; a torn or stale read only affects
    a display or a pre-check, never the booking itself (that runs in _charge under the lock)."""
    import json
    try:
        with open(_ledger_path()) as f:
            led = json.loads(f.read() or "{}")
    except (OSError, ValueError):
        led = {}
    return _roll(led)


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
        return url or pick_public_rpc()
    cost = credit_cost(url, {"json": {"method": method}})
    left = credits_left_today()
    if left is not None and left < cost and cost == 1:
        return pick_public_rpc()
    return url


# ---------------- live / bulk lanes with window limits ----------------
# Solana public RPC (docs "Clusters and public RPC endpoints"): 100 requests / 10 s per IP, 40 / 10 s per single
# method, 40 concurrent connections. We keep a margin; live calls (a fresh whale buy) may use the whole window,
# bulk calls (backfills) only (1 - LIVE_SHARE) of it, and bulk pauses while live calls are waiting.
def _env_int(name, default):
    try:
        return int(os.getenv(name) or default)
    except ValueError:
        return default


# Below the documented 100 / 40: other traffic from the same IP (copytrade websockets, browser) counts too,
# and 429s at 90 / 36 showed up on the Mac. Tune with PUBLIC_RPC_WINDOW_TOTAL / PUBLIC_RPC_WINDOW_PER_METHOD.
WINDOW_LIMITS = {"api.mainnet-beta.solana.com": {"window": 10.0, "total": _env_int("PUBLIC_RPC_WINDOW_TOTAL", 60),
                                                 "per_method": _env_int("PUBLIC_RPC_WINDOW_PER_METHOD", 24)}}
HALVE_SECONDS = 60  # after a 429 the endpoint's window is halved for this long


def _parse_map(env, default):
    """'a=b,c=d' -> dict (values int if numeric)."""
    raw = os.getenv(env)
    if not raw:
        return dict(default)
    out = {}
    for part in raw.split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = int(v) if v.strip().isdigit() else v.strip()
    return out


# Methods with a tighter limit on an endpoint, per 10 s: 'method@host=n' (PUBLIC_RPC_METHOD_LIMITS).
# mainnet-beta rejected a third of getTokenLargestAccounts at low volume on the Mac.
METHOD_LIMITS = _parse_map("PUBLIC_RPC_METHOD_LIMITS", {"getTokenLargestAccounts@api.mainnet-beta.solana.com": 6})
# Methods that should go to one endpoint first: 'method=host' (PUBLIC_RPC_PREFER).
METHOD_PREFER = _parse_map("PUBLIC_RPC_PREFER", {"getTokenLargestAccounts": "solana-rpc.publicnode.com"})
LIVE_MARKER_SECONDS = 2.0
LANE_RANK = {"live": 2, "normal": 1, "bulk": 0}  # waiting order: live before normal before bulk


def live_share():
    try:
        return min(0.9, max(0.0, float(os.getenv("PUBLIC_RPC_LIVE_SHARE") or 0.4)))
    except ValueError:
        return 0.4


def _window_wait(host, method, priority):
    """Cross-process sliding window for hosts in WINDOW_LIMITS. Returns seconds waited."""
    import json
    lim = WINDOW_LIMITS.get(host)
    if not lim:
        return 0.0
    try:
        import fcntl
    except ImportError:
        return 0.0
    os.makedirs(_LOCK_DIR, exist_ok=True)
    path = os.path.join(_LOCK_DIR, host + ".window")
    rank = LANE_RANK.get(priority, 0)
    live = rank == 2
    markers = {r: os.path.join(_LOCK_DIR, f"{host}.{name}") for name, r in LANE_RANK.items() if r > 0}
    waited, t0 = 0.0, time.time()
    while True:
        with open(path, "a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                f.seek(0)
                try:
                    calls = json.loads(f.read() or "[]")
                except ValueError:
                    calls = []
                now = time.time()
                calls = [c for c in calls if c[0] > now - lim["window"]]
                total = len(calls)
                same = sum(1 for c in calls if c[1] == (method or ""))
                total_cap = lim["total"] // 2 if _halved(host, now) else lim["total"]
                per_method = METHOD_LIMITS.get(f"{method}@{host}", lim["per_method"])
                per_method = max(1, per_method // 2) if _halved(host, now) else per_method
                # live: whole window; normal (service cycles): all but half the live share; bulk: no live share
                cap = total_cap if live else int(total_cap * (1 - live_share() * (0.5 if rank == 1 else 1.0)))
                live_waiting = False  # a higher lane is waiting -> step aside
                for r, mk in markers.items():
                    if r > rank:
                        try:
                            live_waiting = live_waiting or now - os.path.getmtime(mk) < LIVE_MARKER_SECONDS
                        except OSError:
                            pass
                ok = total < cap and same < per_method and not live_waiting
                if ok:
                    calls.append([now, method or "", "LNB"[2 - rank]])
                    f.seek(0)
                    f.truncate()
                    json.dump(calls, f)
                    f.flush()
                    break
                # wait until the oldest relevant call leaves the window
                rel = [c[0] for c in calls if total >= cap or c[1] == (method or "")]
                wait = (min(rel) + lim["window"] - now) if rel and not live_waiting else 0.2
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
        if rank > 0:  # tell lower lanes in every process to step aside
            try:
                with open(markers[rank], "a"):
                    os.utime(markers[rank], None)
            except OSError:
                pass
        time.sleep(max(0.05, min(wait, 2.0)))
        waited = time.time() - t0
    return waited


def throttle(host, interval=None, cost=None, das=False, priority="bulk", method=None):
    """Block until this process may call `host`: Helius credits (cost, default = the host's base cost),
    then its own per-minute budget (see budget()), then the shared cross-process spacing for SHARED_HOSTS,
    then the window limits (WINDOW_LIMITS) in the 'live' or 'bulk' lane.
    DAS calls (das=True) and api.helius.xyz also wait for the fixed 2 req/s DAS + Enhanced spacing.
    priority='live' for time-critical reads (copying a fresh buy), 'bulk' (default) for backfills."""
    lane = priority if priority in LANE_RANK else "bulk"
    _charge(credit_cost(f"https://{host}/") if cost is None else cost)
    _take_budget(host)
    t0 = time.time()
    if das or host == "api.helius.xyz":
        _shared_spacing(DAS_ENHANCED_SPACING, HOST_INTERVAL[DAS_ENHANCED_SPACING])
    if host in WINDOW_LIMITS and lane == "live":
        # live skips the plain spacing queue (bulk callers hold it); the window keeps the host limit
        _window_wait(host, method, lane)
    else:
        _shared_spacing(host, interval)
        _window_wait(host, method, lane)
    g = group_of(host)
    _count(g, f"{lane}_calls")
    _count(g, f"{lane}_waited_s", time.time() - t0)


def _shared_spacing(host, interval=None):
    iv = interval if interval is not None else HOST_INTERVAL.get(host, DEFAULT_INTERVAL)
    if host not in SHARED_HOSTS:
        return _wait_local(host, iv)
    try:
        f = _locked_state(host)
    except (ImportError, OSError):  # Windows / unwritable: fall back to per-process spacing
        return _wait_local(host, iv)
    try:
        last, factor = _read_state(f)
        factor = min(factor, _max_slowdown(host))
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
        _write_state(f, max(last, time.time() + seconds), min(factor * SLOWDOWN_ON_429, _max_slowdown(host)))
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


# ---------------- pool of keyless public Solana RPC endpoints ----------------
# Candidates (not verifiable from the build machine): each is probed once (getSignaturesForAddress +
# getTransaction jsonParsed v1) and dead or keyed ones leave the rotation for POOL_COOLDOWN seconds.
# Verified keyless on the Mac 2026-10-16; drpc.org and ankr (needs a key) failed the probe. More via PUBLIC_RPC_URLS.
DEFAULT_PUBLIC_RPC_URLS = ["https://api.mainnet-beta.solana.com", "https://solana-rpc.publicnode.com"]
POOL_COOLDOWN = 600          # after a failed probe
POOL_ERROR_COOLDOWN = 120    # after POOL_FAILS_TO_DISABLE timeouts / 5xx in a row
POOL_FAILS_TO_DISABLE = 3
PROBE_ADDRESS = "So11111111111111111111111111111111111111112"
_latency = {}   # host -> EWMA ms (this process)
_pool_fail = {}  # host -> consecutive failures (this process)


def public_rpc_urls():
    raw = os.getenv("PUBLIC_RPC_URLS")
    urls = [u.strip().rstrip("/") for u in raw.split(",")] if raw else list(DEFAULT_PUBLIC_RPC_URLS)
    return [u for u in urls if u]


def _pool_hosts():
    hosts = [urlparse(u).netloc for u in public_rpc_urls()]
    for h in hosts:  # every pool endpoint gets its own shared spacing and window
        SHARED_HOSTS.add(h)
        HOST_INTERVAL.setdefault(h, 0.11)
        WINDOW_LIMITS.setdefault(h, dict(WINDOW_LIMITS["api.mainnet-beta.solana.com"]))
    return hosts


def _pool_state_path():
    return os.path.join(_LOCK_DIR, "rpc-pool.json")


def _pool_state():
    import json
    try:
        with open(_pool_state_path()) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _pool_update(host, **fields):
    import json
    try:
        os.makedirs(_LOCK_DIR, exist_ok=True)
        with open(_pool_state_path(), "a+") as f:
            try:
                import fcntl
                fcntl.flock(f, fcntl.LOCK_EX)
            except ImportError:
                pass
            f.seek(0)
            try:
                st = json.loads(f.read() or "{}")
            except ValueError:
                st = {}
            st.setdefault(host, {}).update(fields)
            f.seek(0)
            f.truncate()
            json.dump(st, f)
    except OSError:
        pass


def _probe(url):
    """True if the endpoint answers getSignaturesForAddress and getTransaction(jsonParsed, v1) without a key."""
    try:
        t0 = time.time()
        r = _session.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "getSignaturesForAddress",
                                     "params": [PROBE_ADDRESS, {"limit": 1}]}, timeout=10)
        sigs = r.json().get("result") if r.status_code == 200 else None
        if not sigs:
            return False, None
        r = _session.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "getTransaction", "params": [
            sigs[0]["signature"], {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1}]}, timeout=10)
        ok = r.status_code == 200 and isinstance(r.json().get("result"), dict)
        return ok, (time.time() - t0) * 500  # two calls -> per call
    except Exception:  # noqa: BLE001
        return False, None


def pool_status():
    """{host: {url, ok, disabled_until, latency_ms, checked_at, reason}} shared by all processes."""
    st = _pool_state()
    out = {}
    for u in public_rpc_urls():
        h = urlparse(u).netloc
        d = dict(st.get(h, {}))
        d["url"] = u
        if h in _latency:
            d["latency_ms"] = round(_latency[h], 1)
        out[h] = d
    return out


def _healthy(now=None, probe=None):
    """Pool URLs usable now; endpoints never checked (or checked > POOL_COOLDOWN ago) are probed first."""
    now = now or time.time()
    if probe is None and (os.getenv("PYTEST_CURRENT_TEST") or os.getenv("PUBLIC_RPC_PROBE") == "0"):
        probe = lambda u: (True, None)  # noqa: E731 - tests stay offline
    probe = probe or _probe
    st = _pool_state()
    good = []
    for u in public_rpc_urls():
        h = urlparse(u).netloc
        d = st.get(h, {})
        if d.get("disabled_until", 0) > now:
            continue
        if now - d.get("checked_at", 0) > POOL_COOLDOWN:
            ok, ms = probe(u)
            fields = dict(checked_at=now, ok=ok)
            if ok:
                fields.update(latency_ms=ms, disabled_until=0, reason="")
                if ms:
                    _latency.setdefault(h, ms)
            else:
                fields.update(disabled_until=now + POOL_COOLDOWN, reason="probe failed")
                log.warning("public RPC %s left the pool for %d min: probe failed", h, POOL_COOLDOWN // 60)
            if not ok and u == public_rpc_urls()[-1] and not good:
                fields.update(disabled_until=0, reason="probe failed, kept as last endpoint")
                ok = True
            _pool_update(h, **fields)
            if not ok:
                continue
        good.append(u)
    return good


def _window_load(host):
    import json
    lim = WINDOW_LIMITS.get(host)
    try:
        with open(os.path.join(_LOCK_DIR, host + ".window")) as f:
            calls = json.loads(f.read() or "[]")
    except (OSError, ValueError):
        return 0.0
    now = time.time()
    return sum(1 for c in calls if c[0] > now - lim["window"]) / lim["total"] if lim else 0.0


def pick_public_rpc(priority="bulk", probe=None, method=None):
    """One keyless public RPC URL: the endpoint preferred for `method` (METHOD_PREFER) while it is healthy and
    not full, else 'live' = fastest measured endpoint, 'bulk' = the one with most free window."""
    _pool_hosts()
    urls = _healthy(probe=probe) or public_rpc_urls()[:1]
    pref = METHOD_PREFER.get(method or "")
    if pref:
        for u in urls:
            if urlparse(u).netloc == pref and _window_load(pref) < 0.9:
                return u
    if priority == "live":
        st = _pool_state()
        return min(urls, key=lambda u: _latency.get(urlparse(u).netloc)
                   or st.get(urlparse(u).netloc, {}).get("latency_ms") or 1e9)
    return min(urls, key=lambda u: (_window_load(urlparse(u).netloc), _latency.get(urlparse(u).netloc) or 1e9))


_halved_cache = {}


def _halved(host, now=None):
    now = now or time.time()
    c = _halved_cache.get(host)
    if c is None or now - c[0] > 2:  # re-read the shared state at most every 2 s
        _halved_cache[host] = c = (now, _pool_state().get(host, {}).get("halved_until", 0))
    return c[1] > now


def _pool_result(host, ok, ms=None, status=None):
    """Book a pool call. 429 = back off (shared slowdown in _request, plus this endpoint's window halved for
    HALVE_SECONDS), never a reason to drop it. Timeouts / 5xx: POOL_FAILS_TO_DISABLE in a row take it out for
    POOL_COOLDOWN, unless it is the last healthy endpoint."""
    if ok:
        _pool_fail[host] = 0
        if ms is not None:
            _latency[host] = ms if host not in _latency else 0.8 * _latency[host] + 0.2 * ms
            _count(host, "latency_ms_sum", ms)
        return
    if status == 429:
        until = time.time() + HALVE_SECONDS
        _pool_update(host, halved_until=until)
        _halved_cache[host] = (time.time(), until)
        return
    _pool_fail[host] = _pool_fail.get(host, 0) + 1
    if _pool_fail[host] >= POOL_FAILS_TO_DISABLE:
        _pool_fail[host] = 0
        now = time.time()
        st = _pool_state()
        others = [u for u in public_rpc_urls() if urlparse(u).netloc != host
                  and st.get(urlparse(u).netloc, {}).get("disabled_until", 0) <= now]
        if not others:
            log.warning("public RPC %s failing (%s) but it is the last healthy endpoint, keeping it", host, status)
            return
        _pool_update(host, disabled_until=now + POOL_ERROR_COOLDOWN, reason=f"{status or 'errors'} x3")
        log.warning("public RPC %s left the pool for %d s (%s)", host, POOL_ERROR_COOLDOWN, status or "errors")


def _request(method, url, retries=4, timeout=20, priority="bulk", **kw):
    host = urlparse(url).netloc
    cost = credit_cost(url, kw)
    if cost == 1 and method == "POST" and kw.get("public_fallback", True):
        left = credits_left_today()
        if left is not None and left < cost:  # today's Helius share used up: standard RPC goes to the public node
            url, host, cost = PUBLIC_RPC, urlparse(PUBLIC_RPC).netloc, 0
    kw.pop("public_fallback", None)
    payload = kw.get("json")
    das = group_of(host) == CREDIT_GROUP and any(
        isinstance(it, dict) and it.get("method") in DAS_METHODS
        for it in (payload if isinstance(payload, list) else [payload]))
    rpc_method = payload.get("method") if isinstance(payload, dict) else ("batch" if payload else None)
    pooled = method == "POST" and host in _pool_hosts()
    for attempt in range(retries + 1):
        if pooled:  # any call to a pool endpoint may go to whichever endpoint has room (failover on retry)
            url = pick_public_rpc(priority, method=rpc_method)
            host = urlparse(url).netloc
        throttle(host, cost=cost, das=das, priority=priority, method=rpc_method)
        t_req = time.time()
        try:
            r = _session.request(method, url, timeout=min(timeout, POOL_TIMEOUT) if pooled else timeout, **kw)
        except requests.RequestException as e:
            if pooled:
                _pool_result(host, False, status="error")
            if attempt == retries:
                raise requests.RequestException(redact(e)) from None
            log.warning("%s %s failed (%s), retry", method, host, e)
            time.sleep(min(2 ** attempt, POOL_MAX_SLEEP_429) if pooled else 2 ** attempt)
            continue
        if pooled:
            _pool_result(host, r.status_code < 400, (time.time() - t_req) * 1000, r.status_code)
        if r.status_code == 429 or r.status_code >= 500:
            if attempt == retries:
                raise requests.HTTPError(f"{r.status_code} for {redact(r.url)}")
            delay = float(r.headers.get("Retry-After") or 0) or (2 ** attempt) * 2
            if r.status_code == 429:
                delay = max(delay, MIN_PENALTY_SECONDS.get(host, DEFAULT_MIN_PENALTY))
                if pooled:
                    delay = min(delay, POOL_MAX_SLEEP_429)
                penalize(host, delay)
                log.warning("429 %s %s (slowdown x%.2f, window %.0f%% of %s/10s%s), sleeping %.1fs", method,
                            _endpoint(url, kw), slowdown(host), 100 * _window_load(host) if host in WINDOW_LIMITS
                            else 0, (WINDOW_LIMITS.get(host) or {}).get("total", "-"),
                            ", halved" if host in WINDOW_LIMITS and _halved(host) else "", delay)
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
