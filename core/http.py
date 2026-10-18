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
_LOCK_DIR = os.getenv("MAYHAM_RATELIMIT_DIR") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "ratelimit")


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
# Helius free plan = 10 req/s = 600/min; these add up to 570/min, and the shared interval smooths peaks.
# 18.10. by measured need: signals 120 (backfill waited 418 s in 10 min at 60), research 30 (a few calls a day),
# copytrade 180 (whale reads Helius-first, peaks over 120 spilled to mainnet-beta 429s), brain 60 (measured 6-10).
DEFAULT_BUDGETS = {
    "helius": {"signals": 120, "research": 30, "copytrade": 180, "safety": 60, "sniper": 120, "brain": 60},
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
    line = (f", credits today {st['credits_today']:.0f}/{st['credits_day_budget']:.0f}, month "
            f"{st['credits_month']:.0f}/{st['credits_cap']:.0f} ({st['share_pct']:.0f}% share)")
    try:
        p = plan_pace()
        line += f", plan {p['used']:.0f}/{p['on_pace']:.0f} on pace (x{p['ratio']:.2f})"
    except Exception:  # noqa: BLE001 - the usage line never fails over the pace
        pass
    return line


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
    for h, methods in method_stats().items():  # since process start
        log.info("rpc pool %s: %s", h, "; ".join(f"{m} {v['calls']} calls, {v['429']} 429, {v['avoided']} avoided"
                                                for m, v in methods.items()))
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
# brain: every paper buy needs a rug verdict (getTokenLargestAccounts), which the public nodes refuse in long 429
# stretches; with no share of its own it only borrowed and fell through to mainnet-beta. Research spends little.
# 18.10.: signals 12.5 -> 7.5 (its backfill is capped, see signals/run.py), brain 5 -> 10 for the learning account
DEFAULT_CREDIT_SHARES = {"research": 45, "signals": 7.5, "safety": 12.5, "copytrade": 12.5, "sniper": 12.5,
                         "brain": 10}
# Services that react to events may run ahead of their even daily share (still never past their month share).
CREDIT_BURST = {"safety": 1.5, "sniper": 1.5, "brain": 1.5}
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
    path = _ledger_path()
    # lock on a separate file and replace the ledger atomically: truncating and rewriting it in place left an empty
    # or half-written file when a process was killed mid-write, and the next reader started the month from zero
    with open(path + ".lock", "a+") as lk:
        try:
            import fcntl
            fcntl.flock(lk, fcntl.LOCK_EX)
        except ImportError:
            pass
        led = None
        for src in (path, path + ".bak"):
            try:
                with open(src) as f:
                    led = json.loads(f.read() or "{}")
                break
            except FileNotFoundError:
                continue
            except ValueError:
                log.warning("credit ledger %s unreadable, trying the backup", src)
        led = _roll(led or {}, write=True)
        res = fn(led)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w") as f:
            json.dump(led, f)
            f.flush()
            os.fsync(f.fileno())
        if os.path.exists(path):
            try:  # a copy, not a rename: readers without the lock (dashboard, ops) always find the ledger
                import shutil
                shutil.copyfile(path, path + ".bak")
            except OSError:
                pass
        os.replace(tmp, path)
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
    warn_level (share of the month used, 0..1), warn (>= 80 %), history [{month, key_fp, used, until},
    cap_at / cap_at_no_stream: unix time the plan runs out at today's rate, all services / without sniper's
    stream (None = not this month)]}."""
    led = _ledger_read()
    out = _status(led)
    out.update(cap_forecast(led, out))
    return out


def cap_forecast(led=None, status=None):
    """When the month's credits run out at today's rate (the ledger's day is UTC): {cap_at, cap_at_no_stream}.
    The calendar ratio alone hid a day that burnt ten times the days before (18.10.)."""
    led = _ledger_read() if led is None else led
    st = status or _status(led)
    day = _now_parts()[1]
    per = {s: (v.get("days") or {}).get(day, 0) for s, v in led.get("services", {}).items()}
    now = time.time()
    hours = (now % 86400) / 3600

    def cap(spent):
        if not (st.get("limit") and spent > 0 and hours >= 1):
            return None
        left_h = max(0.0, st["limit"] - st.get("used", 0)) / (spent / hours)
        return now + left_h * 3600 if left_h < 31 * 24 else None
    today = sum(per.values())
    return {"cap_at": cap(today), "cap_at_no_stream": cap(today - per.get("sniper", 0))}


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


def daily_limit_on():
    """18.10. the user: Helius may be used as much as the bot needs, they watch the plan and the key themselves.
    So the per-service day shares only book and show usage; they refuse calls (CreditsExhausted, public
    fallback) only with HELIUS_DAILY_LIMIT=1. Per-second limits and 429 handling stay either way."""
    return (os.getenv("HELIUS_DAILY_LIMIT") or "").strip().lower() in ("1", "true", "yes", "on")


def plan_used_up():
    """The month's Helius credits (HELIUS_CREDITS_MONTH, all services, this key) are spent per the ledger."""
    st = _status(_ledger_read())
    return bool(st.get("limit")) and st.get("used", 0) >= st["limit"]


def helius_paused(service=None):
    """HELIUS_PAUSE=signals,sniper (the user's switch, nothing sets it on its own): those services leave Helius
    as if their share were used up, standard RPC goes to the public pool, DAS/Enhanced raise CreditsExhausted,
    own transactions (TX_METHODS) keep Helius. Order if the plan gets tight (18.10.): pause sniper's stream and
    signals first, research and brain next; safety and copytrade (whale detection) last."""
    svc = service if service is not None else _service
    return bool(svc) and svc in {x.strip() for x in (os.getenv("HELIUS_PAUSE") or "").split(",")}


def credits_left_today(service=None):
    """Credits this service may still spend today (None = no limit, e.g. a process without a service name;
    inf while HELIUS_DAILY_LIMIT is off, so callers comparing it with a reserve keep the Helius lane)."""
    svc = service if service is not None else _service
    if not svc:
        return None
    if helius_paused(svc):
        return 0.0
    if not daily_limit_on():
        # free use, but once the ledger says the month is used up every service goes public by itself
        # instead of hammering a key Helius now refuses (own transactions keep Helius: TX_METHODS)
        return 0.0 if plan_used_up() else float("inf")
    st = credits(svc)
    if not st or st["credits_day_budget"] is None:
        return None
    day_left = st["credits_day_budget"] * CREDIT_BURST.get(svc, 1.0) - st["credits_today"]
    month_left = st["credits_cap"] - st["credits_month"]
    return max(0.0, min(day_left, month_left), _borrowable(_ledger_read(), svc))


def _borrow_pct():
    try:
        return max(0.0, float(os.getenv("HELIUS_BORROW_PCT") or 50))
    except ValueError:
        return 50.0


def _borrowable(led, svc):
    """Credits `svc` may still take today beyond its own share, lent from what the other services leave
    unused today (HELIUS_BORROW_PCT of it, default 50 %; 0 turns lending off). Only while the whole plan
    is not ahead of the calendar (used <= usable * day/days of month), so the lenders' credits stay there."""
    pct = _borrow_pct()
    if pct <= 0:
        return 0.0
    _, _, days_in_month, mday = _now_parts()
    credits_month_total, reserve_pct, shares = _credit_cfg()
    usable = credits_month_total * (1 - reserve_pct / 100)
    services = set(led.get("services", {})) | set(shares)
    lend = borrowed = 0.0
    month_all = 0.0
    for s in services:
        st = _svc_stats(led, s)
        month_all = st["credits_month_all"]
        budget = st["credits_day_budget"]
        own = (budget or 0.0) * CREDIT_BURST.get(s, 1.0)
        borrowed += max(0.0, st["credits_today"] - own)
        if s != svc and budget:
            lend += max(0.0, budget - st["credits_today"])
    pace_left = usable * mday / days_in_month - month_all
    return max(0.0, min(lend * pct / 100 - borrowed, pace_left))


def plan_pace():
    """Whole Helius plan against the calendar: {used, on_pace (usable * day/days of month), ratio = used /
    on_pace}. ratio > 1 = the month runs ahead of its plan. Order when that happens (18.10.): bulk consumers
    step back first (sniper stream 2 to the public RPCs), whale detection (copytrade) and the rug check
    (safety, brain) keep Helius."""
    led = _ledger_read()
    _, _, days_in_month, mday = _now_parts()
    credits_month_total, reserve_pct, _ = _credit_cfg()
    on_pace = credits_month_total * (1 - reserve_pct / 100) * mday / days_in_month
    used = _status(led).get("used", 0.0)
    return {"used": used, "on_pace": on_pace, "ratio": used / on_pace if on_pace else 0.0}


_credit_warned = set()


def _charge(cost, force=False):
    """Book `cost` credits for this service; raise CreditsExhausted when today's share is used up
    (force=True books without refusing: sending and confirming an own transaction never waits for tomorrow)."""
    if cost <= 0:
        return
    svc = _service or "other"

    def book(led):
        st = _svc_stats(led, svc)
        if _service and not force and (helius_paused() or (not daily_limit_on() and st["credits_month_all"]
                                                          >= st["credits_month_limit"])):
            return st, False
        if _service and st["credits_day_budget"] is not None and not force and daily_limit_on():
            day_left = st["credits_day_budget"] * CREDIT_BURST.get(svc, 1.0) - st["credits_today"]
            if cost > min(day_left, st["credits_cap"] - st["credits_month"]) and cost > _borrowable(led, svc):
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


# Sending and confirming an own transaction: always the configured (Helius) RPC, booked even when the day's
# share is used up and never held back by the per-minute budget; a public node lands transactions worse.
TX_METHODS = {"sendTransaction", "simulateTransaction", "getSignatureStatuses", "getLatestBlockhash"}


def rpc_url(method="", helius_url=None):
    """RPC endpoint for a JSON-RPC `method`: the Helius URL while this service has credits left today,
    else the public RPC for standard methods (DAS / Helius-only methods keep Helius and may raise;
    TX_METHODS always keep Helius)."""
    url = helius_url or os.getenv("SOLANA_RPC_URL") or ""
    if group_of(urlparse(url).netloc) != CREDIT_GROUP:
        return url or pick_public_rpc()
    if method in TX_METHODS:
        return url
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
    tx = method in TX_METHODS
    c = credit_cost(f"https://{host}/") if cost is None else cost
    _charge(c, force=True) if tx else _charge(c)
    if not tx:  # an own transaction never queues behind this service's wallet polling
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
# An endpoint can answer one method and refuse another for long stretches (api.mainnet-beta: getTransaction 429
# all day, getSignaturesForAddress fine). Each 429 for (endpoint, method) keeps that method away from it for
# METHOD_AVOID_BASE * 2^(n-1) s (capped), shared by all services. n is a strike score: +1 per 429, times
# METHOD_AVOID_DECAY per success, so an endpoint that refuses a method every few calls stays away for longer
# instead of being reset by each odd success (17.10.: getTransaction on mainnet-beta banned and freed
# thousands of times). A success never lifts a running ban; it runs out on its own.
METHOD_AVOID_BASE = 10
METHOD_AVOID_MAX = 600
METHOD_AVOID_DECAY = 0.75
# When a long ban (strike score >= METHOD_PROBE_FROM) runs out, every queued call of every service used to hit
# the endpoint at once (18.10.: ~30 getTransaction 429s per 600-s ban on mainnet-beta). Now the first caller
# claims one probe and the others stay away METHOD_PROBE_S more; each probe takes a notch off the shared score.
METHOD_PROBE_FROM = 3
METHOD_PROBE_S = 10
_method_429 = {}    # (host, method) -> strike score (this process)
_method_stats = {}  # (host, method) -> {"calls", "429", "avoided"} (this process)


def _mstat(host, method, key):
    d = _method_stats.setdefault((host, method), {"calls": 0, "429": 0, "avoided": 0})
    d[key] += 1


def method_stats():
    """{host: {method: {calls, 429, avoided}}} of this process: how often each endpoint was used, refused and
    skipped for each RPC method."""
    out = {}
    for (h, m), v in sorted(_method_stats.items()):
        out.setdefault(h, {})[m] = dict(v)
    return out


def _avoid_until(st, host, method):
    return (st.get(host, {}).get("avoid") or {}).get(method, 0) if method else 0


def _claim_probe(host, method, now):
    """True for the one caller that may try `host` for `method` after a long ban ran out."""
    won = []

    def fn(st):
        h = st.setdefault(host, {})
        av, strikes = h.setdefault("avoid", {}), h.setdefault("strikes", {})
        if av.get(method, 0) > now:
            return
        won.append(1)
        av[method] = now + METHOD_PROBE_S
        if strikes.get(method, 0) - 1 < METHOD_PROBE_FROM:
            strikes.pop(method, None)
        else:
            strikes[method] -= 1
    _pool_edit(fn)
    return bool(won)


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
    _pool_edit(lambda st: st.setdefault(host, {}).update(fields))


def _pool_edit(fn):
    """Change the shared pool state under its file lock (fn mutates the dict in place)."""
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
            fn(st)
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
        if any(k[0] == h for k in _method_stats):
            d["methods"] = method_stats()[h]
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
    if method and method != "batch" and len(urls) > 1:
        st, now = _pool_state(), time.time()
        free = [u for u in urls if _avoid_until(st, urlparse(u).netloc, method) <= now]
        for u in [u for u in free if (st.get(urlparse(u).netloc, {}).get("strikes") or {}).get(method, 0)
                  >= METHOD_PROBE_FROM]:
            if _claim_probe(urlparse(u).netloc, method, now):
                return u   # the claimed probe goes out now, or the window is lost for everyone
            free.remove(u)
        for u in urls:
            if u not in free:
                _mstat(urlparse(u).netloc, method, "avoided")
        # every endpoint refuses this method right now: the one whose backoff ends first
        urls = free or [min(urls, key=lambda u: _avoid_until(st, urlparse(u).netloc, method))]
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


def _pool_result(host, ok, ms=None, status=None, method=None):
    """Book a pool call. 429 = back off (shared slowdown in _request, plus this endpoint's window halved for
    HALVE_SECONDS), never a reason to drop it. Timeouts / 5xx: POOL_FAILS_TO_DISABLE in a row take it out for
    POOL_COOLDOWN, unless it is the last healthy endpoint."""
    if method:
        _mstat(host, method, "calls")
    if ok:
        _pool_fail[host] = 0
        if method and _method_429.get((host, method)):
            n = _method_429[(host, method)] * METHOD_AVOID_DECAY
            _method_429[(host, method)] = n if n >= 0.25 else 0
        if ms is not None:
            _latency[host] = ms if host not in _latency else 0.8 * _latency[host] + 0.2 * ms
            _count(host, "latency_ms_sum", ms)
        return
    if status == 429:
        until = time.time() + HALVE_SECONDS
        _pool_update(host, halved_until=until)
        _halved_cache[host] = (time.time(), until)
        if method and method != "batch":
            _mstat(host, method, "429")
            n = _method_429[(host, method)] = _method_429.get((host, method), 0) + 1
            away = round(min(METHOD_AVOID_BASE * 2 ** (n - 1), METHOD_AVOID_MAX))

            def ban(st):
                h = st.setdefault(host, {})
                h.setdefault("avoid", {})[method] = time.time() + away
                if n >= METHOD_PROBE_FROM:
                    sk = h.setdefault("strikes", {})
                    sk[method] = max(sk.get(method, 0), int(n))
            _pool_edit(ban)
            if any(n - 1 < k <= n for k in (3, 6)) or away == METHOD_AVOID_MAX:
                log.info("public RPC %s refuses %s (strike score %.1f), other endpoints for %d s",
                         host, method, n, away)
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
    if cost == 1 and method == "POST" and kw.get("public_fallback", True) \
            and not (isinstance(kw.get("json"), dict) and kw["json"].get("method") in TX_METHODS):
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
    rate_limited = False
    for attempt in range(retries + 1):
        if pooled:  # any call to a pool endpoint may go to whichever endpoint has room (failover on retry)
            url = pick_public_rpc(priority, method=rpc_method)
            host = urlparse(url).netloc
        # a 429 is not billed, so the retry after one does not book the credits a second time
        throttle(host, cost=0 if rate_limited and cost else cost, das=das, priority=priority, method=rpc_method)
        rate_limited = False
        t_req = time.time()
        try:
            r = _session.request(method, url, timeout=min(timeout, POOL_TIMEOUT) if pooled else timeout, **kw)
        except requests.RequestException as e:
            if pooled:
                _pool_result(host, False, status="error", method=rpc_method)
            if attempt == retries:
                raise requests.RequestException(redact(e)) from None
            log.warning("%s %s failed (%s), retry", method, host, e)
            time.sleep(min(2 ** attempt, POOL_MAX_SLEEP_429) if pooled else 2 ** attempt)
            continue
        if pooled:
            _pool_result(host, r.status_code < 400, (time.time() - t_req) * 1000, r.status_code, rpc_method)
        if r.status_code == 429 or r.status_code >= 500:
            if attempt == retries:
                raise requests.HTTPError(f"{r.status_code} for {redact(r.url)}")
            try:
                delay = float(r.headers.get("Retry-After") or 0) or (2 ** attempt) * 2
            except ValueError:  # Retry-After as an HTTP date
                delay = (2 ** attempt) * 2
            if r.status_code == 429:
                rate_limited = True
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
