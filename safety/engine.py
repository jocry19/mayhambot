"""assess() runs the checks, combines them into one risk score and stores the result.

Score: each check gives a risk 0..100 (its reasons combined as independent dangers). The coin's score is
the chance that at least one danger is real: 100 * (1 - prod(1 - risk_i/100)), "noisy OR". A hard flag
(mint/freeze authority, honeypot, sell tax above the limit, rugged, ...) lifts the score to at least
SAFETY_HARD_FLAG_FLOOR and always blocks. Unknown checks add nothing but lower `coverage`.
"""
import logging
import os
import sys
import time
from dataclasses import asdict, dataclass, field

from safety import checks, store
from safety.config import settings as default_settings
from safety.sources import BudgetExceeded, Fetcher

log = logging.getLogger("mayhambot.safety")
VERSION = "safety-v1"
QUICK_NAMES = [fn.__name__.replace("check_", "") for fn in checks.QUICK]
DEEP_NAMES = [fn.__name__.replace("check_", "") for fn in checks.DEEP]
LEVEL_DE = {"low": "niedrig", "caution": "erhöht", "high": "hoch", "critical": "kritisch", "unchecked": "ungeprüft"}


@dataclass
class Assessment:
    mint: str
    ts: int
    score: float
    level: str                       # low | caution | high | critical
    blocked: bool
    coverage: float                  # share of checks that produced an answer
    hard_flags: list = field(default_factory=list)
    reasons: list = field(default_factory=list)      # [{check, risk, text}] worst first
    positives: list = field(default_factory=list)    # ["Mint-Authority abgegeben", ...]
    checks: dict = field(default_factory=dict)       # name -> {label, status, risk, hard, data}
    creator: str | None = None
    pair_created_at: int | None = None
    version: str = VERSION
    calls: int = 0
    error: str | None = None
    override: str | None = None
    deep_ts: int | None = None                       # last time insiders/bundles/dev actually ran

    def to_dict(self):
        d = asdict(self)
        d["level_de"] = LEVEL_DE.get(self.level, self.level)
        return d


def level_for(score, cfg):
    if score >= cfg.block_score:
        return "critical"
    if score >= cfg.high_score:
        return "high"
    if score >= cfg.caution_score:
        return "caution"
    return "low"


def combine(mint, results, cfg, calls=0, override=None):
    done = [r for r in results if r.status == "ok"]
    score = checks.noisy_or(r.risk for r in results)
    hard = [text for r in results if r.hard for risk, text in r.reasons if risk >= 50]
    if hard:
        score = max(score, cfg.hard_flag_floor)
    score = round(score, 1)
    reasons = sorted(({"check": r.name, "risk": risk, "text": text} for r in results for risk, text in r.reasons
                      if text), key=lambda x: -x["risk"])
    blocked = bool(hard) or score >= cfg.block_score
    if override == "block":
        blocked = True
    elif override == "allow":
        blocked = False
    creator = next((r.data.get("creator") for r in results if r.data.get("creator")), None)
    return Assessment(
        mint=mint, ts=int(time.time()), score=score, level=level_for(score, cfg), blocked=blocked,
        coverage=round(len(done) / len(results), 2) if results else 0.0, hard_flags=hard, reasons=reasons,
        positives=[p for r in results for p in r.positives],
        checks={r.name: {"label": r.label, "status": r.status, "risk": r.risk, "hard": r.hard, "data": r.data,
                         "reasons": [list(x) for x in r.reasons], "positives": r.positives} for r in results},
        creator=creator, calls=calls, override=override)


def offline():
    """No network for the safety layer: SAFETY_OFFLINE=1, or inside any pytest run (other packages' tests
    must never reach the real APIs through safety). Tests that want checks pass a fake `fetcher`."""
    return os.getenv("SAFETY_OFFLINE", "").strip().lower() in ("1", "true", "yes", "on") \
        or "PYTEST_CURRENT_TEST" in os.environ or "pytest" in sys.modules


def _run(ctx, fns, mint):
    out = []
    for fn in fns:
        name = fn.__name__.replace("check_", "")
        try:
            out.append(fn(ctx))
        except BudgetExceeded:
            out.append(checks.CheckResult(name, name, status="skipped"))
        except Exception as e:  # noqa: BLE001 - one broken source must not kill the assessment
            log.warning("safety check %s for %s failed: %s", name, mint, e)
            out.append(checks.CheckResult(name, name, status="error", data={"error": str(e)[:200]}))
    return out


