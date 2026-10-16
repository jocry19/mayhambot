# Trading: Copy-Pumps (Modus 1) und Smart-Money-Signale (Modus 2)

**Modus 1 · whale**: folgt Wal-Wallets auf Solana, kauft direkt nach ihnen (bevor die Copy-Trader-Masse kommt) und verkauft in den ersten Pump.
**Modus 2 · signal**: kauft die besten Smart-Money-Signale der Signal-Engine (Tabelle `signals`) mit deinem Preset;
ist das Forschungsmodell (`model.score_token`) trainiert, entscheidet dessen Score und die Note A–E skaliert die Größe.

Jeder Modus hat eine **Betriebsart**: `off` · `manual` (nur Kaufsignale, du handelst selbst) · `semi` (Bot kauft, du verkaufst,
der Bot zeigt Verkaufs-Hinweise) · `auto` (Bot kauft und verkauft). Standard ist alles im **Papiermodus**:
echte Marktdaten, simulierte Fills. Echtgeld braucht beides: `LIVE_TRADING=true` + Schlüssel in `.env`
(Hauptschalter, nur von Hand) **und** Echtgeld für den Modus im Dashboard/Terminal eingeschaltet.

## Starten (aus dem Ordner `mayhambot/`)

```bash
pip install -r requirements.txt
python -m copytrade run            # läuft dauerhaft: Wale finden, bewerten, beobachten, kopieren
streamlit run dashboard/app.py     # Seite "copytrade" im Menü links
```

### Im mayhambot-App-Launcher

```python
import threading, copytrade
stop = threading.Event()
copytrade.start(stop_event=stop, background=True)   # startet die Copy-Trade-Schleife als Thread
...
stop.set()                                           # sauber beenden
```
`copytrade.start()` liest `.env`, läuft im Papiermodus, außer `LIVE_TRADING=true` und ein Schlüssel sind gesetzt,
und schreibt in dieselbe `data/mayhambot.db` wie das Signal-Modul. Die Dashboard-Seite ist
`dashboard/pages/2_copytrade.py` im gemeinsamen Dashboard. Ein eigener Prozess ist nicht nötig;
`python -m copytrade run` bleibt zum Testen einzeln.

Weitere Befehle:

| Befehl | Was er tut |
|---|---|
| `python -m copytrade discover` | Wale auf heißen Coins finden und ihre Historie laden |
| `python -m copytrade rank` | Wal-Käufe mit Minutenkerzen auswerten, Wallets bewerten |
| `python -m copytrade add <wallet> [name]` | einer Wallet fest folgen (oder in `copytrade/wallets.txt` eintragen) |
| `python -m copytrade stats` | Ergebnisse und Top-Wale im Terminal |
| `python -m copytrade backtest` | Rückblick-Test ohne Blick in die Zukunft |
| `python -m copytrade reset-paper` | Papierkonto zurücksetzen |
| `python -m copytrade status` | Betriebsarten, Presets, offene Positionen |
| `python -m copytrade mode <whale\|signal> <off\|manual\|semi\|auto>` | Betriebsart setzen |
| `python -m copytrade preset <whale\|signal> <name>` | Preset zuweisen |
| `python -m copytrade live <whale\|signal> <on\|off>` | Echtgeld je Modus (nur mit .env-Hauptschalter) |
| `python -m copytrade pause` / `resume` | neue Käufe stoppen / wieder erlauben |
| `python -m copytrade buy <mint> [sol]` / `sell <id>` | Auftrag an die laufende App |
| `python -m pytest tests/test_copytrade.py` | Tests (offline) |

## Schnittstelle für Dashboard und Terminal (web/, cli/)

Alles liegt in der Datenbank; die laufende App liest Änderungen sofort, ohne Neustart.
Aufträge (kaufen/verkaufen) führt die laufende Copytrade-Schleife aus (alle ~5 s), so bleibt der Schlüssel in einem Prozess.

