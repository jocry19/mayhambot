"""One decision: which exit plan, whether to enter, how much, and the trader's note explaining it."""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

from . import features as F
from .plans import PLANS


@dataclass
class Decision:
    enter: bool
    plan: str
    mean: float
    std: float
    p_pos: float  # how sure the brain is that this plan's average is positive
    sample: float
    explore: bool
    size_sol: float
    why_not: str = ""
    means: list = field(default_factory=list)
    pros: list = field(default_factory=list)
    cons: list = field(default_factory=list)


def _phi(z: float) -> float:
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


PLAN_SHRINK = 0.5  # plan-specific deviations from the plans' average count half: the best of six noisy estimates
#                   is biased upward (winner's curse); simulated on synthetic data this halved the overconfidence


def verdict(preds, s) -> tuple[int, float, float, bool]:
    """(best plan, its shrunk expected return, P(that > 0), convinced?) from the model's per-plan predictions."""
    avg = sum(m for m, _ in preds) / len(preds)
    adj = [avg + PLAN_SHRINK * (m - avg) for m, _ in preds]
    best = max(range(len(preds)), key=lambda k: adj[k])
    sd = preds[best][1]
    p = _phi(adj[best] / sd) if sd > 0 else float(adj[best] > 0)
    return best, adj[best], p, adj[best] > s.min_edge and p >= s.min_conf


def choose(model, kind: str, feats: dict, s, rng: random.Random | None = None,
           equity: float | None = None) -> Decision:
    """Pick the exit plan and decide.

    Convinced: the chosen plan's expected net return is positive and the brain is at least BRAIN_MIN_CONF sure of
    that; the size grows from the minimum at that certainty to the maximum at 95 %.
    Exploring (Thompson sampling): not convinced, but one draw from the posterior says it could pay, or the best
    plan is at least BRAIN_EXPLORE_MIN_P likely to be positive; bought with the minimum size, except in the segments
    BRAIN_NO_EXPLORE names. Everything else is passed (and still learned from)."""
    rng = rng or random
    toks = F.tokens(kind, feats)
    preds = model.predict(toks)
    avg = sum(m for m, _ in preds) / len(preds)
    adj = [avg + PLAN_SHRINK * (m - avg) for m, _ in preds]
    # one draw of "how lucky is this context" shared by all plans: their errors move together (same coin, same
    # moment); independent draws would let the best of six lucky draws through far too often
    z = rng.gauss(0, 1)
    draws = [m + z * sd for m, (_, sd) in zip(adj, preds)]
    best, _, p_best, convinced = verdict(preds, s)
    i = best if convinced else max(range(len(PLANS)), key=lambda k: draws[k])
    mean, sd = adj[i], preds[i][1]
    p_pos = _phi(mean / sd) if sd > 0 else float(mean > 0)
    # paper learns from its own fills: also try what is merely plausible (P(best plan > 0) >= BRAIN_EXPLORE_MIN_P)
    explore = not convinced and (draws[i] > s.min_edge or p_best >= s.explore_min_p)
    cold = next((f"{k}:{v}" for k, v in feats.items() if f"{k}:{v}" in s.no_explore_set), "")
    if explore and cold:  # a segment that loses at entry: only a convinced buy, the replay still learns it
        explore = False
    enter = convinced or explore
    frac = max(0.0, min(1.0, (p_pos - s.min_conf) / max(1e-6, 0.95 - s.min_conf)))
    size = round(s.min_size_sol + (s.max_size_sol - s.min_size_sol) * frac, 3) if convinced else s.min_size_sol
    if convinced and equity and equity > 0 and s.kelly_frac > 0:
        # compounding (user, 10/2026: the account should double fast): a convinced buy stakes a fraction of the
        # account by the Kelly rule, edge / variance of one trade's outcome, scaled down (BRAIN_KELLY_FRAC) because
        # the edge is an estimate; never more than BRAIN_MAX_STAKE of the account, never less than the minimum
        # the model's own doubt about the edge adds to the outcome's variance (Bayesian Kelly): an unsure edge stakes
        # less by itself, without a threshold
        kelly = s.kelly_frac * mean / max(model.sigma2(i) + sd * sd, 1e-4)
        size = round(max(s.min_size_sol, min(s.max_stake * equity, kelly * equity)), 3)
    elif explore and equity and equity > 0 and s.explore_share > 0:
        size = round(max(s.min_size_sol, s.explore_share * equity), 3)
    contrib = sorted(model.contributions(toks, i), key=lambda c: c[1])
    pros = [c for c in reversed(contrib) if c[1] > 0.004][:3]
    cons = [c for c in contrib if c[1] < -0.004][:3]
    why_not = "" if enter else (f"bester Plan im Schnitt {adj[best]:+.1%} netto, sicher im Plus nur zu {p_best:.0%}; "
                                + (f"keine Erkundung bei {F.LABELS.get(cold.split(':')[0], cold.split(':')[0])} "
                                   f"{cold.split(':', 1)[1]} (verliert laut Daten)" if cold else "lohnt nicht"))
    return Decision(enter, PLANS[i].name, mean, sd, p_pos, draws[i], explore, size, why_not,
                    [round(m, 4) for m in adj], pros, cons)


