"""Online Bayesian model of the net return of every exit plan given the context.

One Bayesian linear regression per plan over sparse 0/1 context tokens ("tier=ultra", "whale:wash=>30%", ...).
Every outcome updates the posterior at once (no versions); old evidence fades with a half-life, so the model follows
the market. The posterior gives a mean AND an uncertainty per decision: Thompson sampling uses both (explores where
it does not know yet, exploits where it does), and the size scales with how sure the brain is that the mean is
positive. Tokens the model has never seen add their prior uncertainty instead of being ignored.
"""
from __future__ import annotations

import io
import json
import math
import time
from pathlib import Path

import numpy as np

BIAS = "bias"
PRIOR_PREC_BIAS = 1.0
PRIOR_PREC = 20.0  # weight std ~ noise std / sqrt(20) (~7 %): one token moves the expectation only with evidence
PRIOR_SIGMA2 = 0.09  # memecoin trades: ~30 % std of the net return per trade
PRIOR_N = 10.0
CLIP = (-1.0, 2.0)  # one rug is -100 %, one moonshot must not carry the model


class Brain:
    def __init__(self, n_plans: int, half_life_h: float = 120.0):
        self.n_plans = n_plans
        self.half_life_h = half_life_h
        self.vocab: dict[str, int] = {BIAS: 0}
        self.counts: dict[str, float] = {}
        d = 1
        self.prior = np.array([PRIOR_PREC_BIAS])
        self.A = np.stack([np.diag(self.prior) for _ in range(n_plans)])
        self.b = np.zeros((n_plans, d))
        self.ss = np.zeros(n_plans)  # weighted squared residuals (noise estimate)
        self.sw = np.zeros(n_plans)
        self.n = 0.0  # weighted outcomes seen (all plans together count once)
        self.forgot_at = time.time()
        self._cache: dict = {}

    # ------------------------------------------------------------------ vocabulary
    @property
    def d(self) -> int:
        return len(self.vocab)

    def _grow(self, new: list[str]) -> None:
        d0 = self.d
        for t in new:
            self.vocab[t] = len(self.vocab)
        k = self.d - d0
        A = np.zeros((self.n_plans, self.d, self.d))
        A[:, :d0, :d0] = self.A
        add = np.full(k, PRIOR_PREC)
        for p in range(self.n_plans):
            A[p, d0:, d0:] = np.diag(add)
        self.A = A
        self.b = np.concatenate([self.b, np.zeros((self.n_plans, k))], axis=1)
        self.prior = np.concatenate([self.prior, add])
        self._cache.clear()

    def index(self, tokens, grow: bool = False) -> tuple[list[int], int]:
        """(indices incl. bias, number of tokens the model has never seen)."""
        toks = list(dict.fromkeys(tokens))
        if grow:
            new = [t for t in toks if t not in self.vocab]
            if new:
                self._grow(new)
        idx = [0] + [self.vocab[t] for t in toks if t in self.vocab and t != BIAS]
        return idx, sum(1 for t in toks if t not in self.vocab)

    # ------------------------------------------------------------------ posterior
    def sigma2(self, p: int) -> float:
        return float((self.ss[p] + PRIOR_SIGMA2 * PRIOR_N) / (self.sw[p] + PRIOR_N))

    def _inv(self, p: int) -> np.ndarray:
        if p not in self._cache:
            self._cache[p] = np.linalg.inv(self.A[p])
        return self._cache[p]

    def weights(self, p: int) -> np.ndarray:
        return self._inv(p) @ self.b[p]

    def predict(self, tokens) -> list[tuple[float, float]]:
        """Per plan: (expected net return, std of that expectation)."""
        idx, unseen = self.index(tokens)
        out = []
        for p in range(self.n_plans):
            inv = self._inv(p)
            w = inv[idx] @ self.b[p]
            s2 = self.sigma2(p)
            var = s2 * float(inv[np.ix_(idx, idx)].sum()) + s2 * unseen / PRIOR_PREC
            out.append((float(w.sum()), math.sqrt(max(var, 1e-12))))
        return out

    def contributions(self, tokens, p: int) -> list[tuple[str, float, float]]:
        """(token, effect on the expected return of plan p, evidence weight) for every known token but the bias."""
        idx, _ = self.index(tokens)
        w = self.weights(p)
        inv_vocab = {i: t for t, i in self.vocab.items()}
        return [(inv_vocab[i], float(w[i]), self.counts.get(inv_vocab[i], 0.0)) for i in idx[1:]]

    # ------------------------------------------------------------------ learning
    def forget(self, now: float | None = None) -> float:
        """Fade old evidence: posterior precision and data move toward the prior with the half-life."""
        now = now or time.time()
        dt_h = max(0.0, (now - self.forgot_at) / 3600)
        self.forgot_at = now
        if dt_h <= 0 or self.half_life_h <= 0:
            return 1.0
        lam = 0.5 ** (dt_h / self.half_life_h)
        P = np.diag(self.prior)
        self.A = P + lam * (self.A - P)
        self.b *= lam
        self.ss *= lam
        self.sw *= lam
        self.n *= lam
        for k in self.counts:
            self.counts[k] *= lam
        self._cache.clear()
        return lam

    def update(self, tokens, ys: dict[int, float], w: float = 1.0) -> None:
        """Learn one outcome: ys = {plan index: net return}. Plans without a value are left alone."""
        if not ys or w <= 0:
            return
        idx, _ = self.index(tokens, grow=True)
        ix = np.ix_(idx, idx)
        for p, y in ys.items():
            if y is None:
                continue
            y = max(CLIP[0], min(CLIP[1], float(y)))
            inv = self._inv(p)
            r = y - float((inv[idx] @ self.b[p]).sum())  # residual before the update: honest noise estimate
            self.ss[p] += w * r * r
            self.sw[p] += w
            self.A[p][ix] += w
            self.b[p, idx] += w * y
            u = inv[:, idx].sum(axis=1)  # Sherman-Morrison: the inverse follows the rank-1 update in O(d^2)
            inv -= np.outer(u, u) * (w / (1.0 + w * float(u[idx].sum())))
        self.n += w
        for tok in dict.fromkeys(tokens):
            if tok in self.vocab and tok != BIAS:
                self.counts[tok] = self.counts.get(tok, 0.0) + w

    # ------------------------------------------------------------------ persistence
    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        buf = io.BytesIO()
        meta = {"vocab": self.vocab, "counts": self.counts, "n": self.n, "forgot_at": self.forgot_at,
                "half_life_h": self.half_life_h, "n_plans": self.n_plans}
        np.savez_compressed(buf, A=self.A.astype(np.float64), b=self.b, ss=self.ss, sw=self.sw, prior=self.prior,
                            meta=np.frombuffer(json.dumps(meta).encode(), dtype=np.uint8))
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(buf.getvalue())
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path, n_plans: int, half_life_h: float) -> "Brain":
        m = cls(n_plans, half_life_h)
        if not path.exists():
            return m
        try:
            z = np.load(path, allow_pickle=False)
            meta = json.loads(bytes(z["meta"]).decode())
            if meta.get("n_plans") != n_plans:
                return m  # plans changed: start over (the warm start rebuilds the prior knowledge)
            m.vocab = {k: int(v) for k, v in meta["vocab"].items()}
            m.counts = {k: float(v) for k, v in meta["counts"].items()}
            m.n, m.forgot_at = float(meta["n"]), float(meta["forgot_at"])
            m.A, m.b, m.ss, m.sw, m.prior = z["A"], z["b"], z["ss"], z["sw"], z["prior"]
        except Exception:
            return cls(n_plans, half_life_h)
        return m