```python
from copytrade import control, stats, store
from copytrade.config import load
conn = store.connect(load())                       # gleiche DB wie core, legt ct_-Tabellen an

# Betriebsart, Presets, Schalter
control.STRATEGIES                                 # ("whale", "signal")
control.MODES                                      # ("off", "manual", "semi", "auto"); Labels: control.MODE_LABELS
control.mode(conn, "signal") / control.set_mode(conn, "signal", "semi")
control.presets(conn) -> {name: Preset}            # eingebaut: vorsichtig, standard, aggressiv
control.preset(conn, "whale") -> Preset            # aktives Preset des Modus
control.save_preset(conn, "mein", control.Preset(size_sol=0.2, stop_loss=0.15, ...))
control.use_preset(conn, "whale", "mein")
control.paused(conn) / control.set_paused(conn, True)          # stoppt alle neuen Käufe
control.live_wanted(conn, "whale") / control.set_live(conn, "whale", True)   # wirkt nur mit .env-Hauptschalter
load().live_ready                                  # True = .env-Hauptschalter an

# Aufträge an den Bot
control.request_sell(conn, position_id)            # -> ct_commands
control.request_buy(conn, mint, size_sol=None, strategy="manual")   # du verkaufst (managed='semi')

# Statistiken (pandas DataFrames / dicts)
stats.positions(conn, mode=None, strategy=None)    # alle Positionen, + hold_min, entry_delay_s, current_pct
stats.summary(df) -> dict                          # trades, open, win_rate, pnl_sol, avg_pnl_pct, avg_win_pct, avg_loss_pct,
                                                   # profit_factor, max_drawdown_sol, avg_hold_min, best_pct, worst_pct
stats.by_strategy(conn, mode=None)                 # summary je Modus (whale / signal / manual)
stats.equity(conn, mode)                           # Equity-Kurve (ts, balance_sol, open_value_sol, equity_sol)
stats.exit_reasons(df)                             # trades, avg_pct, pnl_sol je Ausstiegsgrund
stats.alerts(conn, since_ts=0, strategy=None)      # Einstiege: action 'bought' oder 'signal' (manuell, nicht gekauft)
stats.coin_history(conn, mint)                     # {positions, alerts, whale_swaps} für die Coin-Seite
stats.wallet_table(conn)                           # Wal-Ranking (Pump-Quote, erster Pump, Copy-Ansturm, EV, Score, Kopie-PnL)
                                                   # + tier, tier_source, model_score, cluster_id, exit_capture aus `wallets`,
                                                   #   entity (= cluster:<id> oder Adresse; ein Cluster = ein Wal)
store.wallet_info(conn, wallet)                    # {tier, model_score, cluster_id, exit_capture, tier_source}
stats.outcomes(conn)                               # jeder ausgewertete Wal-Kauf mit Pump/Timing-Messung
stats.backtest(conn, settings)                     # Modus 1 walk-forward
stats.signal_backtest(conn, preset)                # Modus 2 mit Preset auf vergangenen Signalen
stats.events(conn, limit)                          # Protokoll
strategy.EXIT_NAMES                                # deutsche Namen der Ausstiegsgründe

# Selbstlernen (copytrade/learn.py, siehe Abschnitt „Selbstlernen“)
learn.status(conn)                                 # je Modus: an/aus, ausgewertete Einstiege je Art, fehlende bis zum Tuning,
                                                   # aktive Version (dict mit take_profit, stop_loss, ..., version), Wal-Versionen
learn.versions(conn, strategy=None, scope=None)    # Parameter-Historie + Papier/Live-PnL der Trades je Version
                                                   # (id, dt, scope 'mode'|Wallet, params JSON, n_train, n_test, train_gain,
                                                   #  test_gain, baseline_test_gain, accepted, active, trades, pnl_sol, win_rate)
learn.variant_surface(conn, "whale", "take_profit", "stop_loss")   # Heatmap: Ø Netto-Gewinn je Parameterpaar
learn.attribution(conn, "signal")                  # Ø Gewinn je Merkmal-Bucket (tier, grade, mcap, liq, score, buyers, size)
learn.exit_rule_comparison(conn, "whale")          # eigene Trades: tatsächlich vs. bester Ziel/Trailing/Copy-Ansturm/kurz/lang
learn.params_for(conn, "whale", wallet=None)       # was der Bot gerade für neue Positionen nimmt (None = Preset)
control.set_value(conn, "autotune:whale", "0")     # Selbstlernen je Modus aus ("1" = an, Standard)
```

