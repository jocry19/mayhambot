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
| `tokens` | mint PK, symbol, name, pool_address, dex, price_usd, mcap_usd, liquidity_usd, pair_created_at, price_change_m5/h1/h24, volume_h1, first_seen, last_updated, tracked |
| `token_prices` | mint, ts, price_usd, mcap_usd, liquidity_usd (DexScreener snapshot each cycle) |
| `trades` | sig, wallet, mint, side ('buy'/'sell'), ts, token_amount, sol_amount, usd_amount, price_usd, source ('geckoterminal' / 'rpc' / 'helius' / 'rpc_early') |
| `wallets` | address PK, label, source, manual, score (0-100), **tier** ('ultra'/'smart'/'good'/NULL), is_smart (=tier in ultra,smart), is_bot, tokens_traded, win_rate, early_rate, realized_pnl_usd, total_pnl_usd, roi, median_peak_multiple, consistency, last_trade_ts, backfilled_at, updated_at |
| `wallet_positions` | wallet, mint, buy_usd, sell_usd, buy_tokens, sell_tokens, realized_pnl_usd, unrealized_pnl_usd, roi, first_buy_ts, first_buy_price, peak_multiple, trades |
| `signal_metrics` | one row per tracked coin per cycle: ts, mint, window_min, buy_usd, sell_usd, net_usd, buy_sol, net_sol, smart_buyers, n_ultra, n_smart, n_good, avg_quality, score, prediction, contributions_json, price_usd, mcap_usd |
| `signals` | id, ts, mint, symbol, score, **mcap_usd** (at signal), price_usd, liquidity_usd, buy_usd, net_usd, **buy_sol, net_sol**, smart_buyers, **n_ultra, n_smart, n_good**, avg_quality, **prediction** (0-1 or NULL), prediction_label, **contributions_json**, model_version, wallets_json, notified, outcomes: ret_5m, ret_15m, ret_1h, ret_6h, max_ret_1h, max_mcap_1h, max_ret_24h |

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
