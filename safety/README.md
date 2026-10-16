# safety/ — Rug-, Honeypot- und Insider-Schutz

Owned by the thread "Rug- und Insider-Schutz". Other packages only call the functions listed under
**Interface**; they never write the `safety_*` tables themselves.

## Interface (for signals/, copytrade/, research/, web/, cli/)

```python
import safety
from core import db
conn = db.connect()

# no network, cheap enough for every live-loop iteration
safety.is_blocked(conn, mint) -> bool          # copy trader / auto-buy: skip when True (unchecked coins: False)
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

`Assessment.to_dict()` gives JSON for the API; `level` is `low | caution | high | critical`, German label in
`level_de` (`niedrig | erhöht | hoch | kritisch`).

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
| `wash` | our own `trades` table (no network) | – | round-trip volume of the same wallets, top-3 volume share |
| `socials` | DexScreener info, RugCheck metadata | – | no website/socials, mutable metadata |
| `rugcheck` | api.rugcheck.xyz report (keyless, optional) | RugCheck says rugged | its other danger/warn items (max 45; items our own checks measure are skipped) |

Funding from an exchange hot wallet never links wallets (one exchange funds thousands of strangers).

## Cost

Every assessment has its own call budget (`SAFETY_MAX_CALLS_PER_TOKEN`, default 70) on top of core.http's
shared per-host limits. A first deep check of a young coin costs about 30–65 calls (up to `SAFETY_LAUNCH_MAX_PAGES`=15 more to page back to the launch of a busy pump coin, once); launch data is stored
forever and wallet profiles for a day, so a re-check costs about 15–25. `refresh` checks at most
`SAFETY_REFRESH_MAX_TOKENS` coins and stops after `SAFETY_REFRESH_BUDGET_SEC`. TTL: 15 min for coins younger than
6 h, 2 h for older ones, 6 h for blocked ones.

## Tables

| table | content |
|---|---|
| `safety_tokens` | latest result per mint: score, level, blocked, coverage, hard_flags_json, reasons_json, positives_json, checks_json (per check: label, status, risk, hard, data), creator, pair_created_at, version, calls |
| `safety_history` | mint, ts, score, level per assessment (chart) |
| `safety_overrides` | mint, action (`block`/`allow`), reason, ts |
| `safety_wallets` | wallet profile cache: n_txs, complete, first_ts, last_ts, funder, funded_sol |
| `safety_launch` | creator, created_slot, created_ts, launch buyers |

## CLI

```bash
.venv/bin/python -m safety assess <MINT>          # full check with reasons (add --quick for no deep checks)
.venv/bin/python -m safety refresh                 # one pass over tracked coins and recent signals
.venv/bin/python -m safety run                     # endless loop (own process)
.venv/bin/python -m safety list
.venv/bin/python -m safety block <MINT> "grund"    # allow <MINT> / clear <MINT>
```

Offline: with `SAFETY_OFFLINE=1` or inside any pytest run, `assess` and `refresh` make no network calls
(stored result, or `level='unchecked'`, not blocked) unless a test passes its own `fetcher`.

All thresholds: `safety/config.py` (`SAFETY_*` in `.env`). Tests: `tests/test_safety.py` (fixtures, no network).
