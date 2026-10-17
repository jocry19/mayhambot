# mayhambot core — interfaces

Owned by the "Smart-Money-Signale und Dashboard" thread. Other modules (signals/, copytrade/,
dashboard/) import only what is listed here. Run everything from the `mayhambot/` folder
(`python -m signals.run`, `streamlit run dashboard/app.py`), so imports are `from core import ...`.

If copytrade/ needs something new in core, send the proposed file/function through the coordinator;
core will merge it. Do not edit core/ directly from another thread.

## Config — `core.config`

```python
from core.config import settings          # loaded once from mayhambot/.env (+ environment)
settings.db_path, settings.rpc_url, settings.helius_api_key, settings.telegram_bot_token, ...
```
All keys and thresholds: see `.env.example`. Never hard-code keys.

## Storage — `core.db`

```python
from core import db
conn = db.connect()            # sqlite3.Connection, row_factory=sqlite3.Row, schema auto-created, WAL mode
db.insert_trades(conn, trades) # list[Trade] -> number of new rows (dedup on sig+wallet+mint+side)
db.upsert_tokens(conn, tokens) # list[Token]; also appends a token_prices snapshot
db.latest_price(conn, mint)    # float | None
db.trades_for_mint(conn, mint, since_ts=None)    # list[sqlite3.Row]
db.trades_for_wallet(conn, wallet, since_ts=None)
db.retry_locked(conn, fn)     # rerun a repeatable write step if the DB stayed locked past the 30 s busy timeout
```
Tables (stable column names; columns are only added via `db.MIGRATIONS`, never removed or renamed):

| table | columns |
|---|---|
| `tokens` | mint PK, symbol, name, pool_address, dex, price_usd, mcap_usd, liquidity_usd, pair_created_at, price_change_m5/h1/h24, volume_h1, txns_m5, txns_h1 (DexScreener buys+sells), first_seen, last_updated, tracked, last_trades_fetch |
| `token_prices` | mint, ts, price_usd, mcap_usd, liquidity_usd (DexScreener snapshot each cycle) |
| `trades` | sig, wallet, mint, side ('buy'/'sell'), ts, token_amount, sol_amount, usd_amount, price_usd, source ('geckoterminal' / 'rpc' / 'helius' / 'rpc_early') |
| `wallets` | address PK, label, source, manual, score (0-100, heuristic), tier_source (model|heuristic), tier_version, model_score, cluster_id, exit_capture, **tier** (once a model version is active: synced from model_wallet_stats every rescore; 'ultra'/'smart'/'good'/NULL), is_smart (=tier in ultra,smart), is_bot, tokens_traded, win_rate, early_rate, realized_pnl_usd, total_pnl_usd, roi, median_peak_multiple, consistency, last_trade_ts, backfilled_at, updated_at |
| `wallet_positions` | wallet, mint, buy_usd, sell_usd, buy_tokens, sell_tokens, realized_pnl_usd, unrealized_pnl_usd, roi, first_buy_ts, first_buy_price, peak_multiple, trades |
| `signal_metrics` | one row per tracked coin per cycle: ts, mint, window_min, buy_usd, sell_usd, net_usd, buy_sol, net_sol, smart_buyers, n_ultra, n_smart, n_good, avg_quality, score, prediction, contributions_json, price_usd, mcap_usd |
| `signals` | id, ts, mint, symbol, score (after safety penalty), score_raw, safety_factor, safety_risk, safety_level, safety_reasons_json, **mcap_usd** (at signal), price_usd, liquidity_usd, buy_usd, net_usd, **buy_sol, net_sol**, smart_buyers, **n_ultra, n_smart, n_good**, avg_quality, **prediction** (0-1 or NULL), prediction_label, **contributions_json**, model_version, wallets_json, notified, outcomes: ret_5m, ret_15m, ret_1h, ret_6h, max_ret_1h, max_mcap_1h, max_ret_24h |