`Preset`-Felder: `size_sol, max_open, max_daily_loss_sol, take_profit, learned_tp, stop_loss, trail_activate,
trail_drawdown, max_hold_min, min_score, min_model_score, require_model, min_smart_buyers, min_liquidity_usd, max_mcap_usd, max_signal_age_s,
slippage_bps` (Anteile: 0.2 = 20 %; `min_score`, `min_smart_buyers`, `max_signal_age_s` gelten für Modus 2).

### Tabellen (Präfix `ct_`, Spalten werden nur ergänzt)

| Tabelle | Inhalt |
|---|---|
| `ct_positions` | id, mode (paper/live), strategy (whale/signal/manual), managed (auto/semi), wallet, mint, symbol, signal_id, opened_at, entry_price_sol, entry_price_usd, entry_mcap_usd, size_sol, tokens, entry_cost_sol, take_profit, stop_loss, trail_activate, trail_drawdown, max_hold_min, high_price_sol, last_price_sol, last_price_usd, last_mcap_usd, status (open/closed), closed_at, exit_price_sol, exit_mcap_usd, proceeds_sol, pnl_sol, pnl_pct, exit_reason, exit_hint (Verkaufs-Hinweis im semi-Modus), meta (JSON: Signal-/Wal-/Modell-Infos, Fill-Quelle, Impact) |
| `ct_alerts` | id, ts, strategy, mint, symbol, wallet, price_sol, price_usd, mcap_usd, score, action (bought/signal), reason, data (JSON), position_id |
| `ct_control` | key, value: `mode:<strategy>`, `preset:<strategy>`, `live:<strategy>`, `paused` |
| `ct_presets` | name, data (JSON Preset), builtin |
| `ct_commands` | id, ts, action (buy/sell), position_id, mint, size_sol, strategy, status (pending/done/failed), result |
| `ct_equity` | ts, mode, balance_sol, open_value_sol, equity_sol (jede Minute) |
| `ct_wallets`, `ct_swaps`, `ct_buy_outcomes`, `ct_wallet_rank` | Wal-Tracking, Wal-Käufe, Pump/Timing-Messung je Kauf, Wal-Ranking |
| `ct_events` | ts, level, kind (buy/sell/hint/skip/wait/learn/error/discover/start/stop), message, data |
| `ct_evals` | kind (trade/whale_buy/signal), ref, strategy, wallet, mint, ts, gains (JSON: Netto-Gewinn je Gitter-Variante), actual_gain, sim_actual, features (JSON) |
| `ct_param_versions` | id, ts, strategy, scope ('mode' oder Wallet), params (JSON inkl. gelernter Filter `block`), n_train, n_test, train_gain, test_gain, baseline_test_gain, accepted, active, notes |
| `ct_candles` | 1-Minuten-Kerzen, die das Lernen selbst geholt hat (sonst `research_candles`) |

`ct_positions` hat zusätzlich `safety_level` (low/caution/high/critical/unchecked), `safety_score` (0–100) und
`safety_reasons` (JSON-Liste deutscher Gründe) vom Rug-Schutz beim Kauf; dieselben Infos stehen in
`ct_alerts.data.safety` und `ct_positions.meta.safety`. `param_version` (welche gelernte Version die Ausstiege gesetzt hat, NULL = Preset) und `use_inflow_exit`.

## Tiers, Cluster, Abfrage-Takt

- Tiers kommen aus `wallets.tier` (vom Forschungsmodell synchronisiert, eine Wahrheit für Dashboard, Signale und
  Copy-Trader); automatisch verfolgt werden `ultra` und `smart`, nach `model_score` sortiert.
- Wallets mit derselben `cluster_id` sind ein Wal: ein Coin wird pro Cluster nur einmal kopiert (6 h Fenster), und
  das Lernen tunt Ausstiege pro Cluster. `exit_capture` (wie viel vom Pump der Wal mitnimmt) ist ein Lern-Merkmal.
- Abfrage-Takt pro Wallet: letzter Swap < 6 h alle `CT_WALLET_POLL_SECONDS`, < 48 h alle `CT_POLL_WARM_SECONDS` (60),
  sonst alle `CT_POLL_COLD_SECONDS` (300); manuelle Wallets immer schnell.
