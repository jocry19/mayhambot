# sniper/ — Launch-Sniper mit Bundle- und Insider-Erkennung

Owned by the thread "Launch-Sniper mit Bundle-Erkennung". Erkennt neue Solana-Memecoin-Launches (pump.fun und
bonk über PumpPortal, Migrationen, neue Pools anderer DEXe über GeckoTerminal) in Sekunden, prüft jeden auf
Bundles und Insider, handelt die aussichtsreichen **nur im Papiermodus** und misst für jeden Launch, auch die
übersprungenen, was nach 1, 5, 15 und 60 Minuten passiert ist. Echte Käufe laufen später nur über copytrade/.

```bash
.venv/bin/python -m sniper run                         # Dauerbetrieb (Launcher-Dienst "sniper", Log logs/sniper.log)
.venv/bin/python -m sniper watch --minutes 15 --db /tmp/sniper-test.db --record /tmp/launches.jsonl
.venv/bin/python -m sniper replay /tmp/launches.jsonl --db /tmp/replay.db   # offline nachspielen (nur Datenstrom)
.venv/bin/python -m sniper stats [--hours 24] [--json]
.venv/bin/python -m sniper list [-n 30] [--deep] [--entered]
.venv/bin/python -m sniper show <MINT>                 # Gründe, Bundle-Wallets, Geldquellen, Ergebnis
.venv/bin/python -m sniper wallet <ADRESSE>            # bei welchen Launches war die Wallet Bundle/Insider/Dev
.venv/bin/python -m sniper reset-paper
```

## Interface (für web/, cli/, copytrade/, model/)

```python
import sniper
from core import db
conn = db.connect()
sniper.stats(conn, hours=24) -> dict        # Übersicht, Ergebnisgruppen, Filterwert, Score-Buckets, Schwellen-Sweep, Papier
sniper.recent(conn, limit=50, decision=None, stage=None) -> list[dict]   # Launch-Liste (reasons, positives, skip_label)
sniper.launch(conn, mint) -> dict | None    # Detail: Zeile + reasons + features + wallets + curve + positions
sniper.paper_stats(conn, since=0) -> dict   # Guthaben, offen/geschlossen, Trefferquote, PnL, Ausstiegsgründe
sniper.wallet_history(conn, wallet) -> list[dict]
```

`stats()` liefert: `launches, stage2, entered, bundle_flagged, insider_flagged`; `groups` (entered / skipped_deep /
skipped_stage1 / all) je mit `n, median_ret_1m..60m, median_max_ret_60m, hit_rate_2x, rug_rate`; `filters` je
Skip-Grund mit `label, n, missed_winners` (Max ≥ +100 % in 60 min) und `avoided_rugs` (≤ −80 % nach 60 min);
`calls_per_deep_launch {rpc, pump_api}`; `score_buckets`; `threshold_sweep` (was andere Mindest-Scores gekauft
hätten, gemessen an echten Ergebnissen); `paper`. Alle Renditen sind relativ zum Market Cap **im
Entscheidungsmoment**, also zu dem Preis, den wir hätten bekommen können, nicht zum Launch-Preis.

