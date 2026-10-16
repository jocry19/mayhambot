"""Small statistics: Wilson interval, AUC, standardized logistic + ridge regression.
The regression fits use numpy when available (same results, many times faster); models stay plain lists."""
import math

try:
    import numpy as np
except ImportError:  # pragma: no cover - numpy is in requirements, the pure-Python path stays as fallback
    np = None


def wilson(k, n, z=1.96):
    """(rate, lower, upper) of a binomial proportion."""
    if n <= 0:
        return 0.0, 0.0, 1.0
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return p, max(0.0, c - h), min(1.0, c + h)


def auc(scores, labels):
    """Rank AUC (probability a random positive scores higher than a random negative)."""
    pairs = sorted(zip(scores, labels), key=lambda x: x[0])
    n_pos = sum(1 for _, y in pairs if y)
    n_neg = len(pairs) - n_pos
    if not n_pos or not n_neg:
        return None
    rank_sum, i = 0.0, 0
    while i < len(pairs):
        j = i
        while j + 1 < len(pairs) and pairs[j + 1][0] == pairs[i][0]:
            j += 1
        avg_rank = (i + j) / 2 + 1
        rank_sum += avg_rank * sum(1 for k in range(i, j + 1) if pairs[k][1])
        i = j + 1
    return (rank_sum - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg)


def sigmoid(z):
    if z >= 0:
        return 1 / (1 + math.exp(-z))
    e = math.exp(z)
    return e / (1 + e)


def standardize_fit(X):
    k = len(X[0]) if X else 0
    n = len(X)
    mean = [sum(r[j] for r in X) / n for j in range(k)] if n else [0.0] * k
    std = []
    for j in range(k):
        v = sum((r[j] - mean[j]) ** 2 for r in X) / n if n else 0.0
        std.append(math.sqrt(v) if v > 1e-12 else 1.0)
    return mean, std


def _z(X, mean, std):
    return [[(r[j] - mean[j]) / std[j] for j in range(len(mean))] for r in X]


def logistic_fit(X, y, l2=5.0, iters=25, weights=None):
    """L2-regularised logistic regression on standardized inputs, fitted with Newton/IRLS.
    Returns dict(bias, coef, mean, std) with coef per standard deviation."""
    if np is not None and X:
        return _logistic_fit_np(X, y, l2, iters, weights)
    mean, std = standardize_fit(X)
    Z = [[1.0] + r for r in _z(X, mean, std)]
    n, k = len(Z), len(mean) + 1
    w = weights or [1.0] * n
    W = sum(w)
    pbar = min(0.99, max(0.01, sum(wi * yi for wi, yi in zip(w, y)) / W))
    beta = [math.log(pbar / (1 - pbar))] + [0.0] * (k - 1)
    for _ in range(iters):
        g = [0.0] * k
        H = [[0.0] * k for _ in range(k)]
        for zi, yi, wi in zip(Z, y, w):
            p = sigmoid(sum(b * z for b, z in zip(beta, zi)))
            e = wi * (p - yi)
            v = wi * max(p * (1 - p), 1e-6)
            for a in range(k):
                za = zi[a]
                if za == 0.0:
                    continue
                g[a] += e * za
                vza = v * za
                Ha = H[a]
                for c in range(a, k):
                    Ha[c] += vza * zi[c]
        for a in range(k):
            for c in range(a):
                H[a][c] = H[c][a]
        for a in range(1, k):  # no penalty on the bias
            g[a] += l2 * beta[a]
            H[a][a] += l2
        step = _solve(H, g)
        beta = [b - s for b, s in zip(beta, step)]
        if max(abs(s) for s in step) < 1e-6:
            break
    return dict(bias=beta[0], coef=beta[1:], mean=mean, std=std)


def _standardize_np(X):
    A = np.asarray(X, dtype=float)
    mean = A.mean(axis=0)
    var = ((A - mean) ** 2).mean(axis=0)
    std = np.where(var > 1e-12, np.sqrt(var), 1.0)
    return A, mean, std