- **Websocket:** Für die heißen Wallets (letzter Swap < 6 h, manuelle) hält der Bot eine `logsSubscribe`-Subscription
  je Wallet auf `CT_WS_URL` (Standard: öffentlicher Mainnet-Websocket, `off` = nur Polling, max. `CT_WS_MAX_WALLETS`=40).
  Ein Wal-Kauf ist damit nach ~1 s bekannt statt beim nächsten Poll. Abbrüche: Neuverbindung mit Backoff 1→60 s.
  Abgedeckte Wallets werden nur noch als Netz im 60-s-Takt gepollt; jede Transaktion wird nur einmal gehandelt.
  Einmal pro Stunde eine Logzeile: Swaps per Websocket (mit mittlerer Zeit Meldung → erkannter Swap) vs. per Polling,
  RPC-Aufrufe öffentlich vs. Helius.
- Spuren am öffentlichen RPC (core.http): `live` für alles rund um einen frischen Wal-Kauf oder eigenen Trade
  (getTransaction nach Websocket-Treffer oder Poll-Fund, Polls heißer Wallets ohne Websocket, getTokenSupply,
  getBalance, sendTransaction, getSignatureStatuses), `bulk` für Backfill, Wal-Suche und die 60-s-Netzpolls;
  die Methode wird immer mitgegeben.
- **RPC:** Lesen (`getSignaturesForAddress`, `getTransaction`, `getTokenSupply`) für Polls, Websocket-Treffer,
  Wal-Suche und Backfill geht zuerst an `CT_POLL_RPC_URL` (Standard: öffentliches Mainnet-RPC, wenn `SOLANA_RPC_URL`
  Helius ist; `off` = alles über Helius), Helius nur als Fallback. Helius bleibt für Live-Pfade (`getBalance`,
  `sendTransaction`, `getSignatureStatuses`, eigene Swap-Bestätigung). Ist copytrades Helius-Tagesanteil
  (core.http, 15 %) verbraucht, leitet core auch diese Standard-Aufrufe an das öffentliche RPC.
- Wal-Suche auf Trend-Coins höchstens alle `CT_DISCOVER_EVERY_SECONDS` (1800).

## So entscheidet der Bot (Modus 1)

1. **Wale finden**: große Käufer (≥ `CT_MIN_WHALE_BUY_SOL`) auf aktuell heißen Coins (DexScreener, GeckoTerminal), dazu die Smart Wallets des Signal-Moduls und deine eigenen.
2. **Wale bewerten**: für jeden früheren Kauf eines Wals werden Minutenkerzen geladen und gemessen:
   ob ein Pump folgte (≥ `CT_PUMP_MIN_GAIN`), wie groß der erste Pump war, nach wie vielen Minuten er oben war,
   und wann die Copy-Trader kamen (Volumen ≥ `CT_INFLOW_SPIKE_FACTOR` × ruhige Basis). Daraus: was **wir** mit unserer
   Verzögerung (`CT_ASSUMED_LATENCY_S`) nach Kosten verdient hätten. Score = dieser Erwartungswert × Vertrauen (Anzahl Käufe).
   Wallets mit vielen Daten und negativem Score werden abgeschaltet, Bots (>80 Transaktionen in 15 min) ignoriert.
3. **Kopieren**, wenn: Wal gut bewertet, Kauf jünger als `CT_MAX_ENTRY_DELAY_S`, Preis seit dem Wal nicht schon
   mehr als `CT_MAX_RUN_SINCE_WHALE` gestiegen, Copy-Ansturm noch nicht da, Liquidität ≥ `CT_MIN_LIQUIDITY_USD`,
   Risiko-Limits frei. Im Papiermodus werden unbewertete Wale mit halber Größe "erkundet", damit das Ranking schneller lernt.
4. **Verkaufen** in den ersten Pump: Ziel = `CT_TP_FRACTION` × typischer erster Pump dieses Wals (begrenzt auf
   `CT_TP_MIN`…`CT_TP_MAX`), oder wenn der Copy-Ansturm abflaut (5-min-Käufe 40 % unter Spitze) und wir im Plus sind,
   Trailing-Stop, Stop-Loss, Wal verkauft, oder die typische Zeit bis zum Gipfel ist um. `CT_EXIT_MODE=target` nimmt
   stattdessen festes `CT_TAKE_PROFIT`, `inflow_peak` verkauft nur am Ansturm-Ende.

## Modus 2 · Smart-Money-Signale