`contributions_json` = `{"<input>": {"weight": w, "value": 0..1, "points": 100*w*value, "why": "text"}}`; the
points add up to `score`. `wallets_json` = list of `{wallet, tier, score, n_tokens, buy_usd, buy_sol, sell_usd, first_ts}`
(net buyers in the window). `model_version` names what produced score/tiers/prediction
(`core-heuristic-v1` until model/ is wired in).

All timestamps are unix seconds (int, UTC). Amounts: `usd_amount` in USD, `sol_amount` in SOL.

## Models — `core.models`

```python
Trade(sig, wallet, mint, side, ts, token_amount, sol_amount, usd_amount, price_usd, source)
Token(mint, symbol, name, pool_address, dex, price_usd, mcap_usd, liquidity_usd, pair_created_at,
      price_change_m5, price_change_h1, price_change_h24, volume_h1)
```

## Data sources — `core.sources`

```python
from core.sources import market, wallet_source
market.discover_tokens(limit=40) -> list[Token]        # new/trending Solana memecoins (DexScreener + GeckoTerminal)
market.token_info(mints: list[str]) -> list[Token]     # live price/mcap/liquidity, best pool per mint (DexScreener)
market.pool_trades(token: Token) -> list[Trade]        # latest swaps of the token's pool, signer wallet = Trade.wallet (GeckoTerminal)
market.sol_price_usd() -> float
market.pool_ohlcv(token, timeframe="minute", aggregate=15) -> [(ts, o, h, l, c, vol_usd)]   # GeckoTerminal candles
wallet_source.wallet_trades(wallet, limit=60) -> list[Trade]   # a wallet's recent swaps (Helius if key, else Solana RPC)
wallet_source.early_buyers(mint, max_pages=8, n_tx=120) -> list[Trade]  # first trades of a young token (RPC)
```
All network calls go through `core.http.get_json/post_json` (per-host rate limit, retries on 429/5xx).

## Rate limits and API budgets — `core.http`

```python
from core import http
http.get_json(url, params=None, headers=None) / http.post_json(url, payload)   # retries on 429/5xx, keys redacted in logs
http.throttle(host)            # call before your own request to a host (packages with their own HTTP client)
http.penalize(host, seconds)   # after a 429 you received yourself
http.set_service("safety")     # name this process; mayham.py sets MAYHAM_SERVICE=<service> for every service
http.set_budget("helius", 90)  # this process: at most 90 calls/min to that host or group (0/None = off)
http.budget("helius")          # current per-minute budget of this process, None = unlimited
http.usage()                   # {group: {calls, 429, waited_s, budget_per_min, last_min}} of this process
http.usage(all_services=True)  # {service: {service, pid, ts, groups: {...}}} of all processes (updated every minute)
http.slowdown(host)            # shared adaptive factor after 429s (1.0 = base interval)
```

Three layers, in this order on every call:
1. **Own budget** (per process, sliding 60 s window): when the service's calls per minute to a group are used up,
   `throttle` waits instead of calling. Groups: `helius` (api.helius.xyz, mainnet/beta.helius-rpc.com),
   `geckoterminal`, otherwise the host. Defaults per service in `DEFAULT_BUDGETS` (Helius: signals 60,
   research 120, copytrade 120, safety 60, sniper 120 = 480/min, free plan 600/min); override with
   `<GROUP>_BUDGET_<SERVICE>` in `.env`, e.g. `HELIUS_BUDGET_SAFETY=90`. A process without a service name has
   no budget (as before).
2. **Shared spacing** for `SHARED_HOSTS`: one minimum interval per host across all processes (lock files in
   `data/ratelimit/`).
3. **Adaptive slowdown**: every 429 stretches that shared interval x1.5 (max x4) and pauses the host for all
   processes (GeckoTerminal at least 20 s); successful calls relax it x0.98.

