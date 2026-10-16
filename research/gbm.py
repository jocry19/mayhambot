"""Small gradient-boosted trees for binary outcomes (logistic loss), JSON-serialisable.
Fitting uses numpy histograms when available (same trees, many times faster); prediction is pure Python.

Explainable: every split remembers how much it improved the loss (feature importance), and a prediction
can be split into per-feature contributions by following its path through each tree (Saabas method).
"""
import math
import random

from research.stats import np, sigmoid


def _bins(col, n_bins):
    xs = sorted(set(col))
    if len(xs) <= n_bins:
        return [(a + b) / 2 for a, b in zip(xs, xs[1:])]
    return sorted({xs[int(len(xs) * i / n_bins)] for i in range(1, n_bins)})


def _leaf(g, h, lam):
    return -sum(g) / (sum(h) + lam)


def _build(X, g, h, idx, depth, cuts, p, gain_acc):
    G, H = sum(g[i] for i in idx), sum(h[i] for i in idx)
    node = {"v": -G / (H + p["lam"]), "n": len(idx)}
    if depth >= p["depth"] or len(idx) < 2 * p["min_leaf"]:
        return node
    base = G * G / (H + p["lam"])
    best = (p["min_gain"], None, None)
    for j, cj in enumerate(cuts):
        if not cj:
            continue
        # histogram over cut points
        k = len(cj) + 1
        gs, hs, ns = [0.0] * k, [0.0] * k, [0] * k
        col = p["Xb"][j]
        for i in idx:
            b = col[i]
            gs[b] += g[i]
            hs[b] += h[i]
            ns[b] += 1
        gl = hl = 0.0
        nl = 0
        for b in range(k - 1):
            gl += gs[b]
            hl += hs[b]
            nl += ns[b]
            nr = len(idx) - nl
            if nl < p["min_leaf"] or nr < p["min_leaf"]:
                continue
            gr, hr = G - gl, H - hl
            gain = gl * gl / (hl + p["lam"]) + gr * gr / (hr + p["lam"]) - base
            if gain > best[0]:
                best = (gain, j, cj[b])
    gain, j, thr = best
    if j is None:
        return node
    gain_acc[j] += gain
    left = [i for i in idx if X[i][j] <= thr]
    right = [i for i in idx if X[i][j] > thr]
    node.update(f=j, t=thr, l=_build(X, g, h, left, depth + 1, cuts, p, gain_acc),
                r=_build(X, g, h, right, depth + 1, cuts, p, gain_acc))
    return node


def _build_np(g, h, idx, depth, cuts, p, gain_acc):
    """Same tree as _build; all features' histograms in one numpy pass (idx: index array).
    p["Xo"] holds bin indices offset per feature, so feature j's bins are p["off"][j] .. p["off"][j+1]-1."""
    gi, hi = g[idx], h[idx]
    G, H = float(gi.sum()), float(hi.sum())
    n = len(idx)
    node = {"v": -G / (H + p["lam"]), "n": n}
    if depth >= p["depth"] or n < 2 * p["min_leaf"]:
        return node
    lam, ml, off, tot = p["lam"], p["min_leaf"], p["off"], p["n_bins_total"]
    base = G * G / (H + lam)
    k = len(cuts)
    b = p["Xo"][:, idx].ravel()
    gs = np.bincount(b, weights=np.tile(gi, k), minlength=tot)
    hs = np.bincount(b, weights=np.tile(hi, k), minlength=tot)
    ns = np.bincount(b, minlength=tot)
    seg = p["seg"]  # feature of each global bin
    start = off[:-1][seg]

    def run(a):  # cumulative sum within each feature's bins
        c = np.cumsum(a)
        return c - np.concatenate(([0], c))[start]

    gl, hl, nl = run(gs), run(hs), run(ns)
    gain = gl * gl / (hl + lam) + (G - gl) ** 2 / (H - hl + lam) - base
    gain[p["last"] | (nl < ml) | (n - nl < ml)] = -np.inf
    bi = int(np.argmax(gain))  # first maximum = what the sequential scan keeps
    if not gain[bi] > p["min_gain"]:
        return node
    j, bb = int(seg[bi]), bi - int(off[seg[bi]])
    gain_acc[j] += float(gain[bi])
    go_left = p["Xo"][j][idx] <= bi  # bin <= bb  <=>  x <= cuts[j][bb]
    node.update(f=j, t=cuts[j][bb], l=_build_np(g, h, idx[go_left], depth + 1, cuts, p, gain_acc),
                r=_build_np(g, h, idx[~go_left], depth + 1, cuts, p, gain_acc))
    return node