def _carried(prev, name):
    """The previous run's result of one check, so a quick re-check or a skipped check keeps its findings."""
    c = (prev.checks or {}).get(name) if prev else None
    if not c or c.get("status") not in ("ok", "unknown"):
        return None
    reasons = [tuple(x) for x in c["reasons"]] if "reasons" in c else \
        [(x["risk"], x["text"]) for x in prev.reasons if x.get("check") == name]
    return checks.CheckResult(name, c.get("label", name), status=c["status"], risk=c.get("risk", 0.0),
                              hard=bool(c.get("hard")), reasons=reasons, positives=list(c.get("positives") or []),
                              data=dict(c.get("data") or {}, carried_from=(c.get("data") or {}).get("carried_from", prev.ts)))


def assess(mint, conn=None, deep=True, force=False, fetcher=None, cfg=None, priority=True):
    """Safety assessment of one token. Uses the stored result while it is fresh (see ttl), unless force=True.
    deep=False runs only the cheap checks (about 4 RPC calls) and keeps the last deep findings (insiders,
    bundles, dev); it turns deep by itself when the top-10 share moved by SAFETY_DEEP_ON_TOP10_CHANGE_PP.
    RPC calls come from a fixed per-minute budget shared by all processes (safety/budget.py); checks that do
    not fit are 'skipped', keep their previous result and run again next cycle. priority=False (the
    refresher) never waits for budget and leaves SAFETY_HELIUS_RESERVE calls to live buys and signals."""
    cfg = cfg or default_settings
    own = conn is None
    conn = store.connect() if own else store.ensure_schema(conn)
    try:
        prev = get(conn, mint, cfg=cfg)
        if not force and prev and not is_due(prev, cfg):
            return prev
        if fetcher is None and offline():
            return prev or Assessment(mint=mint, ts=int(time.time()), score=0.0, level="unchecked", blocked=False,
                                      coverage=0.0, error="offline")
        f = fetcher or Fetcher(cfg, priority=priority)
        ctx = checks.Context(conn, mint, f, cfg, deep=deep, hot=mint in set(hot_mints(conn, (), cfg)))
        results = _run(ctx, checks.QUICK, mint)
        if not deep and prev and prev.deep_ts:
            new = next((r.data.get("top10_pct") for r in results if r.name == "holders" and r.status == "ok"), None)
            old = ((prev.checks.get("holders") or {}).get("data") or {}).get("top10_pct")
            if new is not None and old is not None and abs(new - old) >= cfg.deep_on_top10_change_pp:
                deep = ctx.deep = True
        if deep:
            results += _run(ctx, checks.DEEP, mint)
        deep_ran = deep and any(r.status in ("ok", "unknown") for r in results if r.name in DEEP_NAMES)
        by_name = {r.name: r for r in results}
        for name in QUICK_NAMES + DEEP_NAMES:
            r = by_name.get(name)
            if r is None or r.status in ("skipped", "error"):
                old_r = _carried(prev, name)
                if old_r is not None:
                    by_name[name] = old_r
        results = [by_name[n] for n in QUICK_NAMES + DEEP_NAMES if n in by_name]
        a = combine(mint, results, cfg, calls=getattr(f, "calls", 0), override=store.override(conn, mint))
        a.deep_ts = int(time.time()) if deep_ran else (prev.deep_ts if prev else None)
        pair = ctx._memo.get("pair") or {}
        if pair.get("pairCreatedAt"):
            a.pair_created_at = int(float(pair["pairCreatedAt"]) / 1000)
        elif prev:
            a.pair_created_at = prev.pair_created_at
        store.save_assessment(conn, a)
        return a
    finally:
        if own:
            conn.close()