Every 10 minutes each process logs `usage helius: N calls, M 429, budget X/min, credits today a/b, month c/d
(e% share) (...)`, and every 429 is logged as `429 GET host/path [rpc method]`.

### Live and bulk lanes (public RPC)

```python
http.post_json(url, payload, priority="live")         # time-critical read, e.g. getTransaction of a fresh whale buy
http.throttle(host, priority="live", method="getTransaction")   # own HTTP clients
```

Solana public RPC (api.mainnet-beta.solana.com; limits per the Solana docs on public cluster endpoints, from
memory, not re-checked online: 100 requests / 10 s per IP, 40 / 10 s per single method, 40 connections) gets a
cross-process sliding window in `WINDOW_LIMITS`: at most 60 calls per 10 s in total and 24 per method
(`PUBLIC_RPC_WINDOW_TOTAL`, `PUBLIC_RPC_WINDOW_PER_METHOD`), so one method never starves the others.
Tighter limits per method and endpoint: `PUBLIC_RPC_METHOD_LIMITS` (`method@host=n,...`, default
getTokenLargestAccounts@api.mainnet-beta.solana.com=6); preferred endpoint per method: `PUBLIC_RPC_PREFER`
(`method=host,...`, default getTokenLargestAccounts=solana-rpc.publicnode.com).
A 429 never removes an endpoint: it slows the host down (shared slowdown) and halves that endpoint's window for
60 s. Only timeouts / 5xx (3 in a row) or a failed probe take an endpoint out for 10 min, and never the last
healthy one. 429 log lines show the method, the window fill and whether the window is halved.
- `core.sources.wallet_source.wallet_trades` raises `SkipForNow` on a public-RPC 429 (retry the wallet later)
  instead of falling back to Helius Enhanced; Enhanced fallbacks are capped at 10 per hour per process.
- Three lanes, waiting order live > normal > bulk: `"live"` uses the whole window, `"normal"` (service cycles)
  all but half the live share, `"bulk"` (default) no live share; lower lanes step aside while a higher one waits.
- Pool calls: timeout 8 s, after a 429 at most 3 s sleep, slowdown at most x2; 3 timeouts / 5xx in a row take an
  endpoint out for 2 min (failed probe: 10 min), never the last healthy one.
- `wallet_trades(..., helius_rpc=True)`: paid Helius RPC lane first (1 credit per call, within the daily share),
  public pool as fallback.
- `priority="bulk"` (default, backfills, research) may use only `(1 - PUBLIC_RPC_LIVE_SHARE)` of the window
  (share default 0.4, so 54 of 90) and pauses while a live call is waiting (`data/ratelimit/<host>.live` marker).
- `priority="live"` (copytrade, safety, sniper) may use the whole window and skips the plain spacing queue.
- `usage()` shows `live_calls`, `bulk_calls`, `live_waited_s`, `bulk_waited_s` per group.

### Pool of public RPC endpoints

`PUBLIC_RPC_URLS` (comma list, keyless) defaults to api.mainnet-beta.solana.com and solana-rpc.publicnode.com
(both passed the probe on the Mac; solana.drpc.org and rpc.ankr.com/solana did not). Every POST to any pool
URL (also `http.PUBLIC_RPC` and the Helius fallback) goes to the pool:
- Each endpoint is probed (getSignaturesForAddress + getTransaction jsonParsed v1, no key) before first use and
  every 10 min; failing endpoints, and endpoints with 3 failures (429/5xx/errors) in a row, leave the rotation for
  10 min (shared state `data/ratelimit/rpc-pool.json`). `PUBLIC_RPC_PROBE=0` skips probing.
- Each endpoint has its own spacing and window (90 / 10 s, 36 per method) and its own live/bulk lanes.
- `priority="live"` takes the endpoint with the lowest measured response time, `"bulk"` the one with the most
  free window; a retry may go to another endpoint.
