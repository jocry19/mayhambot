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
predicted that a wallet's *next* buys pumped (blended with the prior by amount of evidence).
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
