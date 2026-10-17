"""In-memory state of one launch, built from the PumpPortal trade stream, plus bonding-curve math."""
from dataclasses import dataclass, field

PUMP_SUPPLY = 1_000_000_000.0          # every pump.fun / bonk launch has 1e9 tokens (6 decimals)
PUMP_DECIMALS = 6
PUMP_REAL_TOKENS = 793_100_000.0      # tokens sold through the curve until migration
PUMP_TOKEN_OFFSET = 279_900_000.0     # virtual minus real token reserves (constant)


def f(v, default=0.0):
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


class PricePath:
    """Market-cap path of one launch from its creation, for exact replays of exit rules (which came first, the
    stop or the take-profit). Buckets of 5 s for the first 15 minutes, 30 s after, up to 65 minutes; each bucket
    keeps last, low and high, so no extreme between two samples gets lost."""
    FINE_SEC, FINE_UNTIL, COARSE_SEC, MAX_SEC = 5, 900, 30, 3900

    def __init__(self, t0):
        self.t0, self.b = t0, {}

    def key(self, dt):
        if dt < self.FINE_UNTIL:
            return int(dt // self.FINE_SEC) * self.FINE_SEC
        return self.FINE_UNTIL + int((dt - self.FINE_UNTIL) // self.COARSE_SEC) * self.COARSE_SEC

    def add(self, now, mcap):
        if not mcap or mcap <= 0:
            return
        dt = max(0.0, now - self.t0)
        if dt > self.MAX_SEC:
            return
        k = self.key(dt)
        e = self.b.get(k)
        if e is None:
            self.b[k] = [mcap, mcap, mcap]
        else:
            e[0], e[1], e[2] = mcap, min(e[1], mcap), max(e[2], mcap)

    def points(self):
        """[[seconds after creation, last, low, high], ...] in time order (mcap in SOL)."""
        return [[k, round(v[0], 4), round(v[1], 4), round(v[2], 4)] for k, v in sorted(self.b.items())]

    def __len__(self):
        return len(self.b)


def curve_buy(v_sol, v_tok, sol_in, fee_pct):
    """Tokens out for sol_in on a constant-product curve (fee taken from the input)."""
    if v_sol <= 0 or v_tok <= 0 or sol_in <= 0:
        return 0.0
    net = sol_in * (1 - fee_pct / 100)
    return v_tok - (v_sol * v_tok) / (v_sol + net)


def curve_sell(v_sol, v_tok, tokens, fee_pct):
    """SOL out for selling tokens on the curve (fee taken from the output)."""
    if v_sol <= 0 or v_tok <= 0 or tokens <= 0:
        return 0.0
    gross = v_sol - (v_sol * v_tok) / (v_tok + tokens)
    return gross * (1 - fee_pct / 100)


def curve_progress(v_tok, pool="pump"):
    """Share of the pump.fun curve already sold (1.0 = migration). None for other launchpads."""
    if pool != "pump" or v_tok <= 0:
        return None
    real = v_tok - PUMP_TOKEN_OFFSET
    return max(0.0, min(1.0, 1 - real / PUMP_REAL_TOKENS))


@dataclass
class StreamTrade:
    ts: float
    sig: str
    wallet: str
    side: str          # buy | sell
    sol: float
    tokens: float
    new_balance: float | None
    v_sol: float
    v_tok: float
    mcap_sol: float
    slot: int | None = None


@dataclass
class LaunchState:
    mint: str
    created_ts: float
    source: str = "pumpportal"
    pool: str = "pump"
    symbol: str = ""
    name: str = ""
    uri: str = ""
    creator: str = ""
    create_sig: str = ""
    dev_tokens: float = 0.0
    dev_sol: float = 0.0
    v_sol0: float = 0.0
    v_tok0: float = 0.0
    v_sol: float = 0.0
    v_tok: float = 0.0
    mcap_sol: float = 0.0
    mcap0_sol: float = 0.0
    supply: float = PUMP_SUPPLY
    trades: list = field(default_factory=list)
    balances: dict = field(default_factory=dict)      # wallet -> tokens (from newTokenBalance)
    bought: dict = field(default_factory=dict)        # wallet -> tokens bought (seen)
    first_buy_ts: dict = field(default_factory=dict)  # wallet -> first seen buy time
    last_trade_ts: float = 0.0
    subscribed: bool = True
    migrated: bool = False
    migrated_ts: float | None = None
    stage: int = 0                                    # 0 watching, 1 decided free, 2 checked in depth
    decision: str = "pending"                         # pending | queued | enter | skip
    decided_ts: float | None = None
    base_mcap_sol: float | None = None                # mcap at decision (outcomes are measured from here)
    outcomes: dict = field(default_factory=dict)      # horizon min -> mcap_sol
    max_mcap_sol: float = 0.0                         # high/low after the decision (outcomes; set to base there)
    min_mcap_sol: float = 0.0
    peak_mcap_sol: float = 0.0                        # high since creation (drawdown feature before the decision)
    insiders: dict = field(default_factory=dict)      # wallet -> role (bundle/insider/dev) for exit rules
    insider_base: dict = field(default_factory=dict)  # wallet -> tokens an insider got before we watched (enrichment)
    insider_sold: dict = field(default_factory=dict)  # wallet -> share of its buy sold after the decision
    curve: list = field(default_factory=list)         # (ts, mcap_sol, v_sol, n_trades), 2 s resolution
    external_mcap_sol: float | None = None            # DexScreener after migration / unsubscribe
    external_ts: float | None = None
    queued_ts: float | None = None
    prescore: float | None = None
    stage1_reason: str | None = None
    done: bool = False
    trade_src: str = "pumpportal"                     # rpc_logs: subscribed after the creation block, bundles are unseen
    seen_net_tokens: float = 0.0                      # tokens bought minus sold by the trades we saw
    n_seen: int = 0
    max_trades: int = 3000
    stream_complete: bool = False                     # every trade since the creation is in the stream (RPC program mode)
    _keys: set = field(default_factory=set, repr=False)
    learned: bool = False                             # picked by the accepted research rule (sniper.research)
    create_slot: int | None = None                    # slot of the creation (RPC create stream), bundle reference
    path: PricePath | None = None                     # bucketed mcap path for exit replays (sniper_paths)

    def __post_init__(self):
        if self.path is None:
            self.path = PricePath(self.created_ts)

    # ---- stream updates
    def apply_create(self, m, now):
        self.symbol, self.name, self.uri = m.get("symbol") or "", m.get("name") or "", m.get("uri") or ""
        self.creator = m.get("traderPublicKey") or ""
        self.create_sig = m.get("signature") or ""
        self.create_slot = m.get("slot") or None
        self.pool = m.get("pool") or "pump"
        self.dev_tokens = f(m.get("initialBuy") or m.get("tokenAmount"))
        if self.pool != "pump" and (self.dev_tokens >= 0.9 * self.supply or not m.get("vTokensInBondingCurve")):
            self.dev_tokens = 0.0  # bonk creates report the pool's tokens as initialBuy and no curve state
        self.dev_sol = f(m.get("solAmount"))
        self.v_sol = self.v_sol0 = f(m.get("vSolInBondingCurve"))
        self.v_tok = self.v_tok0 = f(m.get("vTokensInBondingCurve"))
        self.mcap_sol = self.mcap0_sol = f(m.get("marketCapSol")) or self.price_sol * self.supply
        self.max_mcap_sol = self.min_mcap_sol = self.peak_mcap_sol = self.mcap_sol
        if self.creator and self.dev_tokens > 0:
            self.balances[self.creator] = self.dev_tokens
            self.bought[self.creator] = self.dev_tokens
            self.first_buy_ts[self.creator] = now
        self.curve.append((int(now), self.mcap_sol, self.v_sol, 0))
        self.path.add(now, self.mcap_sol)

    def apply_trade(self, m, now):
        side = (m.get("txType") or "").lower()
        if side not in ("buy", "sell"):
            return None
        t = StreamTrade(ts=now, sig=m.get("signature") or "", wallet=m.get("traderPublicKey") or "", side=side,
                        sol=f(m.get("solAmount")), tokens=f(m.get("tokenAmount")),
                        new_balance=f(m.get("newTokenBalance"), None) if m.get("newTokenBalance") is not None else None,
                        v_sol=f(m.get("vSolInBondingCurve")), v_tok=f(m.get("vTokensInBondingCurve")),
                        mcap_sol=f(m.get("marketCapSol")), slot=m.get("slot"))
        key = (t.sig, t.wallet, round(t.tokens, 3), side)
        if t.sig and key in self._keys:
            return None            # the same trade twice (buffer replay, reconnect, two sources)
        if t.sig and t.sig == self.create_sig and t.wallet == self.creator and side == "buy" and self.dev_tokens:
            self._keys.add(key)    # the dev buy inside the create transaction is already in apply_create
            return None
        if t.sig:
            self._keys.add(key)
        if len(self.trades) < self.max_trades:
            self.trades.append(t)
        self.n_seen += 1
        if m.get("src"):
            self.trade_src = m["src"]
        self.seen_net_tokens += t.tokens if side == "buy" else -t.tokens
        if t.v_sol > 0 and t.v_tok > 0:
            self.v_sol, self.v_tok = t.v_sol, t.v_tok
        self.mcap_sol = t.mcap_sol or (self.price_sol * self.supply)
        self.peak_mcap_sol = max(self.peak_mcap_sol, self.mcap_sol)
        # only after the decision: a dump before it must not count as upside of an entry at the decision price
        if self.decided_ts is not None and now - self.decided_ts <= 3600:
            self.max_mcap_sol = max(self.max_mcap_sol, self.mcap_sol)
            self.min_mcap_sol = min(self.min_mcap_sol or self.mcap_sol, self.mcap_sol)
        if t.wallet:
            if t.new_balance is not None:
                self.balances[t.wallet] = t.new_balance
            elif side == "buy":
                self.balances[t.wallet] = self.balances.get(t.wallet, 0.0) + t.tokens
            else:
                self.balances[t.wallet] = max(0.0, self.balances.get(t.wallet, 0.0) - t.tokens)
            if side == "buy":
                self.bought[t.wallet] = self.bought.get(t.wallet, 0.0) + t.tokens
                self.first_buy_ts.setdefault(t.wallet, now)
            elif t.wallet in self.insiders and (self.bought.get(t.wallet) or self.insider_base.get(t.wallet)):
                # a bundle wallet bought in the creation slot, before our stream: its base comes from the enrichment
                got = self.bought.get(t.wallet) or self.insider_base[t.wallet]
                self.insider_sold[t.wallet] = min(1.0, self.insider_sold.get(t.wallet, 0.0) + t.tokens / got)
        self.last_trade_ts = now
        self.path.add(now, self.mcap_sol)
        if not self.curve or now - self.curve[-1][0] >= 2:
            self.curve.append((int(now), self.mcap_sol, self.v_sol, self.n_seen))
        return t

    # ---- derived values
    @property
    def price_sol(self):
        return self.v_sol / self.v_tok if self.v_tok > 0 else 0.0

    @property
    def age(self):
        return self.last_trade_ts - self.created_ts if self.last_trade_ts else 0.0

    def current_mcap_sol(self):
        if (self.migrated or not self.subscribed) and self.external_mcap_sol:
            return self.external_mcap_sol
        return self.mcap_sol

    def unseen_tokens(self):
        """Tokens bought from the curve by transactions we never saw (bundled with the creation, before
        our subscription was active): drop of the curve minus what the seen trades explain."""
        if self.v_tok0 <= 0 or self.v_tok <= 0:
            return 0.0
        return max(0.0, (self.v_tok0 - self.v_tok) - self.seen_net_tokens)