- `http.pick_public_rpc(priority)` returns a pool URL for own clients; `http.pool_status()` = {host: {url, ok,
  disabled_until, latency_ms, checked_at, reason}}; `usage()` has calls, 429 and latency_ms per endpoint host.

### Helius monthly credits

Checked in the Helius docs on 2026-10-16 (docs.helius.xyz/docs/billing/credits.md, helius.dev/docs/billing/plans-and-rate-limits):
free plan **1,000,000 credits per calendar month**, 10 RPC req/s, **DAS + Enhanced API together 2 req/s**.
Cost per call: standard RPC 1 (incl. getTransaction, getSignaturesForAddress, getAccountInfo), getProgramAccounts 10,
DAS methods 10, Enhanced Transactions / Wallet API on api.helius.xyz 100, getTransactionsForAddress 10+.

```python
http.credit_cost(url, {"json": payload})   # credits of one request (0 for non-Helius hosts)
http.credits()                    # {service: {credits_today, credits_month, credits_cap, credits_day_budget, share_pct, ...}}
http.credits_left_today()         # this service, None = unlimited (process without a service name)
http.rpc_url(method, helius_url)  # Helius URL, or the public RPC once today's share is used up (standard methods only)
http.throttle(host, cost=n)       # own HTTP clients: pass the request's credit cost (default: host base cost)
http.CreditsExhausted             # raised instead of calling when today's share is used up (a RequestException)
```

- Ledger `data/ratelimit/credits-helius.json`, shared by all processes, survives restarts, resets with the UTC month.
- Month budget `HELIUS_CREDITS_MONTH` (1,000,000) minus reserve `HELIUS_CREDITS_RESERVE_PCT` (10 %), split into shares
  per service (`HELIUS_SHARE_<SERVICE>`, percent): research 50, signals 12.5, safety 12.5, copytrade 12.5, sniper 12.5
  (with 1,000,000 and 10 % reserve: 30,000 credits a day in total, research 15,000, the others 3,750 each).
- Daily budget = (share − used before today) / days left in the month incl. today, so unused credits carry over.
  safety and sniper may use 1.5x their day budget for events, never more than their month share.
- Lending: a service past its own day budget may borrow up to `HELIUS_BORROW_PCT` (50 %) of what the other services
  leave unused today (research usually spends little of its 50 %), only while the whole plan is not ahead of the
  calendar (used <= usable x day / days in month). The lender's own share is never refused. `HELIUS_BORROW_PCT=0` = off.
- Used up today: `post_json` sends standard RPC methods (1 credit) to the public RPC instead; DAS / Enhanced calls
  raise `CreditsExhausted` (callers already treat it as a failed request) instead of emptying the month early.
- DAS + Enhanced: one fixed cross-process spacing of 0.5 s (2 req/s) for api.helius.xyz and DAS methods on any Helius host.
- `credits()` and `credits_left_today()` only read the ledger (no lock), safe to poll from the dashboard.
- New Helius key in `.env` (fresh free quota): the ledger stores only a fingerprint (first 8 hex of sha256, never
  the key). A new fingerprint resets month and day counts for all services, logs `Helius key changed, credit ledger
  reset`, and keeps the old totals in `history` ({month, key_fp, used, until, reason}). A process still running
  with an older key never switches back. `http.credits_status()` = {month, key_fp, key_changed_at, used, limit,
  warn_level (0..1), warn (>= 80 %), history}; every `credits()` row also carries key_changed_at, warn_level, warn.
- Warning in the log once the whole month reaches 80 %. A process without a service name is counted as `other`
  but not limited.

## Wallets and scoring — `core.wallets`, `core.scoring`