def _apply_np(t, A, rows, out):
    if "f" not in t:
        out[rows] += t["v"]
        return
    left = A[rows, t["f"]] <= t["t"]
    _apply_np(t["l"], A, rows[left], out)
    _apply_np(t["r"], A, rows[~left], out)


def fit(X, y, n_trees=120, lr=0.08, depth=3, min_leaf=12, lam=2.0, subsample=0.8, n_bins=16, min_gain=1e-3,
        seed=7):
    n, k = len(X), len(X[0]) if X else 0
    pbar = min(0.99, max(0.01, sum(y) / max(1, n)))
    base = math.log(pbar / (1 - pbar))
    cuts = [_bins([r[j] for r in X], n_bins) for j in range(k)]
    import bisect
    Xb = [[bisect.bisect_left(cuts[j], r[j]) for r in X] for j in range(k)]  # bin index per feature, once
    p = dict(depth=depth, min_leaf=min_leaf, lam=lam, min_gain=min_gain, Xb=Xb)
    F = [base] * n
    trees, gain_acc = [], [0.0] * k
    rng = random.Random(seed)
    if np is not None and n:
        sizes = np.array([len(c) + 1 for c in cuts], dtype=np.int64)
        off = np.concatenate(([0], np.cumsum(sizes)))
        p.update(off=off, n_bins_total=int(off[-1]), seg=np.repeat(np.arange(k), sizes),
                 Xo=np.asarray(Xb, dtype=np.int64) + off[:-1, None])
        p["last"] = np.zeros(p["n_bins_total"], dtype=bool)
        p["last"][off[1:] - 1] = True  # a feature's last bin has nothing on its right
        A, yv, Fv, allr = np.asarray(X, dtype=float), np.asarray(y, dtype=float), np.full(n, base), np.arange(n)
        for _ in range(n_trees):
            pr = 1 / (1 + np.exp(-Fv))
            g, h = pr - yv, np.maximum(pr * (1 - pr), 1e-6)
            idx = np.array([i for i in range(n) if rng.random() < subsample], dtype=np.int64) \
                if subsample < 1 else allr
            t = _build_np(g, h, idx, 0, cuts, p, gain_acc)
            _scale(t, lr)
            trees.append(t)
            _apply_np(t, A, allr, Fv)
        tot = sum(gain_acc) or 1.0
        return dict(kind="gbm", base=base, trees=trees, importance=[a / tot for a in gain_acc])
    for _ in range(n_trees):
        pr = [sigmoid(f) for f in F]
        g = [pi - yi for pi, yi in zip(pr, y)]
        h = [max(pi * (1 - pi), 1e-6) for pi in pr]
        idx = [i for i in range(n) if rng.random() < subsample] if subsample < 1 else list(range(n))
        t = _build(X, g, h, idx, 0, cuts, p, gain_acc)
        _scale(t, lr)
        trees.append(t)
        for i in range(n):
            F[i] += _value(t, X[i])
    tot = sum(gain_acc) or 1.0
    return dict(kind="gbm", base=base, trees=trees, importance=[a / tot for a in gain_acc])


def _scale(t, lr):
    t["v"] *= lr
    if "f" in t:
        _scale(t["l"], lr)
        _scale(t["r"], lr)


def _value(t, x):
    while "f" in t:
        t = t["l"] if x[t["f"]] <= t["t"] else t["r"]
    return t["v"]


def predict(m, x):
    return sigmoid(m["base"] + sum(_value(t, x) for t in m["trees"]))


def contributions(m, x):
    """Per-feature effect on the log-odds: along each tree path, the change of node value at a split is
    credited to the split feature. Sum of contributions + sum of root values + base = log-odds."""
    k = len(m["importance"])
    c = [0.0] * k
    for t in m["trees"]:
        node = t
        while "f" in node:
            nxt = node["l"] if x[node["f"]] <= node["t"] else node["r"]
            c[node["f"]] += nxt["v"] - node["v"]
            node = nxt
    return c
