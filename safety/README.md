# safety/ — Rug-, Honeypot- und Insider-Schutz

Owned by the thread "Rug- und Insider-Schutz". Other packages only call the functions listed under
**Interface**; they never write the `safety_*` tables themselves.

## Interface (for signals/, copytrade/, research/, web/, cli/)

```python
import safety
from core import db
conn = db.connect()

# no network, cheap enough for every live-loop iteration
safety.is_blocked(conn, mint) -> bool          # copy trader / auto-buy: skip when True (never-checked coins: False; checked but no answer: True)
safety.blocked_mints(conn) -> set[str]         # whole block list (auto + manual, minus manual allows)
safety.signal_penalty(conn, mint) -> dict      # {factor 0..1, risk, level, blocked, checked, reasons[:3], ts}
safety.get(conn, mint) -> Assessment | None    # stored result incl. all checks (dashboard detail view)

# network (Solana RPC, Jupiter quote, DexScreener, RugCheck), budgeted per coin
safety.assess(mint, conn, deep=True, force=False) -> Assessment   # uses the stored result while it is fresh
safety.refresh(conn, extra=[mints]) -> list[Assessment]           # one scheduled pass over the most urgent coins

# manual list (dashboard buttons / CLI)
safety.block(conn, mint, reason) / safety.allow(conn, mint, reason) / safety.clear_override(conn, mint)
```