def _from_row(row):
    import json
    a = Assessment(mint=row["mint"], ts=row["ts"], score=row["score"], level=row["level"], blocked=bool(row["blocked"]),
                   coverage=row["coverage"], hard_flags=json.loads(row["hard_flags_json"] or "[]"),
                   reasons=json.loads(row["reasons_json"] or "[]"), positives=json.loads(row["positives_json"] or "[]"),
                   checks=json.loads(row["checks_json"] or "{}"), creator=row["creator"],
                   pair_created_at=row["pair_created_at"], version=row["version"], calls=row["calls"] or 0,
                   error=row["error"], deep_ts=row["deep_ts"] if "deep_ts" in row.keys() else None)
    return a


def get(conn, mint, cfg=None):
    """Stored assessment (no network), with manual overrides applied, or None if never checked."""
    cfg = cfg or default_settings
    store.ensure_schema(conn)
    row = store.load_row(conn, mint)
    if not row:
        return None
    a = _from_row(row)
    a.override = store.override(conn, mint)
    if a.override == "block":
        a.blocked = True
    elif a.override == "allow":
        a.blocked = False
    return a


def ttl_for(a, cfg):
    if a.blocked and a.override != "allow":
        return cfg.ttl_blocked_sec
    if a.pair_created_at and time.time() - a.pair_created_at < 6 * 3600:
        return cfg.ttl_young_sec
    return cfg.ttl_old_sec


def is_due(a, cfg=None):
    cfg = cfg or default_settings
    incomplete = any(c.get("status") in ("skipped", "error") for c in a.checks.values())
    ttl = ttl_for(a, cfg)
    return time.time() - a.ts >= (ttl / 3 if incomplete else ttl)


# ---------------------------------------------------------------- what other packages call

def is_blocked(conn, mint):
    """True if the copy trader / auto-buyer must not buy this coin. Unchecked coins are not blocked
    (call assess() first if you want to require a check)."""
    store.ensure_schema(conn)
    ov = store.override(conn, mint)
    if ov:
        return ov == "block"
    r = conn.execute("SELECT blocked FROM safety_tokens WHERE mint=?", (mint,)).fetchone()
    return bool(r and r["blocked"])


def blocked_mints(conn):
    store.ensure_schema(conn)
    auto = {r["mint"] for r in conn.execute("SELECT mint FROM safety_tokens WHERE blocked=1")}
    ov = {r["mint"]: r["action"] for r in conn.execute("SELECT mint, action FROM safety_overrides")}
    return (auto | {m for m, a in ov.items() if a == "block"}) - {m for m, a in ov.items() if a == "allow"}


def signal_penalty(conn, mint, cfg=None):
    """Multiplier for the signal score: 1.0 up to SAFETY_CAUTION_SCORE, falling linearly to
    SAFETY_MIN_PENALTY_FACTOR at SAFETY_BLOCK_SCORE, 0.0 when blocked. Unchecked coins get 1.0."""
    cfg = cfg or default_settings
    a = get(conn, mint, cfg)
    if not a:
        return {"factor": 1.0, "risk": None, "level": "unchecked", "blocked": False, "checked": False, "reasons": []}
    if a.blocked:
        factor = 0.0
    elif a.score <= cfg.caution_score:
        factor = 1.0
    else:
        span = max(1.0, cfg.block_score - cfg.caution_score)
        factor = 1.0 - (1.0 - cfg.min_penalty_factor) * min(1.0, (a.score - cfg.caution_score) / span)
    return {"factor": round(factor, 3), "risk": a.score, "level": a.level, "blocked": a.blocked, "checked": True,
            "reasons": [r["text"] for r in a.reasons[:3]], "ts": a.ts}


def block(conn, mint, reason="manuell"):
    store.ensure_schema(conn)
    store.set_override(conn, mint, "block", reason)


def allow(conn, mint, reason="manuell freigegeben"):
    store.ensure_schema(conn)
    store.set_override(conn, mint, "allow", reason)


def clear_override(conn, mint):
    store.ensure_schema(conn)
    store.set_override(conn, mint, None)


def hot_mints(conn, extra=(), cfg=None):
    """Coins that get deep checks: open copy-trading positions, signals of the last
    SAFETY_DEEP_SIGNAL_WINDOW_SEC and `extra` (buy wishes). Everything else is checked in quick mode only."""
    cfg = cfg or default_settings
    since = int(time.time()) - cfg.deep_signal_window_sec
    return list(dict.fromkeys(open_positions(conn) + list(extra) + [
        r[0] for r in conn.execute("SELECT DISTINCT mint FROM signals WHERE ts>=? ORDER BY ts DESC", (since,))]))