def _solve_np(A, b):
    try:
        return np.linalg.solve(A, b)
    except np.linalg.LinAlgError:
        return np.asarray(_solve(A.tolist(), b.tolist()))


def _logistic_fit_np(X, y, l2, iters, weights):
    A, mean, std = _standardize_np(X)
    n = len(A)
    Z = np.hstack([np.ones((n, 1)), (A - mean) / std])
    k = Z.shape[1]
    yv = np.asarray(y, dtype=float)
    w = np.asarray(weights, dtype=float) if weights else np.ones(n)
    pbar = min(0.99, max(0.01, float(w @ yv) / float(w.sum())))
    beta = np.zeros(k)
    beta[0] = math.log(pbar / (1 - pbar))
    pen = np.full(k, l2)
    pen[0] = 0.0  # no penalty on the bias
    for _ in range(iters):
        t = Z @ beta
        p = np.where(t >= 0, 1 / (1 + np.exp(-np.abs(t))), np.exp(-np.abs(t)) / (1 + np.exp(-np.abs(t))))
        g = Z.T @ (w * (p - yv)) + pen * beta
        v = w * np.maximum(p * (1 - p), 1e-6)
        H = (Z * v[:, None]).T @ Z + np.diag(pen)
        step = _solve_np(H, g)
        beta = beta - step
        if np.max(np.abs(step)) < 1e-6:
            break
    return dict(bias=float(beta[0]), coef=[float(c) for c in beta[1:]], mean=[float(m) for m in mean],
                std=[float(sd) for sd in std])


def logistic_predict(m, x):
    t = m["bias"] + sum(c * (xi - mu) / sd for c, xi, mu, sd in zip(m["coef"], x, m["mean"], m["std"]))
    return sigmoid(t)


def contributions(m, x):
    """Per-feature effect on the log-odds relative to the training average."""
    return [c * (xi - mu) / sd for c, xi, mu, sd in zip(m["coef"], x, m["mean"], m["std"])]


def _solve(A, b):
    n = len(A)
    M = [row[:] + [bi] for row, bi in zip(A, b)]
    for i in range(n):
        p = max(range(i, n), key=lambda r: abs(M[r][i]))
        M[i], M[p] = M[p], M[i]
        if abs(M[i][i]) < 1e-12:
            continue
        for r in range(n):
            if r != i:
                f = M[r][i] / M[i][i]
                for cidx in range(i, n + 1):
                    M[r][cidx] -= f * M[i][cidx]
    return [M[i][n] / M[i][i] if abs(M[i][i]) > 1e-12 else 0.0 for i in range(n)]


def ridge_fit(X, y, l2=5.0):
    if np is not None and X and y:
        A, mean, std = _standardize_np(X)
        Z = (A - mean) / std
        yv = np.asarray(y, dtype=float)
        ybar = float(yv.mean())
        k = Z.shape[1]
        coef = _solve_np(Z.T @ Z + l2 * np.eye(k), Z.T @ (yv - ybar)) if k else []
        return dict(bias=ybar, coef=[float(c) for c in coef], mean=[float(m) for m in mean],
                    std=[float(sd) for sd in std])
    mean, std = standardize_fit(X)
    Z = _z(X, mean, std)
    k = len(mean)
    ybar = sum(y) / len(y) if y else 0.0
    A = [[sum(z[i] * z[j] for z in Z) + (l2 if i == j else 0.0) for j in range(k)] for i in range(k)]
    bvec = [sum(z[i] * (yi - ybar) for z, yi in zip(Z, y)) for i in range(k)]
    return dict(bias=ybar, coef=_solve(A, bvec) if k else [], mean=mean, std=std)


def ridge_predict(m, x):
    return m["bias"] + sum(c * (xi - mu) / sd for c, xi, mu, sd in zip(m["coef"], x, m["mean"], m["std"]))


def median(xs):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def quantile(xs, q):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    return xs[min(len(xs) - 1, max(0, int(q * (len(xs) - 1) + 0.5)))]
