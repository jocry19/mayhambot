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
        checks={r.name: {"label": r.label, "status": r.status, "risk": r.risk, "hard": r.hard, "data": r.data}
                for r in results},
        creator=creator, calls=calls, override=override)


def offline():
    """No network for the safety layer: SAFETY_OFFLINE=1, or inside any pytest run (other packages' tests
    must never reach the real APIs through safety). Tests that want checks pass a fake `fetcher`."""
    return os.getenv("SAFETY_OFFLINE", "").strip().lower() in ("1", "true", "yes", "on") \
        or "PYTEST_CURRENT_TEST" in os.environ or "pytest" in sys.modules


def assess(mint, conn=None, deep=True, force=False, fetcher=None, cfg=None):
    """Full safety assessment of one token. Uses the stored result while it is fresh (see ttl), unless
    force=True. Network calls are capped per token (SAFETY_MAX_CALLS_PER_TOKEN); checks that do not fit
    in the budget are marked 'skipped' and re-tried on the next refresh."""
    cfg = cfg or default_settings
    own = conn is None
    conn = store.connect() if own else store.ensure_schema(conn)
    try:
        if not force:
            cached = get(conn, mint, cfg=cfg)
            if cached and not is_due(cached, cfg):
                return cached
        if fetcher is None and offline():
            cached = get(conn, mint, cfg=cfg)
            return cached or Assessment(mint=mint, ts=int(time.time()), score=0.0, level="unchecked", blocked=False,
                                        coverage=0.0, error="offline")
        f = fetcher or Fetcher(cfg)
        ctx = checks.Context(conn, mint, f, cfg, deep=deep)
        results = []
        for fn in checks.QUICK + (checks.DEEP if deep else []):
            name = fn.__name__.replace("check_", "")
            try:
                results.append(fn(ctx))
            except BudgetExceeded:
                results.append(checks.CheckResult(name, name, status="skipped"))
            except Exception as e:  # noqa: BLE001 - one broken source must not kill the assessment
                log.warning("safety check %s for %s failed: %s", name, mint, e)
                results.append(checks.CheckResult(name, name, status="error", data={"error": str(e)[:200]}))
        a = combine(mint, results, cfg, calls=getattr(f, "calls", 0), override=store.override(conn, mint))
        pair = ctx._memo.get("pair") or {}
        if pair.get("pairCreatedAt"):
            a.pair_created_at = int(float(pair["pairCreatedAt"]) / 1000)
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
                   error=row["error"])
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


def candidates(conn, extra=(), cfg=None):
    """Coins worth checking, most urgent first: open copy-trading positions (re-checked every
    SAFETY_TTL_YOUNG_SEC: dev cash-out or LP pull must show up while we hold), then never-checked coins,
    then coins due by TTL. Sources: ct_positions, signals of the last 24 h, tracked coins, plus `extra`."""
    cfg = cfg or default_settings
    store.ensure_schema(conn)
    since = int(time.time()) - 86400
    held = open_positions(conn)
    mints = list(dict.fromkeys(
        held + list(extra)
        + [r[0] for r in conn.execute("SELECT DISTINCT mint FROM signals WHERE ts>=? ORDER BY ts DESC", (since,))]
        + [r[0] for r in conn.execute("SELECT mint FROM tokens WHERE tracked=1 ORDER BY last_updated DESC LIMIT 60")]))
    urgent, never, due = [], [], []
    for m in mints:
        a = get(conn, m, cfg)
        if m in held and (a is None or time.time() - a.ts >= cfg.ttl_young_sec):
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


def refresh(conn=None, extra=(), budget_sec=None, max_tokens=None, cfg=None, fetcher_factory=None):
    """One scheduled pass: assess the most urgent coins within a time budget. Safe to call from any
    service loop (research, signals) or run alone with `python -m safety run`."""
    cfg = cfg or default_settings
    own = conn is None
    conn = store.connect() if own else store.ensure_schema(conn)
    budget = cfg.refresh_budget_sec if budget_sec is None else budget_sec
    limit = cfg.refresh_max_tokens if max_tokens is None else max_tokens
    t0, done = time.time(), []
    try:
        for m in candidates(conn, extra, cfg)[:limit]:
            if time.time() - t0 > budget:
                break
            if fetcher_factory is None and offline():
                break
            try:
                f = fetcher_factory(cfg) if fetcher_factory else None
                done.append(assess(m, conn, force=True, fetcher=f, cfg=cfg))
            except Exception as e:  # noqa: BLE001
                log.warning("safety assess %s failed: %s", m, e)
        return done
    finally:
        if own:
            conn.close()
