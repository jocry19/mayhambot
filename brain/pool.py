"""The pool model: one learner for the opportunities of every source (plan "Richtung neu", 18.10.).

The sources report raw features under shared names; each value becomes a token, once plain (learned across sources)
and once with its source (what is special to that source). Thousands of tokens are expected, so unlike model.Brain
(a full posterior, d x d) this keeps an independent Gaussian per token and plan and updates it by assumed density
filtering: each outcome moves every active weight by its share of the total uncertainty. Same interface as Brain
(predict -> per plan (expected net, std of that expectation), sigma2, update, forget, fade), so decide.choose
draws from it the same way: what is unknown is tried, what is known to lose is tried less, nothing is gated.
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path

from .model import BIAS, CLIP, PRIOR_N, PRIOR_PREC, PRIOR_SIGMA2

PRIOR_V = PRIOR_SIGMA2 / PRIOR_PREC  # weight variance of a token never seen (std ~7 %)
PRIOR_V_BIAS = PRIOR_SIGMA2  # the overall level: unknown at the start, learned within a few dozen outcomes
VERSION = "pool-adf-1"


class Pool:
    def __init__(self, n_plans: int, half_life_h: float = 72.0):
        self.n_plans = n_plans
        self.half_life_h = half_life_h
        self.m: list[dict[str, float]] = [{} for _ in range(n_plans)]
        self.v: list[dict[str, float]] = [{} for _ in range(n_plans)]
        self.ss = [0.0] * n_plans
        self.sw = [0.0] * n_plans
        self.counts: dict[str, float] = {}
        self.n = 0.0
        self.forgot_at = time.time()

    @property
    def d(self) -> int:
        return len(self.counts) + 1

    @staticmethod
    def _pv(tok: str) -> float:
        return PRIOR_V_BIAS if tok == BIAS else PRIOR_V

    def _toks(self, tokens) -> list[str]:
        return [BIAS] + [t for t in dict.fromkeys(tokens) if t != BIAS]

    def sigma2(self, p: int) -> float:
        return (self.ss[p] + PRIOR_SIGMA2 * PRIOR_N) / (self.sw[p] + PRIOR_N)

    def predict(self, tokens) -> list[tuple[float, float]]:
        toks = self._toks(tokens)
        out = []
        for p in range(self.n_plans):
            m, v = self.m[p], self.v[p]
            mean = sum(m.get(t, 0.0) for t in toks)
            var = sum(v.get(t, self._pv(t)) for t in toks)
            out.append((mean, math.sqrt(max(var, 1e-12))))
        return out

    def contributions(self, tokens, p: int) -> list[tuple[str, float, float]]:
        return [(t, self.m[p].get(t, 0.0), self.counts.get(t, 0.0)) for t in self._toks(tokens)[1:]]

    def update(self, tokens, ys: dict[int, float], w: float = 1.0) -> None:
        if not ys or w <= 0:
            return
        toks = self._toks(tokens)
        for p, y in ys.items():
            if y is None:
                continue
            y = max(CLIP[0], min(CLIP[1], float(y)))
            m, v = self.m[p], self.v[p]
            vs = [v.get(t, self._pv(t)) for t in toks]
            mean = sum(m.get(t, 0.0) for t in toks)
            r = y - mean
            s2 = self.sigma2(p)
            S = s2 + w * sum(vs)
            self.ss[p] += w * r * r * s2 / S  # residual scaled to the noise share: honest noise estimate
            self.sw[p] += w
            for t, vt in zip(toks, vs):
                k = w * vt / S
                m[t] = m.get(t, 0.0) + k * r
                v[t] = max(vt * (1 - k), 1e-8)
        self.n += w
        for t in toks[1:]:
            self.counts[t] = self.counts.get(t, 0.0) + w

    def fade(self, lam: float) -> None:
        for p in range(self.n_plans):
            for t in list(self.m[p]):
                self.m[p][t] *= lam
            for t in list(self.v[p]):
                pv = self._pv(t)
                self.v[p][t] = pv - lam * (pv - self.v[p][t])
            self.ss[p] *= lam
            self.sw[p] *= lam
        self.n *= lam
        for t in list(self.counts):
            self.counts[t] *= lam
            if self.counts[t] < 1e-3:  # a pattern gone for weeks: back to the prior, out of the files
                del self.counts[t]
                for p in range(self.n_plans):
                    self.m[p].pop(t, None)
                    self.v[p].pop(t, None)

    def forget(self, now: float | None = None) -> float:
        now = now or time.time()
        dt_h = max(0.0, (now - self.forgot_at) / 3600)
        self.forgot_at = now
        if dt_h <= 0 or self.half_life_h <= 0:
            return 1.0
        lam = 0.5 ** (dt_h / self.half_life_h)
        self.fade(lam)
        return lam

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"version": VERSION, "n_plans": self.n_plans, "half_life_h": self.half_life_h,
                                   "m": self.m, "v": self.v, "ss": self.ss, "sw": self.sw, "counts": self.counts,
                                   "n": self.n, "forgot_at": self.forgot_at}))
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path, n_plans: int, half_life_h: float = 72.0) -> "Pool":
        p = cls(n_plans, half_life_h)
        if not path.exists():
            return p
        try:
            z = json.loads(path.read_text())
            if z.get("n_plans") != n_plans:
                return p
            p.m, p.v, p.ss, p.sw = z["m"], z["v"], z["ss"], z["sw"]
            p.counts, p.n, p.forgot_at = z["counts"], float(z["n"]), float(z["forgot_at"])
        except Exception:
            return cls(n_plans, half_life_h)
        return p