Neue Zeilen der Tabelle `signals` (Signal-Engine) werden alle ~5 s gelesen. Gekauft wird, wenn der Modell-Score
(`model.score_token`: Pump-Chance in 1 h × 100) ≥ `min_model_score` ist; solange das Modell noch nicht trainiert ist
(`ModelNotTrained`), gilt der Signal-Score ≥ `min_score`, oder es wird mit `require_model=True` gar nicht gekauft.
Die Note A–E skaliert die Größe (A 1,5×, B 1,2×, C 1×, D 0,6×, E 0,4×). Außerdem nötig: ≥ `min_smart_buyers` Smart Wallets, Signal jünger als
`max_signal_age_s`, Liquidität/Market Cap im Preset-Rahmen, Preis seit dem Signal nicht schon +50 %.
Ausstieg: Ziel, Stop, Trailing, Zeit, oder „Smart Money steigt aus“ (die Wallets des Signals haben ≥ 50 % verkauft).
Ultra- und Smart-Wallets des Modells werden außerdem als Wale für Modus 1 beobachtet.

## Rug-Schutz (safety/)

Vor jedem Kauf, in jedem Modus und auch bei deinem Kauf-Auftrag: `safety.is_blocked` (ohne Netz, Coins ohne Prüfung
gehen durch). Mit echtem Geld zusätzlich direkt vor der Order `safety.assess(mint, deep=False)` (ca. 8 Abfragen);
blockiert oder nicht prüfbar heißt: kein Echtgeld-Kauf. Übersprungene Käufe stehen mit Grund im Protokoll („skip“).
Eine manuelle Freigabe (`safety.allow`) hat Vorrang. Die laufende Überwachung offener Positionen macht der
Safety-Dienst selbst aus `ct_positions`.

## Selbstlernen (`learn.py`, `sim.py`)

Läuft in der Wartungsschleife (alle 10 min) oder von Hand mit `python -m copytrade learn`.

1. **Auswerten:** Jeder abgeschlossene Trade, jeder gemessene Wal-Kauf und jedes vergangene Smart-Money-Signal wird
   auf 1-Minuten-Kerzen gegen ein Gitter aus 1296 Varianten nachgespielt: Einstieg 15/60/180 s nach dem Wal,
   Ziel +15…+150 %, Stop 10/20/30 %, Trailing aus/15→8 %/30→15 %, Haltezeit 15–240 min, Verkauf am Ende des
   Copy-Ansturms ja/nein, Wal-Verkauf als Zwangsausstieg. Kosten (3 %) sind abgezogen; im selben Kerzen-Minute wird
   der Stop vor dem Ziel geprüft (vorsichtig). Erst nach 4,5 h ist ein Einstieg auswertbar.
2. **Tunen:** je Modus (ab 40 Einstiegen) und je Wal (ab 20) gewinnt die Variante, die auf den älteren 70 % am meisten
   bringt. Übernommen wird sie nur, wenn sie auf den neueren 30 % (nie gesehen) mindestens 1 %-Punkt pro Trade besser
   ist als die aktuellen Einstellungen und dort im Plus liegt. Jede Prüfung landet in `ct_param_versions`.
   Eigene Trades zählen doppelt; `notes.calibration_gap` zeigt, wie weit Papier-Ergebnis und Simulation auseinanderliegen.
3. **Filter:** Merkmal-Gruppen (Wal-Tier, Modell-Note, Market Cap, Liquidität, Signal-Score, Anzahl Smart Wallets,
   Wal-Größe), die mit der gewählten Variante in Training **und** Test verlieren (≥15/≥8 Fälle), werden übersprungen.

Nie angefasst: Positionsgröße, max. offene Positionen, Tagesverlust-Limit, Live-Schalter. Gelernter Stop nie weiter
als der Stop deines Presets, Haltezeit nie länger als dein Maximum (auch wenn du das Preset später verschärfst).
Im manuellen Modus wird nur das gelernte Kursziel im Hinweis gezeigt.

## Papier-Fills

Kauf/Verkauf werden über ein echtes Jupiter-Quote für genau diese Größe bewertet (echte Route, echter Price Impact);
ohne Quote per Constant-Product-Schätzung aus der DexScreener-Liquidität plus DEX-Gebühr. Dazu kommen
`CT_PAPER_ADVERSE_SLIPPAGE` (Standard 2 %, für Latenz im Ansturm) sowie Netzwerk- und Priority-Fee.

## Live-Modus (echtes Geld)

