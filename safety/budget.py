"""Fixed RPC budget of the safety layer, shared by every process that calls safety (the refresher,
signals and copytrade checking a coin right before a buy).

A token bucket per host in data/ratelimit/safety_<host>.budget (fcntl lock): `per_min` calls per minute,
burst up to `per_min`, and at least `spacing` seconds between two calls. The refresher never waits and
leaves `reserve` calls for the live paths; a check that does not fit is skipped and runs next cycle.
"""
import os
import time

from core.http import _LOCK_DIR


class _State:
    def __init__(self, host):
        os.makedirs(_LOCK_DIR, exist_ok=True)
        self.path = os.path.join(_LOCK_DIR, f"safety_{host}.budget")

    def __enter__(self):
        import fcntl
        self.f = open(self.path, "a+")
        fcntl.flock(self.f, fcntl.LOCK_EX)
        self.f.seek(0)
        parts = self.f.read().split()
        try:
            self.tokens, self.ts, self.last = float(parts[0]), float(parts[1]), float(parts[2])
        except (IndexError, ValueError):
            self.tokens, self.ts, self.last = None, time.time(), 0.0
        return self

    def save(self):
        self.f.seek(0)
        self.f.truncate()
        self.f.write(f"{self.tokens!r} {self.ts!r} {self.last!r}")
        self.f.flush()

    def __exit__(self, *exc):
        import fcntl
        try:
            fcntl.flock(self.f, fcntl.LOCK_UN)
        finally:
            self.f.close()


def take(host, per_min, spacing=0.0, reserve=0.0, wait=0.0):
    """Spend one call on `host`. Returns True, or False when the budget (minus `reserve`) is empty and
    waiting up to `wait` seconds would not help. per_min <= 0 means unlimited."""
    if per_min <= 0:
        return True
    deadline = time.time() + wait
    while True:
        try:
            with _State(host) as s:
                now = time.time()
                rate = per_min / 60.0
                s.tokens = per_min if s.tokens is None else min(per_min, s.tokens + (now - s.ts) * rate)
                s.ts = now
                gap = s.last + spacing - now
                if s.tokens - 1 >= reserve and gap <= 0:
                    s.tokens -= 1
                    s.last = now
                    s.save()
                    return True
                s.save()
                need = max(gap, (1 + reserve - s.tokens) / rate)
        except (ImportError, OSError):
            return True  # no lock files possible: core.http's limiter still applies
        if time.time() + need > deadline:
            return False
        time.sleep(max(0.05, need))


def available(host, per_min):
    """Calls that could be spent right now (for planning a refresh pass)."""
    if per_min <= 0:
        return float("inf")
    try:
        with _State(host) as s:
            if s.tokens is None:
                return float(per_min)
            return min(per_min, s.tokens + (time.time() - s.ts) * per_min / 60.0)
    except (ImportError, OSError):
        return float(per_min)
