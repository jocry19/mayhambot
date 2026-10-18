# mayhambot model — wallet tiers + coin prediction (interfaces)

Owned by the "Wallet-Forschung und Tier-Modell" thread (`model/` and `research/`). signals/, copytrade/,
web/ and the dashboard import only what is listed here. Run from the `mayhambot/` folder.
Pure Python (no numpy/sklearn needed), uses `core.db` and `core.sources`.

## Wallet tiers — `model.tiers`

```python
from model import tier_of, wallet_tiers
tier_of(address, conn=None) -> dict | None
#  {"address", "tier": "ultra"|"smart"|"good"|None, "score": 0-100, "version": int,
#   "components": {"win": .., "early": .., "roi": .., "consistency": .., "timing": .., "influence": .., "rug_avoid": ..},
#   "stats": {"n_tokens", "win_rate", "early_rate", "roi", "realized_pnl_usd", "total_pnl_usd",
#             "median_entry_mcap", "median_min_to_2x", "influence_followers", "influence_usd",
#             "inflow_lift_usd", "consistency", "rug_rate"}}
wallet_tiers(conn=None, tier=None, limit=None) -> list[dict]     # ranked by score, same shape
TIERS = ("ultra", "smart", "good")                                # labels: Ultra Smart / Smart / Good
```

Wallet score (0-100) = weighted sum of the components (each 0..1) x evidence factor sqrt(1-exp(-n/4)).
Component weights start from a prior and are re-learned on every retrain from which components
predicted that copying a wallet's *next* buys made money after our round-trip costs (`win`, not `pumped`),
blended with the prior by amount of evidence. No component may exceed `MODEL_WALLET_WEIGHT_CAP` (0.25) of the
total. The backtest's `wallet_rank` metric checks this walk-forward: mean net (with 95 % lower bound) of the
top fifth of wallets by score under the net-learned weights, the old uncapped pump-learned weights and the prior.
Tier = first rule that matches (all configurable in `.env`, see `model/config.py`):

| tier | score ≥ | coins ≥ | win rate ≥ | also |
|---|---|---|---|---|
| ultra | `MODEL_ULTRA_MIN_SCORE` 72 | `MODEL_ULTRA_MIN_TOKENS` 6 | `MODEL_ULTRA_MIN_WINRATE` 0.45 | PnL > 0, rug rate ≤ `MODEL_MAX_RUG_RATE` |
| smart | `MODEL_SMART_MIN_SCORE` 60 | `MODEL_SMART_MIN_TOKENS` 4 | `MODEL_SMART_MIN_WINRATE` 0.35 | PnL > 0, rug rate ok |
| good  | `MODEL_GOOD_MIN_SCORE` 48 | `MODEL_GOOD_MIN_TOKENS` 3 | `MODEL_GOOD_MIN_WINRATE` 0.25 | PnL > 0 |

Rank caps: at most `MODEL_ULTRA_MAX` (40) Ultra and `MODEL_SMART_MAX` (150) Smart wallets, the best by score;
the rest fall through to the next tier. Components: win rate, early entries (≥2x after entry), ROI, consistency
(profit not from one hit, profitable in both halves of its history), timing (low entry market cap), lead
(buys before most other buyers of a coin), influence (wallets following within 10 min), rug avoidance.

Bots (core `is_bot`, or > `BOT_MAX_TRADES_PER_TOKEN` trades in a coin) never get a tier.

### One source of truth for tiers (signals, dashboard, copy-trader)

After the first successful retrain, `model_wallet_stats` (read via `tier_sync` / `tier_of`) is the truth.
It only ever holds the active model version (replaced in one transaction per retrain).

```python
from model import tier_sync
tier_sync(conn=None) -> None | {address: {"tier", "score", "cluster_id", "exit_capture", "version"}}
```

- `None`: no model version yet. Callers use their own heuristic tiers as a fallback.
- Address in the dict: the model scored it. Copy `tier` (None = scored, not good enough), `score` (0-100),
  `cluster_id` (wallets with the same id are one entity: same funder or repeated co-buys within 60 s,
  so count them once) and `exit_capture` (median sell price / peak since entry, 0..1, None = no exits yet).
- Address missing: the model has no positions for it, so it gets no tier from the model. Signals may keep a
  heuristic tier for it only until the next retrain has backfilled it.
- Read-only for other packages. Never write into `model_*` tables.

`tier_of(address)` returns the same values for one wallet plus components and stats.

## Coin prediction — `model.predict`