def _pct(x) -> str:
    return "?" if x is None else f"{x:+.0%}"


def headline(kind: str, raw: dict, symbol: str) -> str:
    sym = symbol or "?"
    if kind == "whale":
        who = f"Wal {raw['wallet'][:6]}… ({raw.get('tier') or 'none'}" + (", Cluster" if raw.get("cluster") else "") + ")"
        t = f"{who} kaufte {raw.get('sol', 0):.1f} SOL {sym}, erkannt nach {raw.get('delay_s', 0):.0f} s"
        if raw.get("run") is not None:
            t += f", Preis seither {_pct(raw['run'])}"
        if raw.get("crowd"):
            t += f", {raw['crowd']} weitere Smart Wallets im Coin"
        return t + "."
    if kind == "signal":
        t = (f"Signal{' v2' if raw.get('sig') == 'v2' else ''} {sym}: Score {raw.get('score') or 0:.0f}, {raw.get('buyers') or 0} Smart Wallets, "
             f"${(raw.get('net_usd') or 0):,.0f} netto")
        if raw.get("ev") is not None:
            t += f", EV {raw['ev']:+.1%}"
        if raw.get("call") and raw["call"] > 1:
            t += f", Call Nr. {raw['call']}"
        return t + "."
    if kind == "pool":
        t = f"{raw.get('source') or '?'} meldet {sym}"
        if raw.get("action"):
            t += f" ({raw['action']}{': ' + str(raw['reason']) if raw.get('reason') else ''})"
        if raw.get("score") is not None:
            t += f", Score {raw['score']:.2f}"
        return t + f", {raw.get('delay_s', 0):.0f} s nach dem Ereignis."
    if kind == "market":
        t = (f"Markt-Coin {sym} ({raw.get('dex') or '?'}): {raw.get('smart') or 0} Smart-Käufer, Kurs 5 min "
             f"{(raw.get('pc_m5') or 0):+.0f} %, 1 h {(raw.get('pc_h1') or 0):+.0f} %, {raw.get('txns_m5') or 0} Trades in 5 min")
        if raw.get("dip15") is not None:
            t += f", 15 min davor {_pct(raw['dip15'])}"
        return t + "."
    t = f"Launch {sym}: Sniper-Score {raw.get('score') or 0:.0f}, Risiko {raw.get('risk') or 0:.0f}"
    if raw.get("reasons"):
        t += f" ({'; '.join(str(r) for r in raw['reasons'])})"
    return t + "."


def note(kind: str, raw: dict, symbol: str, d: Decision, extra: str = "") -> str:
    """2 to 4 lines as a trader writes them in the notebook: what happened, what speaks for and against it, what the
    brain expects, what it does."""
    from .plans import BY_NAME

    lines = [headline(kind, raw, symbol)]
    pro = ", ".join(f"{F.describe(t)} ({e:+.1%})" for t, e, _ in d.pros)
    con = ", ".join(f"{F.describe(t)} ({e:+.1%})" for t, e, _ in d.cons)
    if pro or con:
        lines.append("Dafür: " + (pro or "nichts Belastbares") + ". Dagegen: " + (con or "nichts Belastbares") + ".")
    p = BY_NAME[d.plan]
    lines.append(f"Erwartung mit „{p.label}“ ({p.text()}): Ø {d.mean:+.1%} netto ± {d.std:.1%}, "
                 f"sicher im Plus zu {d.p_pos:.0%}.")
    if d.enter:
        lines.append(f"Kauf {d.size_sol:.2f} SOL" + (" als Erkundung: unsicher, ob es sich lohnt, klein testen."
                                                     if d.explore else "." ) + (f" {extra}" if extra else ""))
    else:
        lines.append("Ausgelassen: " + (extra or d.why_not) + ".")
    return "\n".join(lines)