Wünsche an andere Pakete (über den Coordinator):
- **web/ + cli/**: Seite "Launches": Tabelle aus `sniper.recent`, Filter nach Entscheidung/Grund, Kacheln aus
  `sniper.stats` (Filterwert-Tabelle, Gruppen-Vergleich, Schwellen-Sweep als Linie), Detail aus `sniper.launch`
  (Gründe, Bundle-Wallets mit Geldquelle, Curve-Chart aus `curve`, Papier-Position).
- **copytrade/** (später, Modus 3 "Launch"): echte Käufe nur für `sniper_launches.decision='enter'`, gleiche
  Ausstiegsregeln (`sniper_positions.params_json`), Freigabe nur durch den Nutzer.
- **model/ research/**: `sniper_launch_wallets` (Bundle-, Insider-, Dev- und frühe Wallets je Launch mit Ergebnis)
  als Feature "bekannte Bundler/Rugger" und als Quelle neuer Wallets; `features_json` für ein gelerntes Launch-Modell.

## Ablauf pro Launch

1. **Erstellung** (PumpPortal `subscribeNewToken` + `subscribeMigration`, eine Websocket-Verbindung, kostenlos):
   Zustand im Speicher, sofort ein Trade-Abo für den Coin. **Trades pro Coin** liefert PumpPortal nur mit einem
   API-Key mit mindestens 0,02 SOL Guthaben (`SNIPER_PUMPPORTAL_API_KEY`, Test 10/2026). Ohne Key (Standard) kommen
   sie kostenlos aus dem **Solana-RPC-Websocket** (`sniper/rpcws.py`): ein `logsSubscribe` je Coin (`mentions`),
   pump.funs `TradeEvent` aus der Zeile `Program data:` dekodiert (Mint, SOL, Tokens, Seite, Trader, Curve-Reserven,
   Slot). Lehnt PumpPortal Trade-Abos ab, wechselt der Dienst automatisch auf diesen Weg und meldet es als WARNING.
   bonk-Launches haben dort keinen Decoder: sie werden erfasst und gemessen, aber mit `no_stream` übersprungen.
2. **Stufe 1** nach `SNIPER_DECIDE_SEC` (20 s), **kostenlos** aus dem Datenstrom: organische Käufer, Netto-SOL,
   Kaufdruck, Smart Money (`wallets.tier`), Dev-Anteil und -Verkäufe, Top-10, Sniper-Bots in den ersten 2 s und die
   **Curve-Schätzung für Bundles**: Was die Curve seit der Erstellung verloren hat, minus was die gesehenen Trades
   erklären, wurde zusammen mit der Erstellung gekauft (Jito-Bundle, bevor unser Abo griff). Zu wenig Aktivität oder
   Dev-Verkauf ≥ 50 % → übersprungen. Der Rest kommt nach Pre-Score sortiert in die Warteschlange.
3. **Stufe 2** (höchstens `SNIPER_DEEP_PER_MIN` je Minute, ältere Kandidaten als `SNIPER_MAX_QUEUE_SEC` verfallen):
   - eigene Tabellen ohne Netz: Dev-Historie (`sniper_launches`), bekannte Bundler/Rugger (`sniper_launch_wallets`
     bei gerugten Launches), Geldquellen aus `research_funding` und `safety_wallets`, Cluster aus `wallets.cluster_id`
     und `model_wallet_stats`;
   - Socials aus dem Metadaten-JSON des Coins (`uri` der Erstellung, 1 Call); die pump.fun-API
     (`SNIPER_USE_PUMP_API`, aus: die v3-Pfade antworten seit 10/2026 mit 400/404) bleibt als Option;
   - nur wenn das fehlt, RPC: `getSignaturesForAddress(mint)` für den Erstellungs-Slot (1 Call) und bis zu
     `SNIPER_BUNDLE_TXS_FETCHED` Bundle-Transaktionen; Geldquellen per RPC nur, wenn danach noch Budget übrig ist;
   - `model.score_token`, falls ein Modell trainiert ist (`ModelNotTrained` wird abgefangen).
   Ergebnis: Score mit Gründen. Bundle-, Insider- und harte Befunde werden übersprungen und mit Grund gespeichert.
4. **Letztes Tor** vor jedem Papier-Kauf: `safety.assess(mint, conn, deep=SNIPER_SAFETY_DEEP, force=True)` als
   Kaufwunsch (safety/ nutzt sein eigenes gedeckeltes RPC-Budget), dann `safety.signal_penalty` als Faktor;
   gesperrt → kein Kauf. Ohne Slot-Daten (pump.fun und RPC beide ohne Antwort) wird nie gekauft.
5. **Papier-Kauf**: Jupiter-Quote für die genaue Größe, sonst Bonding-Curve-Formel auf dem Live-Stand, dann
   `SNIPER_ADVERSE_SLIPPAGE` und Gebühren (`SNIPER_PRIORITY_FEE_SOL` + Netz; Curve-Gebühr `SNIPER_CURVE_FEE_PCT`).
6. **Ausstieg** auf dem Live-Curve-Preis (nach Migration DexScreener + Jupiter): erster Gewinnschub
   (`TP1_PCT`, verkauft `TP1_FRAC`), danach Trailing-Stop vom Hoch, Stop-Loss, Zeit-Stop ohne TP1, maximale
   Haltedauer, und sofortiger Verkauf, sobald Bundle-, Insider- oder Dev-Wallets ≥ `INSIDER_SELL_MIN_PCT` ihrer
   Tokens verkaufen. Alle Parameter stehen je Position in `params_json` (für späteres Lernen).
7. **Ergebnis** nach 1, 5, 15, 60 min für **jeden** entschiedenen Launch: Datenstrom solange abonniert, sonst
   DexScreener im Stapel (30 Coins je Call). `rugged` = nach 60 min ≤ −80 % oder Dump der Insider und ≤ −50 %.

## Score

    momentum = 100 × (0,25 Käufer + 0,30 Netto-SOL + 0,15 Kaufdruck + 0,20 Smart Money + 0,10 Streuung)   (nur organische Käufer)
    risk     = 100 × (1 − Π (1 − r_i/100))   über bundle, dev, holders, bots, funders, dev_history, known_bad,
                                             bundle_sells, socials, authorities, safety
    score    = momentum × (1 − risk/100) × safety_factor   (+20 % Modellwahrscheinlichkeit, wenn vorhanden)

Kauf nur bei `score ≥ SNIPER_MIN_SCORE` (50), `risk ≤ SNIPER_MAX_RISK` (45) und ohne Bundle-, Insider- oder harten Befund.

| Flag | wann |
|---|---|
| bundle | ≥ `BUNDLE_BLOCK_PCT` (15 %) des Supplies im Erstellungs-Slot gekauft, oder ≥ `BUNDLE_MIN_WALLETS` (3) Wallets mit ≥ `BUNDLE_WARN_PCT` (5 %) |
| insider | Dev hat ≥ 50 % verkauft; erste Käufer mit gemeinsamer Geldquelle (≥ 5 % Supply oder vom Dev finanziert); ≥ 2 frühere Rugs des Devs; ≥ 2 bekannte Bundler/Rugger unter den ersten Käufern; Bundle-Wallets verkaufen schon |
| hard | Mint- oder Freeze-Authority aktiv; Rug-Schutz sperrt den Coin |

## Kosten und Last

- **Erkennung und Trades: 0 Calls** (ein PumpPortal-Websocket, keine Keys).
- **Trades**: RPC-Websocket-Abos (keine HTTP-Calls, keine Credits), höchstens `SNIPER_WS_MAX_SUBS` gleichzeitig.
- **Stufe 2 je Launch**: 1 Metadaten-Call, dazu 1–5 Calls an `SNIPER_RPC_URL` (Standard: **öffentlicher RPC**, nicht Helius), gedeckelt auf
  `SNIPER_MAX_RPC_PER_LAUNCH` (5) und `SNIPER_RPC_PER_MIN` (10) für den ganzen Dienst. Helius-Credits verbraucht
  der Sniper selbst nur, wenn der Nutzer `SNIPER_RPC_URL` auf Helius stellt.
- **Safety-Tor** nur vor Papier-Käufen (höchstens `SNIPER_MAX_ENTRIES_PER_HOUR` = 12/h), mit dem Budget von safety/.
- DexScreener: etwa 2–4 Batch-Calls pro Minute; GeckoTerminal: 1 Call alle 5 min; Jupiter: nur bei Käufen und
  Verkäufen nach Migration.
- Speicher: ein paar hundert Launches gleichzeitig im Trade-Abo (`SNIPER_MAX_SUBSCRIBED` 400), CPU gering.
- Datenbank: eine kleine Zeile pro Launch (Stufe-1-Skips werden nach `SNIPER_KEEP_DAYS` = 14 Tagen gelöscht),
  ausführliche Daten (Features, Wallets, Curve) nur für tief geprüfte Launches.

## Tabellen

| Tabelle | Inhalt |
|---|---|
| `sniper_launches` | eine Zeile je Launch: Quelle, Dev, Zeiten, Stufe, Entscheidung, `skip_reason`, Score/Momentum/Risiko, Flags, Kennzahlen (Bundle-Anteil und -Wallets, Curve-Schätzung `unseen_pct`, Dev-Anteil/-Verkauf, Top-10, Sniper-Bots, Geldquellen-Gruppen, Dev-Historie, Smart Money, Curve-Fortschritt, Socials, Safety, Modell), `reasons_json`, `positives_json`, `features_json` (Stufe 2), Calls, Migration, `ret_1m/5m/15m/60m`, `max_ret_60m`, `min_ret_60m`, `rugged`, `done` |
| `sniper_launch_wallets` | mint, wallet, role (`bundle`/`insider`/`dev`/`early`), slot, ts, tokens, sol, funder, sold_pct |
| `sniper_curve` | Curve-Verlauf der ersten 15 min tief geprüfter Launches (ts, mcap_sol, v_sol, n_trades) |
| `sniper_positions` | Papier-Positionen: Einstieg, Fill-Quelle, TP1, Peak, realisiert, Ausstiegsgrund, PnL, `params_json`, `events_json` |
| `sniper_state` | `paper_balance_sol`; `service_status` (Heartbeat alle 10 s, JSON, siehe unten) |

`service_status` (lesen mit `json.loads(store.get_state(conn, "service_status"))`): `ts` (Unix-Sekunden; älter als
30 s = Dienst steht), `connected` (PumpPortal-Websocket offen), `ws_msgs` (Nachrichten seit Start), `subscribed`
(Coins im Trade-Abo), `in_memory`, `queue` (Kandidaten in der Warteschlange für Stufe 2), `open_positions`,
`budget: {rpc|api|deep: {used_last_min, per_min, total}}`, `counters: {creates, trades, migrations, stage1, stage2,
entries}` (seit Start), `pump_api_ok`. Beim Beenden wird einmal `connected: false` geschrieben.

## Einstellungen (`.env`, alle optional)

| Variable | Standard | Bedeutung |
|---|---|---|
| `SNIPER_RPC_URL` | öffentlicher RPC | RPC für Launch-Slot und Geldquellen |
| `SNIPER_TRADE_SOURCE` | auto | `auto` (PumpPortal mit Key, sonst RPC-Logs), `pumpportal`, `rpc` |
| `SNIPER_PUMPPORTAL_API_KEY` | – | PumpPortal-Key mit ≥ 0,02 SOL Guthaben (nur der Nutzer legt ihn an) |
| `SNIPER_WS_URL` / `SNIPER_WS_MAX_SUBS` | wss://api.mainnet-beta.solana.com / 150 | RPC-Log-Datenstrom |
| `SNIPER_RPC_PER_MIN` / `SNIPER_MAX_RPC_PER_LAUNCH` | 10 / 5 | RPC-Deckel |
| `SNIPER_PUMP_API_PER_MIN` | 40 | pump.fun-API-Deckel |
| `SNIPER_DEEP_PER_MIN` | 4 | tief geprüfte Launches je Minute |
| `SNIPER_DECIDE_SEC` | 20 | erste Entscheidung nach der Erstellung |
| `SNIPER_MIN_BUYERS` / `SNIPER_MIN_INFLOW_SOL` / `SNIPER_PRESCORE_MIN` | 6 / 1,5 / 30 | Stufe 1 |
| `SNIPER_BUNDLE_WARN_PCT` / `_BLOCK_PCT` / `_MIN_WALLETS` / `SNIPER_BUNDLE_SLOTS` | 5 / 15 / 3 / 1 | Bundle |
| `SNIPER_DEV_WARN_PCT` / `_BLOCK_PCT`, `SNIPER_TOP10_WARN_PCT` / `_BLOCK_PCT` | 5 / 20, 30 / 60 | Dev, Konzentration |
| `SNIPER_DEV_MAX_LAUNCHES_24H` | 3 | Serien-Launcher |
| `SNIPER_MIN_SCORE` / `SNIPER_MAX_RISK` | 50 / 45 | Kaufschwelle |
| `SNIPER_SAFETY_GATE` / `SNIPER_SAFETY_DEEP` | an / an | Rug-Schutz vor jedem Papier-Kauf |
| `SNIPER_REQUIRE_LAUNCH_DATA` | an | ohne Slot-Daten kein Kauf |
| `SNIPER_SIZE_SOL` / `SNIPER_PAPER_START_SOL` / `SNIPER_MAX_OPEN` / `SNIPER_MAX_ENTRIES_PER_HOUR` | 0,1 / 5 / 5 / 12 | Papierkonto |
| `SNIPER_TP1_PCT` / `_TP1_FRAC` / `_TRAIL_PCT` / `_STOP_PCT` | 60 / 0,5 / 25 / 30 | Ausstieg |
| `SNIPER_TIME_STOP_MIN` / `SNIPER_MAX_HOLD_MIN` | 12 / 60 | Zeit-Stops |
| `SNIPER_EXIT_ON_INSIDER_SELL` / `SNIPER_INSIDER_SELL_MIN_PCT` | an / 25 | Verkauf, wenn Insider verkaufen |
| `SNIPER_USE_GECKOTERMINAL` / `SNIPER_TRADE_EXTERNAL` | an / aus | Pools außerhalb pump.fun nur beobachten |

Vollständige Liste: `sniper/config.py`. Tests: `sniper/tests/` (Fixtures, kein Netz).
