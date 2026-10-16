"""Daily Helius credit cap for research, so bulk research never drains the monthly plan that the
event-driven services (signals, safety, sniper, copytrade) depend on.

Costs (Helius free plan): standard RPC call 1 credit, DAS 10, Enhanced API (api.helius.xyz) 100.
Ledger per UTC day in data/research_credits.json, shared by all research processes (merge-on-write).
"""
import json
import os
import threading
import time

from research.config import rsettings

PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "research_credits.json")
COST = {"rpc": 1, "das": 10, "enhanced": 100}
_lock = threading.Lock()
_pending = {}  # kind -> credits not yet written


def _day(ts=None):
    return time.strftime("%Y-%m-%d", time.gmtime(ts or time.time()))


def _read():
    try:
        with open(PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _flush(force=False):
    if not _pending or (not force and sum(_pending.values()) < 50):
        return
    d = _read()
    day = d.setdefault(_day(), {})
    for k, v in _pending.items():
        day[k] = day.get(k, 0) + v
    _pending.clear()
    for old in sorted(d)[:-35]:  # keep about a month
        d.pop(old, None)
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    tmp = PATH + f".{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(d, f)
    os.replace(tmp, PATH)


def used_today():
    with _lock:
        return sum(_read().get(_day(), {}).values()) + sum(_pending.values())


def take(kind, n=1):
    """Book n calls of `kind` if today's research total stays under RESEARCH_HELIUS_CREDITS_PER_DAY."""
    cost = COST[kind] * n
    with _lock:
        today = sum(_read().get(_day(), {}).values()) + sum(_pending.values())
        if today + cost > rsettings.helius_credits_per_day:
            return False
        _pending[kind] = _pending.get(kind, 0) + cost
        _flush()
        return True


def report():
    with _lock:
        _flush(force=True)
        d = _read()
    return dict(today=d.get(_day(), {}), cap=rsettings.helius_credits_per_day,
                last_30d=sum(sum(v.values()) for k, v in d.items() if k >= _day(time.time() - 30 * 86400)))