Suggested wiring (done by the owners of those packages):
- **signals/**: `score *= safety.signal_penalty(conn, mint)["factor"]` and store the reasons with the signal;
  optionally call `safety.refresh(conn, extra=[mint])` right before a signal fires.
- **copytrade/**: before every buy `if safety.is_blocked(conn, mint): skip`. For real money a stricter rule is
  available: `a = safety.assess(mint, conn, deep=False)` (about 8 calls, a few seconds) and skip if `a.blocked`.
  `refresh` reads open positions from `ct_positions` itself and re-checks them every `SAFETY_TTL_YOUNG_SEC`
  (dev cash-out, LP pulled), nothing to pass in.
- **launcher**: runs `python -m safety run` as its own service "safety" (log `logs/safety.log`): every
  `SAFETY_REFRESH_EVERY_SEC`, at most `SAFETY_REFRESH_BUDGET_SEC` per pass.
- **web/ cli/**: risk badge from `safety_tokens.score/level/blocked`, detail list from `reasons_json`
  (`[{check, risk, text}]`, worst first) and `positives_json`, history from `safety_history`.

`Assessment.to_dict()` gives JSON for the API; `level` is `low | caution | high | critical | unchecked`, German label in
`level_de` (`niedrig | erhöht | hoch | kritisch | ungeprüft`).

## How the risk score is defined

Each check returns a risk 0–100 with plain German reasons. Several dangers inside one check and the checks
among each other are combined as independent dangers ("noisy OR"):

    score = 100 × (1 − Π (1 − risk_i / 100))

So one 50 and one 50 give 75, not 100, and many small warnings add up slowly. A **hard flag** lifts the score to
at least `SAFETY_HARD_FLAG_FLOOR` (90) and always blocks. Checks that had no data (`unknown`), ran out of budget
(`skipped`) or failed (`error`) add nothing but lower `coverage`; such results are re-checked after a third of the
normal TTL.

| Score | level | effect |
|---|---|---|
| < 25 | niedrig | no penalty |
| 25–49 | erhöht | signal score × 1.0 → 0.2 (linear up to 75) |
| 50–74 | hoch | same, stronger |
| ≥ 75 or hard flag | kritisch | `blocked`: copy trader skips, signal penalty factor 0 |
| checks got no answer | ungeprüft | `blocked` (`SAFETY_BLOCK_UNCHECKED`), signal factor `SAFETY_UNCHECKED_PENALTY_FACTOR`=0.5, retried after 5 min |

**Coverage gate**: a coin is only "niedrig" if the checks actually saw it. When an essential check
(`authorities`, `sell_sim`) has no answer, or fewer than `SAFETY_MIN_COVERAGE`=60 % of the core checks (authorities,
sell_sim, holders, liquidity, lp) answered, a coin without findings gets level `unchecked` with the reason
"Nur teilweise geprüft (...)" and is blocked until a check gets through. Results carried over from an earlier run
count for `SAFETY_CARRY_MAX_AGE_SEC`=2 h. Findings that were made keep their level; a manual allow still wins.
`signal_penalty` returns `checked: False` for such coins.

## Checks

| check | data | hard flag when | risk from |
|---|---|---|---|
| `authorities` | `getAccountInfo(mint)` | mint or freeze authority active; Token-2022 permanent delegate, non-transferable, default-frozen, pausable, transfer fee ≥ `TAX_CRIT` | transfer hook, transfer fee |
| `sell_sim` | Jupiter quote SOL→token→SOL (`SAFETY_SIM_SIZE_SOL`) | no sell route while buy works (honeypot); hidden tax ≥ `TAX_CRIT` | round-trip loss minus price impact minus `FEE_ALLOWANCE` |
| `holders` | `getTokenLargestAccounts` + owners | – | top-10 and top-1 share; pools, bonding curves, lockers and burn are excluded |
| `insiders` (deep) | first tx of each top holder (cached a day) | – | fresh wallets (< `FRESH_WALLET_DAYS`, ≤ `FRESH_WALLET_MAX_TXS` txs), holders with the same funder or funded by the creator, RugCheck insider flags |
| `bundles` (deep) | oldest signatures of the mint + launch txs (cached forever) | – | supply bought in the first `BUNDLE_SLOTS` slots, buys in the creation block, how much they still hold, shared funders |
| `dev` (deep) | creator from launch/RugCheck, balance, last `DEV_TXS_SCANNED` txs | – | dev holding, share of dev buy already gone, SOL sent to exchanges (`exchanges.txt`), tokens spread to other wallets without a sale |
| `liquidity` | DexScreener, our `token_prices` | liquidity fell ≥ `LIQ_DROP_PCT` (90 %) below its 7-day peak | liquidity below `MIN_LIQUIDITY_USD`, liquidity/mcap below `MIN_LIQ_MCAP_RATIO` |
| `lp` | RugCheck markets, DEX type | – | LP locked/burned below `LP_LOCKED_OK_PCT`; CLMM/DLMM pools; pump.fun curve = safe |
| `wash` | our own `trades` table (no network) | round-trip share ≥ `SAFETY_WASH_BLOCK_ROUNDTRIP` (0.5) | round-trip volume of the same wallets (from `SAFETY_WASH_ROUNDTRIP_WARN` 0.25), top-3 volume share (from `SAFETY_WASH_TOP3_WARN` 0.4) |
| `socials` | DexScreener info, RugCheck metadata | – | no website/socials, mutable metadata |
| `exit` | intel features (`intel.latest`, no network) | – | liquidity leaving in the last 60 min, insider sells relative to liquidity, share of smart money exiting, liquidity that appeared and vanished (flash) |
| `history` | intel network features (`net_dev_*`, `net_funder_*`, `net_early_bad/good`, `net_bot_share_60m`; no network, up to `SAFETY_HISTORY_MAX_AGE_SEC`=6 h old) | – | dev's earlier rugs (shrunk while he has few launches), rug rate of the funder network (≥ `SAFETY_HISTORY_MIN_FUNDER_LAUNCHES`=3), launch wallets from earlier rugs, bot volume share ≥ `SAFETY_HISTORY_BOT_SHARE_WARN`=0.4 |
| `rugcheck` | api.rugcheck.xyz report (keyless, optional) | RugCheck says rugged | its other danger/warn items (max 45; items our own checks measure are skipped) |

Funding from an exchange hot wallet never links wallets (one exchange funds thousands of strangers).

Young coins (pair younger than `SAFETY_DEEP_YOUNG_H`=3 h) get one deep check even when nobody trades them yet:
the real rugs of the first weeks all came from young pump-swap coins within 0.5–5 h. A top holder whose owner has
no SOL account (an emptied wallet) counts as a person, not as a program.

## Learning and backtest (no network)

- `python -m safety learn` (also hourly from `refresh`): labels every checked coin after
  `SAFETY_OUTCOME_HORIZON_H`=24 h from `token_prices` (rugged = price or liquidity −90 %), measures per check how
  often its warnings came true (lift) and turns that into a weight 0.5–1.5 (shrunk toward 1 while the sample is
  small). Also: rug rate per level, hit rate/false alarms per threshold (false alarm = a coin that doubled),
  minutes from the first check to the rug. Report in `safety_cache` key `learn_report` (dashboard).
- `python -m safety backtest` (`safety/backtest.py`, also part of `learn`): walk-forward on the sniper's recorded
  launches (`sniper_launches`, read-only). Learns per-feature rug rates (bundle share, top-10, holders never seen,
  dev buy/sold, fast buyers, buyers, bundle wallets, dev launches, wallets of earlier rugs) on the older half,
  recalibrates on the newest 30 % of that half, and judges on the newer half only: AUC, Brier, calibration table
  and the hit rate when at most 5/10/20 % of doublers may be flagged, next to safety's own ramps and the sniper's
  `risk`. The full-history model is in `safety_cache` key `launch_model`;
  `safety.backtest.rug_probability(conn, feats)` gives a launch's rug probability within 60 min.

## Cost (RPC budget)

Only standard JSON-RPC methods (1 Helius credit each): `getAccountInfo`, `getTokenLargestAccounts`,
`getMultipleAccounts`, `getSignaturesForAddress`, `getTransaction`, `getTokenAccountsByOwner`. No DAS and no
Enhanced API. Jupiter, DexScreener and RugCheck are keyless and not Helius.

- **Fixed per-minute budget shared by every process that calls safety** (token bucket in
  `data/ratelimit/safety_<host>.budget`): Helius `SAFETY_HELIUS_PER_MIN`=15, at least `SAFETY_HELIUS_SPACING_SEC`=1 s
  apart; the public RPC (`SAFETY_PUBLIC_RPC_URL`) `SAFETY_PUBLIC_PER_MIN`=20. It sits on top of core.http's
  per-service budget (`MAYHAM_SERVICE=safety`). A call that does not fit is not fired: the check is `skipped`, keeps
  its previous result and runs next cycle.
- **Routing**: the refresher asks the public RPC first and uses Helius only as overflow, and never touches the last
  `SAFETY_HELIUS_RESERVE`=4 Helius calls of the minute; live paths (signal about to fire, real-money buy,
  `priority=True`) prefer Helius, may use the reserve and wait up to `SAFETY_PRIORITY_WAIT_SEC`=10 s.
- **Quick vs deep**: the refresher deep-checks (insiders, launch bundles, dev wallet) only hot coins (open
  `ct_positions`, signals of the last `SAFETY_DEEP_SIGNAL_WINDOW_SEC`=2 h, `extra`) and at most every
  `SAFETY_DEEP_EVERY_SEC`=30 min; everything else gets the quick check, which keeps the last deep findings.
  A quick check turns deep by itself when the top-10 share moved ≥ `SAFETY_DEEP_ON_TOP10_CHANGE_PP`=10 points.
- **Typical RPC calls per coin**: quick 4 (`getAccountInfo` 1, `getTokenLargestAccounts` 1, `getMultipleAccounts` 2);
  deep re-check about 6–12 more (`getSignaturesForAddress` + up to `SAFETY_DEV_TXS_SCANNED`=4 `getTransaction` of the
  dev, profiles only for holders not seen in the last day); first deep check of a young coin up to about 35, once
  (launch: up to `SAFETY_LAUNCH_MAX_PAGES`=8 signature pages + `SAFETY_LAUNCH_MAX_TXS`=12 transactions, resumed across
  cycles when the budget runs out; coins older than `SAFETY_LAUNCH_MAX_AGE_H`=6 h skip it). Per coin at most
  `SAFETY_MAX_CALLS_PER_TOKEN`=30 calls in one run.
- **getTokenLargestAccounts** (public nodes limit it hardest): one holder snapshot per mint is cached in
  `safety_cache` and shared by every process, 10 min (`SAFETY_HOLDERS_TTL_SEC`), 2 min for hot coins
  (`SAFETY_HOLDERS_TTL_HOT_SEC`); on top an own bucket per endpoint for this method, 30/min and ≥1.7 s apart
  (`SAFETY_LARGEST_PER_MIN`, `SAFETY_LARGEST_SPACING_SEC`), i.e. at most 6 per 10 s per endpoint over all processes.
- **Cache**: results per mint with TTL 15 min (pair < 6 h), 2 h (older), 6 h (blocked); launch forever, wallet
  profiles a day. `refresh` runs every `SAFETY_REFRESH_EVERY_SEC`=60 s, at most `SAFETY_REFRESH_MAX_TOKENS`=8 coins and
  stops early when the minute's budget cannot cover the next coin.

## Tables

| table | content |
|---|---|
| `safety_tokens` | latest result per mint: score, level, blocked, coverage, hard_flags_json, reasons_json, positives_json, checks_json (per check: label, status, risk, hard, data), creator, pair_created_at, version, calls |
| `safety_history` | mint, ts, score, level per assessment (chart) |
| `safety_overrides` | mint, action (`block`/`allow`), reason, ts |
| `safety_wallets` | wallet profile cache: n_txs, complete, first_ts, last_ts, funder, funded_sol |
| `safety_launch` | creator, created_slot, created_ts, launch buyers |
| `safety_risks` | mint, ts, per-check risks of each assessment (learning input) |
| `safety_outcomes` | labelled outcome per coin: price/liquidity at the check, min/max return, rugged, minutes to rug |
| `safety_weights` | learned weight per check with its rug rates and lift |
| `safety_cache` | holder snapshots, `learn_report`, `launch_backtest`, `launch_model` |

## CLI

```bash
.venv/bin/python -m safety assess <MINT>          # full check with reasons (add --quick for no deep checks)
.venv/bin/python -m safety refresh                 # one pass over tracked coins and recent signals
.venv/bin/python -m safety learn                   # label outcomes, re-weight checks, report
.venv/bin/python -m safety backtest                # walk-forward on the sniper's launches
.venv/bin/python -m safety run                     # endless loop (own process)
.venv/bin/python -m safety list
.venv/bin/python -m safety block <MINT> "grund"    # allow <MINT> / clear <MINT>
```

Offline: with `SAFETY_OFFLINE=1` or inside any pytest run, `assess` and `refresh` make no network calls
(stored result, or `level='unchecked'`, not blocked) unless a test passes its own `fetcher`.

All thresholds: `safety/config.py` (`SAFETY_*` in `.env`). Tests: `tests/test_safety.py` (fixtures, no network).