```python
from core import wallets, scoring
wallets.add_wallet(conn, address, label="", source="manual")   # manual wallets are always tracked
wallets.smart_wallets(conn, min_score=None) -> list[sqlite3.Row]  # is_smart=1, sorted by score desc
wallets.get_wallet(conn, address) -> sqlite3.Row | None
scoring.rescore_all(conn) -> int                    # recompute wallet_positions + wallets from trades
scoring.wallet_quality(positions, exclude_mint=None) -> dict  # pure function, score 0-100 + parts
scoring.tier_for(score, n_tokens, is_bot, manual) -> 'ultra'|'smart'|'good'|None  # placeholder until model/
```
Wallet score = track record from its own positions: win rate, early entries (bought before a ≥2x peak),
ROI, consistency (profit not from one lucky hit), scaled by confidence (number of tokens). Bots
(very high trade counts per token) are flagged `is_bot` and never smart.

## Signal thresholds and funnel

- Heuristic score: fires at `SIGNAL_MIN_SCORE` (60).
- Model score = P(+50 % in 1h) x 100, a calibrated probability. The threshold is `min(MODEL_SIGNAL_MIN_SCORE, MODEL_SIGNAL_MIN_LIFT x base rate)`, where the base rate is the 1h `base_rate` of the active version's backtest (`model_versions.metrics_json`). It never drops below `MODEL_SIGNAL_FLOOR` (defaults 40 / 2.0 / 10). A fixed 40 needs four times a 10 % base rate and almost never fired.
- The net inflow minimum is `min(SIGNAL_MIN_NET_USD, SIGNAL_MIN_WALLETS x median Ultra/Smart buy of the last 24h)`, never below `SIGNAL_NET_FLOOR_USD` (100). A Smart buy is about $140 on the Mac, so a fixed $500 needed four buys and let 4 of 139 windows with 2+ Smart buyers through in 10 h.
- Every signal cycle logs `signal funnel: N coins -> no smart buyer / <2 entities / net <$500 / score < thr / safety / cooldown / fired`.
- Each cycle evaluates the watchlist plus every coin that had an Ultra or Smart buy inside the signal window (up to 100 coins), whether or not it is watched. The watchlist is ranked by fresh smart buyers first, then recent score. Ranking by score alone kept the same 25 coins for hours: the Mac funnel showed 49 coins evaluated in 10 h, while smart wallets traded 273.
- `python -m signals.funnel --hours 10` runs the same funnel over the stored `signal_metrics`. It is read only.

## Signal backtest, expected value and reasons (signals/)

- **`python -m signals.backtest --hours 48 [--db copy.db] [--folds 4]`** replays the signal rules over the stored trades.
  - Tiers are point in time: the `model_tier_history` of the model version active at that moment. The current tiers are used only when no history exists, and the output then says `pit False`.
  - Outcomes are entry within 5 min, ret_1h, max_ret_1h and a +50 % take profit, each after 6 % round-trip cost.
  - Walk-forward picks the rule set on earlier folds and judges it on the next fold. The in-sample top list is for orientation only.
- **`signals/ev.py`** turns the replay into an expected value per signal.
  - Every 6 h (`SIGNAL_CALIB_EVERY_HOURS`) the signals service replays 72 h (`SIGNAL_CALIB_HOURS`).
  - It fits mean TP return after costs per bucket (entities 1/2/3+, net <200/<500/>=500, smart sellers 0/1+), shrunk toward the global mean (`SIGNAL_EV_SHRINK` 10).
  - It fits on the first two thirds and tests the rule "EV > `SIGNAL_MIN_EV`" on the last third. The table goes active only if that out-of-sample run made money with at least 10 signals.
  - While a table is active, EV > `SIGNAL_MIN_EV` replaces the wallets, net and score minimums. Rug protection still vetoes at a safety factor below 0.6.

### Following signals by hand (signals/plan.py, 2026-10-16)

