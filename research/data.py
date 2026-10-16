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

    def pool_covered(self, mint, a, b, max_gap):
        """True if pool-wide trades exist on both sides of [a, b] with no hole > max_gap inside."""
        tss = self.pool_ts.get(mint)
        if not tss:
            return False
        i = bisect.bisect_right(tss, a) - 1
        j = bisect.bisect_left(tss, b)
        if i < 0 or j >= len(tss) or a - tss[i] > max_gap or tss[j] - b > max_gap:
            return False
        return all(tss[k + 1] - tss[k] <= max_gap for k in range(i, j))

    def to_sol(self, t):
        return t.sol if t.sol and t.sol > 0 else (t.usd / self.sol_usd if self.sol_usd else 0.0)


def load(conn, since_ts=0, mints=None):
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
    for r in conn.execute("SELECT mint, pair_created_at FROM tokens WHERE pair_created_at > 0"):
        created[r[0]] = r[1]
    for r in conn.execute("SELECT mint, created_at FROM research_tokens WHERE created_at > 0"):
        created[r[0]] = min(created.get(r[0], r[1]), r[1])
    bots = {r[0] for r in conn.execute("SELECT address FROM wallets WHERE is_bot=1")}
    manual = {r[0] for r in conn.execute("SELECT address FROM wallets WHERE manual=1")}
    return Dataset(trades, pidx, created, bots, manual, median(ratios) or 150.0)


def bot_wallets(trades, limit=settings.bot_max_trades_per_token):
    cnt = defaultdict(int)
    for t in trades:
        cnt[(t.wallet, t.mint)] += 1
    return {w for (w, _), c in cnt.items() if c > limit}