```python
from model import score_token, token_signal_history
score_token(mint, now=None, conn=None, store=False) -> dict
#  {"mint", "ts", "version", "score": 0-100 (= P(pump 1h) x 100), "grade": "A".."E",
#   "price_usd", "mcap_usd",
#   "horizons": {"15m": {"p_pump", "threshold", "exp_max_ret"}, "1h": {...}, "6h": {...}},
#   "features": {name: value},
#   "contributions": {"1h": [{"feature", "label", "value", "weight", "contribution", "share"}], ...},
#        # weight = model coefficient (per std-dev), contribution = effect on log-odds for this coin,
#        # share = |contribution| / sum |contributions|  -> "how strongly the model weights this input"
#   "tiers": {"ultra": {"wallets", "net_sol"}, "smart": {...}, "good": {...}},
#   "wallets": [{"address", "tier", "score", "net_sol", "first_ts"}]}
# store=True appends the result to model_predictions (the coin page's signal history).
# Returns None when the coin has no price yet; raises model.predict.ModelNotTrained before the first retrain.
token_signal_history(mint, conn=None) -> list[dict]   # stored predictions for one coin, oldest first,
                                                      # with outcomes ret_15m/ret_1h/ret_6h/max_ret_1h/max_ret_6h
update_outcomes(conn=None) -> int                     # fills outcomes of stored predictions
```
Horizons and pump thresholds: `MODEL_HORIZONS=15m:0.3,1h:0.5,6h:1.0` (max price within horizon ≥ +30 %, +50 %, +100 %).
Features (window `MODEL_WINDOW_MIN`, default 30 min, all as of `now`, no future data):
net SOL inflow and distinct net buyers per tier, tiered buyers in the last 5 min, mean wallet score,
summed influence of the buyers, entry market cap (log10), token age, tiered sell ratio,
entry timing spread, size concentration (HHI), crowd buyers, crowd net USD, price momentum.

## Learning and automatic improvement — `model.learn`

Every retrain: point-in-time events from the walk-forward backtest are split by time (oldest 70 % train, purged by
the horizon; newest 30 % holdout). Candidates = {logistic regression, gradient-boosted trees (`research/gbm.py`)} ×
{all features, without tier / crowd / price-time group}. Each is scored out of sample (log-loss, AUC). The current
champion (cfg stored in the active version's token_model_json) is only replaced when a candidate beats it by more
than 0.5 % log-loss; the winner is refit on all events. Full candidate table: `metrics_json["selection"]`.
Explanations: logistic = coefficient × standardized value; trees = path contributions (Saabas), both summing to the
log-odds. `weights_full(model)` gives one display weight per feature (coefficient, or share of gain for trees).
Extra wallet columns: cluster_id (wallets that repeatedly buy the same coin within 60 s), exit_capture (sell price
as share of the high after entry), median_hold_min. Report also compares stored signals with the copy-trader's
paper P&L per grade (`paper_vs_signals`).

## Retraining — `model.train`

```bash
python -m model retrain            # once: re-tier wallets, rebuild events, walk-forward backtest, fit, new version, report
python -m model retrain --loop     # every MODEL_RETRAIN_HOURS (default 6)
python -m model service            # research discovery + retrain loop together (one long-running process)
python -m model tiers [--tier ultra] | python -m model score <MINT> | python -m model versions
python -m research discover [--loop]   # wallet discovery at scale (see research/README.md)
python -m research discover --shard 1/3 --db /tmp/shard1.db   # parallel runner (cloud), own DB
python -m research merge /tmp/shard1.db                        # merge a runner DB into the app DB
python -m research report              # latest research report as Markdown
# seeding: research/seed_wallets.txt or data/seed_wallets.txt ("<address> [label] # source"),
# Birdeye top traders of fresh pumps when BIRDEYE_API_KEY is set
```
Each retrain stores a new row in `model_versions` (weights, metrics, tier counts) and marks it active,
so the dashboard can show how the model evolves. Walk-forward: events are split by time, every fold
trains only on events whose outcome window ended before the fold starts, wallet tiers inside the
backtest are computed only from trades before each checkpoint.

## Tables (added by `research.store.ensure_schema`, never removed)

| table | key columns |
|---|---|
| `model_versions` | id PK, ts, active, n_events, n_wallets, tier_counts_json, wallet_weights_json, token_model_json, tier_rules_json, metrics_json, notes |
| `model_wallet_stats` | address PK, version, ts, tier, score, n_tokens, win_rate, early_rate, roi, realized_pnl_usd, total_pnl_usd, median_entry_mcap, median_min_to_2x, influence_followers, influence_usd, inflow_lift_usd, consistency, rug_rate, components_json |
| `model_tier_history` | version, address, tier, score (tiered wallets per version) |
| `model_predictions` | id PK, ts, mint, symbol, version, score, p_15m, p_1h, p_6h, exp_ret_1h, price_usd, mcap_usd, n_ultra, n_smart, n_good, net_sol, features_json, contributions_json, wallets_json, ret_15m, ret_1h, ret_6h, max_ret_1h, max_ret_6h |
| `research_reports` | id PK, ts, version, title, markdown, json |
| `research_candles` | mint, tf_sec, ts, o, h, l, c, v (OHLCV history, GeckoTerminal) |
| `research_tokens` | mint PK, pool, supply, created_at, researched_at, early_fetched_at, peak_multiple, pumped, source |
| `research_wallets` | address PK, discovered_via, priority, first_seen, backfilled_at, n_backfills |
| `research_funding` | wallet PK, funder, ts, sol, status (found/none/deep), checked_at: who first sent the wallet SOL. Wallets with the same funder join one cluster (n_clusters); funders of ≥ RESEARCH_FUNDING_HUB_MIN wallets count as exchanges and are ignored |

All timestamps unix seconds UTC. Amounts in SOL are converted with the SOL price stored on the trade.

## Chain access and Helius credits (research)

Bulk research goes through `research.rpc`: public Solana RPC first (`RESEARCH_RPC_URL`, no Helius credits,
spaced by core.http), Helius RPC (1 credit per call) only as a fallback, Helius Enhanced API (100 credits,
up to 100 parsed swaps) only for backfills of tiered and user wallets. Every Helius call is booked by core.http in its shared ledger
(see core/README.md, "Helius monthly credits"; research share 35 % of the month). Once today's research budget
is used, standard RPC goes to the public RPC and an Enhanced backfill falls back to RPC for that wallet
(`CreditsExhausted`, no traceback). `RESEARCH_HELIUS_CREDITS_PER_DAY` > 0 adds a lower own cap (default 0).
Wallets in the `wallets` table that the model has not scored yet are backfilled before new candidates.

## After costs, intel features, uncopyable wallets (2026-10-16)

- Labels per horizon also carry `net` (sell at the pump threshold if reached, else at the horizon end, minus
  `MODEL_COST_ROUNDTRIP` 0.06) and `win` (net > 0). A second model per horizon learns `win`; `score_token` adds
  `horizons[h]["p_win_net"]` and `["exp_net_ret"]` once it is trained (stored as `<h>_net` in `token_model_json`).
- Walk-forward metrics per horizon: `calibration` (5 bins, ECE) and `net` (mean net of all, of the top fifth by
  pump score and by the net model, AUC of the net model). The report shows them as "Nach Kosten".
- 22 point-in-time coin features from `intel.features_at` (prefix `i_`, group `intel`, neutral when unknown,
  `i_coverage` = share known). intel's smart-money features are not used (today's tiers before the first model).
  Every candidate is also tested without the intel group, so they only stay if they help out of sample.