- **Peak after the call:** update_outcomes also stores max_ret_15m, max_ret_4h, peak_min_1h (minutes to the 1 h high) and dd_before_peak_1h (deepest dip before it). `funnel --outcomes` and the hourly report show hit rates of +25 / +50 / +100 %, median peaks, minutes to the peak, the dip before it, and how many alert targets / stops were hit, per score band and for "all".
- **Several calls per coin:** inside `SIGNAL_COOLDOWN_MIN` a coin is called again when at least one smart wallet that was not in its last call buys now and `SIGNAL_RECALL_MIN` (15) minutes have passed (or the score rose by 15). `call_no` counts calls on the coin in 24 h, and the reason names the previous call and the price move since.
- **Plan in the alert:** entry price, liquidity, target and stop. Target = the 1 h peak `SIGNAL_TARGET_SHARE` (60 %) of the calls of the last 14 days reached; stop = deep enough that 90 % of the calls that reached the target survived their dip, minus 5 points. Defaults +50 % / -30 % until `SIGNAL_PLAN_MIN_N` (8) calls have results. Stored as target_ret / stop_ret.
- **Thin pools:** a candidate whose pool has less than `SIGNAL_MIN_LIQ_USD` (3000, about 2 % price impact for a 0.5 SOL buy) is not called; the funnel counts it as `thin_liq`. Unknown liquidity is not blocked. `funnel` also counts `blocked_loss_veto`: rows that passed every minimum and were stopped only by the model's expected loss.
- **Hard veto checks:** a finding of a safety check in `SIGNAL_VETO_CHECKS` (default `wash`) stops the call whatever the total risk (counted as safety_drop, unless the coin is manually allowed). Wash trading means the volume and any pump are fake; Kavantissa fired at 27 % round-trip volume (risk 34) and was -64 % after 1 h.
- **Latency:** each call stores lag_first_s / lag_last_s (seconds from the first / newest smart buy to the call); the report shows the medians and the alert says how long ago the last smart buy was. Coins smart wallets are buying get their pool trades every cycle and first in line, and the loop starts a cycle every `CYCLE_SECONDS` instead of pausing that long after each one. Each cycle fetches the smart-bought coins' trades first, evaluates and alerts, and only then spends the rest of `TRADES_BUDGET_SECONDS` on the quiet coins (for the next cycle); last_cycle carries alert_s (cycle start to alerts sent) and fetch_s (hot fetch). Single bad prints are filtered both ways (spikes and dips) for highs and the dip before the high.
- **Honest highs (audit round 3):** peaks (max_ret_15m / 1h / 4h / 24h, peak_min, dip) count from `SIGNAL_ENTRY_DELAY_S` (60) after the call, entering at the worse of the call price and the price then, and use `backtest.robust_max`: DexScreener snapshots always stand, a single trade print only with a neighbour within 25 %. The backtest's TP exit uses the same robust max. Unmeasured calls (no price near +1 h, not provably dead) are counted: `outcomes` "all" has n_unmeasured and hit_50_with_unmeasured, `plan.levels` counts them as misses and adds hit_rate_oos (target from the older two thirds, scored on the newest third; the in-sample hit_rate is ~60 % by construction), EV calibration reports how many replayed setups were unmeasurable.
- **Follow-up:** each cycle `plan.check_levels` finds the first target or stop crossing of the calls of the last 4 h (those coins stay watched), stores target_hit_ts / stop_hit_ts and sends one alert ("Ziel erreicht" / "Stopp erreicht"). Crossings older than 15 min (e.g. after a restart) are stored without an alert.
  - Without an active table the fixed minimums count distinct entities (clusters with at most `SIGNAL_MAX_CLUSTER_TIERED` = 3 Ultra/Smart members; bigger clusters are co-buying chains from union-find, their wallets count on their own), not wallets, and the model's own `exp_net_ret` (TP exit, after costs) must be at least `SIGNAL_MODEL_MIN_EXP_NET` (default 0), else the funnel counts `low_ev`.
  - Every run is a row in `signal_calibration`.
- Each signal stores `ev`, `ev_n` and `reason`, a German "Dafür … Dagegen …" text. The Telegram alert shows both.