def deep_due(a, cfg=None):
    cfg = cfg or default_settings
    return a is None or not a.deep_ts or time.time() - a.deep_ts >= cfg.deep_every_sec


def candidates(conn, extra=(), cfg=None):
    """Coins worth checking, most urgent first: hot coins (open positions, recent signals, `extra`) that are
    unchecked, past the young TTL or due for their deep check, then never-checked coins, then coins due by
    TTL. Sources: ct_positions, signals of the last 24 h, tracked coins, plus `extra`."""
    cfg = cfg or default_settings
    store.ensure_schema(conn)
    since = int(time.time()) - 86400
    hot = hot_mints(conn, extra, cfg)
    mints = list(dict.fromkeys(
        hot
        + [r[0] for r in conn.execute("SELECT DISTINCT mint FROM signals WHERE ts>=? ORDER BY ts DESC", (since,))]
        + [r[0] for r in conn.execute("SELECT mint FROM tokens WHERE tracked=1 ORDER BY last_updated DESC LIMIT 60")]))
    urgent, never, due = [], [], []
    for m in mints:
        a = get(conn, m, cfg)
        if m in hot and (a is None or time.time() - a.ts >= cfg.ttl_young_sec or deep_due(a, cfg)):
            urgent.append(m)
        elif a is None:
            never.append(m)
        elif is_due(a, cfg):
            due.append((a.ts, m))
    return urgent + never + [m for _, m in sorted(due)]


def open_positions(conn):
    """Mints of open copy-trading positions (paper and live). Read-only; [] if copytrade never ran."""
    try:
        return [r[0] for r in conn.execute("SELECT DISTINCT mint FROM ct_positions WHERE status='open' AND mint IS NOT NULL")]
    except Exception:  # noqa: BLE001 - table missing
        return []


QUICK_RPC_CALLS, DEEP_RPC_CALLS = 4, 12   # typical RPC cost of a re-check, for planning a pass


def rpc_room(cfg):
    """RPC calls the refresher may still spend this minute (primary budget above the reserve + public overflow)."""
    from urllib.parse import urlparse
    from core.config import settings as core_settings
    from safety import budget
    host, public = urlparse(core_settings.rpc_url).netloc, urlparse(cfg.public_rpc_url).netloc
    per_min = cfg.helius_per_min if "helius" in host else cfg.public_per_min
    room = budget.available(host, per_min) - cfg.helius_reserve
    if host != public and cfg.public_per_min > 0:
        room += budget.available(public, cfg.public_per_min)
    return room


def refresh(conn=None, extra=(), budget_sec=None, max_tokens=None, cfg=None, fetcher_factory=None):
    """One scheduled pass: deep checks for hot coins whose deep check is due, quick checks for the rest,
    within a time budget and the shared per-minute RPC budget. When the budget cannot cover the next coin
    the pass stops and the rest waits for the next cycle. Run alone with `python -m safety run`."""
    cfg = cfg or default_settings
    own = conn is None
    conn = store.connect() if own else store.ensure_schema(conn)
    budget_s = cfg.refresh_budget_sec if budget_sec is None else budget_sec
    limit = cfg.refresh_max_tokens if max_tokens is None else max_tokens
    t0, done = time.time(), []
    try:
        if fetcher_factory is None and offline():
            return done
        hot = set(hot_mints(conn, extra, cfg))
        for m in candidates(conn, extra, cfg)[:limit]:
            if time.time() - t0 > budget_s:
                break
            deep = m in hot and deep_due(get(conn, m, cfg), cfg)
            need = DEEP_RPC_CALLS if deep else QUICK_RPC_CALLS
            if fetcher_factory is None and rpc_room(cfg) < need:
                log.info("safety: RPC budget for this minute used, %s waits for the next pass", m[:6])
                break
            try:
                f = fetcher_factory(cfg) if fetcher_factory else Fetcher(cfg, priority=False)
                done.append(assess(m, conn, deep=deep, force=True, fetcher=f, cfg=cfg, priority=False))
            except Exception as e:  # noqa: BLE001
                log.warning("safety assess %s failed: %s", m, e)
        return done
    finally:
        if own:
            conn.close()