- Tiers need net PnL > 0. Launch/dev bots (≥ 50 % of buys within 3 s of pool creation, or always the first buyer)
  and second-flippers (median hold < 15 s) get no tier (`model.tiers.uncopyable`), nor wash wallets and one-hit
  wallets (> 70 % of the profit from one coin and a net loss without it).
- Wallet component `edge`: median ROI minus the median ROI of the other buyers of the same coins (persists between
  periods, unlike raw ROI); `timing` (low entry mcap) starts with less prior weight.
- Honest labels: outcomes start `MODEL_ENTRY_DELAY_S` (60 s) after the wallet trade; a candle's high/low only counts
  for windows containing the whole candle; isolated bad prints (5x off both neighbours) are dropped; coins picked by
  their later move give no events before the pick, and wallets found through such a pump are not credited for it.
- Walk-forward checkpoints ignore today's `is_bot` flags and use only trades before the cutoff.

**Two wallet measures, side by side:** `net_pnl_usd` / `net_roi` = what the wallet earned itself after costs (tier
basis). `tier_of(addr, with_edge=True)["copy_edge"]` / `wallet_tiers(..., with_edge=True)` = what copying it earned
us (`copytrade.learn.wallet_edge`: our delay, exits, measured costs; `lower` > 0 = proven). Off by default because
copytrade calls `tier_of` itself.

**Fast live scoring:** build the per-cycle context once, `ctx = model.predict.prepare(conn)`, then
`score_token(mint, conn=conn, tiers=ctx["tiers"], shared=ctx["shared"])` for every coin (no full-table scans per
coin). `score_many` does this itself. Models carry `feature_set` / `n_features`; a model from a different release
raises `model.predict.ModelIncompatible` (a `ModelNotTrained`), so callers fall back instead of crashing.

## Wallet features for the shared opportunity table

`model.wallet_rows(addresses)` gives what the active model knows about each scored wallet (tiered or not):
tier, tier_rank (3 ultra .. 0 none), score, components, n_tokens, evidence (1 - exp(-n/4)), win rate, net ROI,
net PnL, cluster, flag (launch_bot / flipper / wash / one_hit). `model.wallet_features(addresses)` folds the
wallets behind one opportunity into flat numbers (`WALLET_FEATURE_NAMES`, all prefixed `w_`): counts per tier,
flagged and one-hit counts, best and mean score, and the top-scored wallet's evidence, net result and
components. Tiers are inputs here, not gates; the decision model learns their weight. Block-0 launch wallets
are flagged `launch_bot` (no tier) and appear as `w_n_flagged` with their score.

Every stored prediction (`score_token(..., store=True)`, so every coin `score_many` scores) is also reported to
`brain.opportunities.report` as source `wallet_model`, ref `pred:<prediction id>`, with shared names (`mc_usd`,
`wallet_score`, `wallet_tier`, `wallets_n`, `inflow_sol`) plus `wallet_model.<coin feature>`,
`wallet_model.p_<h>` / `p_win_net_<h>` / `exp_net_<h>` and `wallet_model.w_*` for all buyers in the window.
Skipped silently while `brain/opportunities.py` does not exist; errors there never break scoring.
