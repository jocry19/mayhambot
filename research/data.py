"""In-memory view of the trade history for research and backtests."""
import bisect
from collections import defaultdict
from dataclasses import dataclass

from core.config import settings
from research.prices import PriceIndex
from research.stats import median

POOL_SOURCES = {"geckoterminal", "rpc_early", "test_pool"}  # pool-wide trade feeds (all wallets of a coin)


@dataclass
class T:
    wallet: str
    mint: str
    side: str
    ts: int
    tok: float
    usd: float
    sol: float
    px: float
    pool: bool


class Dataset:
    def __init__(self, trades, pidx, created, bots, manual, sol_usd):
        self.trades = sorted(trades, key=lambda t: t.ts)
        self.ts_list = [t.ts for t in self.trades]
        self.pidx = pidx
        self.created = created
        self.bots = bots
        self.manual = manual
        self.sol_usd = sol_usd
        self.funding = {}  # wallet -> (funder, ts) from research.funding
        # coins picked for research because of how they later moved (pumped, or calm controls): mint -> pick time.
        # Model events on them before that time would be chosen by their outcome (selection bias).
        self.hindsight = {}
        self.early_found = set()  # wallets found as early buyers of such coins (their find coin is not counted)
        self.intel = None  # research.intel_features.Source, set by load()
        self.by_mint = defaultdict(list)
        for t in self.trades:
            self.by_mint[t.mint].append(t)
        self.by_mint_ts = {m: [t.ts for t in ts] for m, ts in self.by_mint.items()}
        self.pool_ts = {m: [t.ts for t in ts if t.pool] for m, ts in self.by_mint.items()}

    @property
    def start(self):
        return self.ts_list[0] if self.ts_list else 0

    @property
    def end(self):
        return self.ts_list[-1] if self.ts_list else 0

    def before(self, cutoff):
        return self.trades[:bisect.bisect_left(self.ts_list, cutoff)]

    def window(self, mint, a, b):
        """Trades of a coin with a < ts <= b."""
        tss = self.by_mint_ts.get(mint, [])
        lst = self.by_mint.get(mint, [])
        return lst[bisect.bisect_right(tss, a):bisect.bisect_right(tss, b)]

    def pool_covered(self, mint, a, b, max_gap, cutoff=None):
        """True if pool-wide trades exist on both sides of [a, b] with no hole > max_gap inside.
        cutoff: only trades before it may be used (point-in-time)."""
        tss = self.pool_ts.get(mint)
        if not tss:
            return False
        if cutoff is not None:
            tss = tss[:bisect.bisect_left(tss, cutoff)]
        i = bisect.bisect_right(tss, a) - 1
        j = bisect.bisect_left(tss, b)
        if i < 0 or j >= len(tss) or a - tss[i] > max_gap or tss[j] - b > max_gap:
            return False
        return all(tss[k + 1] - tss[k] <= max_gap for k in range(i, j))

    def to_sol(self, t):
        return t.sol if t.sol and t.sol > 0 else (t.usd / self.sol_usd if self.sol_usd else 0.0)


_flag_cache = {"ts": 0.0, "val": (set(), set()), "db": None}


def _flags(conn, ttl=120):
    """is_bot / manual wallet sets, cached for `ttl` s (score_token loads one coin at a time, many times a minute)."""
    import time as _time
    db_path = next((r[2] for r in conn.execute("PRAGMA database_list") if r[1] == "main"), "")
    if _flag_cache["db"] != db_path or _time.time() - _flag_cache["ts"] > ttl:
        bots = {r[0] for r in conn.execute("SELECT address FROM wallets WHERE is_bot=1")}
        manual = {r[0] for r in conn.execute("SELECT address FROM wallets WHERE manual=1")}
        _flag_cache.update(ts=_time.time(), val=(bots, manual), db=db_path)
    return _flag_cache["val"]


class Shared:
    """What every per-coin load needs and does not change within a scoring cycle: bot/manual flags and the intel
    feature source (with its per-minute cache). Build once per cycle with shared(conn), pass to load()."""

    def __init__(self, conn):
        from research.intel_features import Source
        self.bots, self.manual = _flags(conn, ttl=0)
        self.intel = Source(conn)


def shared(conn):
    return Shared(conn)


def load(conn, since_ts=0, mints=None, shared=None):
    """mints=None: everything (training). mints=[...]: those coins only (live scoring); funding links are then
    skipped (only training builds clusters) and `shared` (see Shared) saves the per-call flag/intel setup."""
    cond, args = "WHERE ts >= ?", [since_ts]
    if mints is not None:
        mints = list(mints)
        cond += f" AND mint IN ({','.join('?' * len(mints))})"
        args += mints
    trades, ratios = [], []
    for r in conn.execute(f"SELECT wallet, mint, side, ts, token_amount, usd_amount, sol_amount, price_usd, source "
                          f"FROM trades {cond}", args):
        t = T(r[0], r[1], r[2], int(r[3] or 0), r[4] or 0.0, r[5] or 0.0, r[6] or 0.0, r[7] or 0.0,
              (r[8] or "") in POOL_SOURCES)
        trades.append(t)
        if t.sol > 0.01 and t.usd > 1 and len(ratios) < 50000:
            ratios.append(t.usd / t.sol)
    pidx = PriceIndex.from_db(conn, mints)
    created = {}
    mcond, margs = (f" AND mint IN ({','.join('?' * len(mints))})", mints) if mints is not None else ("", [])
    for r in conn.execute("SELECT mint, pair_created_at FROM tokens WHERE pair_created_at > 0" + mcond, margs):
        created[r[0]] = r[1]
    for r in conn.execute("SELECT mint, created_at FROM research_tokens WHERE created_at > 0" + mcond, margs):
        created[r[0]] = min(created.get(r[0], r[1]), r[1])
    bots, manual = (shared.bots, shared.manual) if shared is not None else _flags(conn)
    sol_usd = median(ratios) or 150.0
    # one money scale: wallet-history trades carry USD at today's SOL price, pool-feed trades USD of their day.
    # Mixing both distorts PnL and tiers, so every trade with a SOL amount is valued in SOL at one rate.
    for t in trades:
        if t.sol > 0:
            t.usd = t.sol * sol_usd
    ds = Dataset(trades, pidx, created, bots, manual, sol_usd)
    if shared is not None:
        ds.intel = shared.intel
    else:
        from research.intel_features import Source
        ds.intel = Source(conn)  # point-in-time coin features from intel/ (neutral when unavailable)
    if mints is not None:
        return ds
    try:
        ds.hindsight = {r[0]: int(r[1]) for r in conn.execute(
            "SELECT mint, early_fetched_at FROM research_tokens WHERE early_fetched_at > 0")}
        ds.early_found = {r[0] for r in conn.execute(
            "SELECT address FROM research_wallets WHERE discovered_via IN ('early_pumped', 'pumpfun_early')")}
    except Exception:  # noqa: BLE001
        pass
    try:
        ds.funding = {r[0]: (r[1], int(r[2] or 0)) for r in conn.execute(
            "SELECT wallet, funder, ts FROM research_funding WHERE funder IS NOT NULL")}
    except Exception:  # noqa: BLE001  (table not created yet)
        pass
    return ds


def bot_wallets(trades, limit=settings.bot_max_trades_per_token):
    cnt = defaultdict(int)
    for t in trades:
        cnt[(t.wallet, t.mint)] += 1
    return {w for (w, _), c in cnt.items() if c > limit}