Nur wenn **beides** gesetzt ist: `LIVE_TRADING=true` und ein Schlüssel (`SOLANA_KEYPAIR_PATH=/pfad/keypair.json`
empfohlen, oder `SOLANA_PRIVATE_KEY` in `.env`). Dann zusätzlich `pip install solders`. Swaps laufen über die
Jupiter-Swap-API, signiert lokal; die echten Mengen werden aus der bestätigten Transaktion gelesen.
Limits: `CT_POSITION_SIZE_SOL`, `CT_MAX_OPEN_POSITIONS`, `CT_MAX_DAILY_LOSS_SOL`, `CT_STOP_LOSS`.
Live folgt nur bewerteten Walen. Teste erst mit kleiner Größe.

## Einstellungen (`.env`)

```ini
# Daten (alles optional, ohne Schlüssel laufen DexScreener, GeckoTerminal, öffentliche Solana-RPC und Jupiter lite)
SOLANA_RPC_URL=            # eigene RPC, z. B. Helius/Triton/QuickNode: viel schnelleres Wal-Polling
HELIUS_API_KEY=            # setzt automatisch die Helius-RPC, wenn SOLANA_RPC_URL leer ist
SOLANA_RPC_MAX_RPS=4       # mit bezahlter RPC auf 20-50 erhöhen
BIRDEYE_API_KEY=           # bessere Minutenkerzen für sehr junge Coins
JUPITER_API_URL=https://lite-api.jup.ag
JUPITER_API_KEY=           # Jupiter Pro: höhere Limits (dann JUPITER_API_URL=https://api.jup.ag)

# Wale
CT_WATCH_WALLETS=          # kommagetrennt, immer beobachten
CT_MAX_TRACKED_WALLETS=40
CT_WALLET_POLL_SECONDS=8
CT_POLL_WARM_SECONDS=60
CT_POLL_COLD_SECONDS=300
CT_POLL_RPC_URL=                      # leer = öffentliches RPC für Polls, wenn Helius das Haupt-RPC ist; off = Helius
CT_DISCOVER_EVERY_SECONDS=1800
CT_WS_URL=wss://api.mainnet-beta.solana.com   # off = nur Polling
CT_WS_MAX_WALLETS=40
CT_MIN_WHALE_BUY_SOL=2

# Bewertung
CT_PUMP_MIN_GAIN=0.25
CT_PUMP_RETRACE=0.15
CT_PUMP_WINDOW_MIN=120
CT_INFLOW_SPIKE_FACTOR=3
CT_MIN_WALLET_SAMPLES=4
CT_MIN_WALLET_SCORE=0.05
CT_ASSUMED_LATENCY_S=15

# Einstieg
CT_MIN_LIQUIDITY_USD=15000
CT_MAX_TOKEN_AGE_H=72
CT_MAX_ENTRY_DELAY_S=90
CT_MAX_RUN_SINCE_WHALE=0.15

# Ausstieg
CT_EXIT_MODE=first_pump    # first_pump | target | inflow_peak
CT_TAKE_PROFIT=0.35
CT_TP_MIN=0.15
CT_TP_MAX=1.5
CT_TP_FRACTION=0.7
CT_STOP_LOSS=0.20
CT_TRAIL_ACTIVATE=0.15
CT_TRAIL_DRAWDOWN=0.10
CT_MAX_HOLD_MIN=60
CT_EXIT_ON_WHALE_SELL=true
CT_PRICE_POLL_SECONDS=5

# Größe und Risiko
CT_POSITION_SIZE_SOL=0.5
CT_MAX_OPEN_POSITIONS=5
CT_MAX_DAILY_LOSS_SOL=2
CT_PAPER_START_SOL=10
CT_PAPER_ADVERSE_SLIPPAGE=0.02
CT_SLIPPAGE_BPS=300
CT_PRIORITY_FEE_SOL=0.0005

# Live (nur bewusst einschalten)
LIVE_TRADING=false
SOLANA_KEYPAIR_PATH=
SOLANA_PRIVATE_KEY=
```

## Daten

Tabellen mit Präfix `ct_` in `data/mayhambot.db` (gleiche Datei wie das Signal-Modul). Wal-Swaps landen zusätzlich in
der Core-Tabelle `trades` (source = `copytrade`), damit die Signale davon profitieren.
