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
   bonk-Launches können über einen eigenen Programm-Datenstrom auf Raydium LaunchLab kommen (`SNIPER_BONK_WS_URL`, standardmäßig aus): Kurs, Seite, Menge und die Gebühr der Plattform (bis etwa 6 % je Seite) stehen im Event, der Pool ist aus der Mint ableitbar. Der Trader fehlt im Event; für die Trades vor der Entscheidung holt der Sniper ihn per `getTransaction` nach (höchstens `SNIPER_BONK_WALLETS_PER_LAUNCH` je Launch).
   Ebenso `no_stream`: Launches ohne einen einzigen Trade, solange der Trade-Datenstrom blind ist (Launches kommen,
   aber seit 5 min keine Trades). Dann steht im Log ein ALARM und in `service_status` `blind: true`.
   **Launches kommen aus zwei Quellen zugleich** (`SNIPER_LAUNCH_SOURCES=both`): PumpPortal und ein
   RPC-Launch-Datenstrom (`logsSubscribe` auf pump.funs Mint-Authority `TSLvdd1p…`, die nur in Erstellungen vorkommt;
   `CreateEvent` und der erste Dev-Kauf aus den Logs dekodiert). Wer einen Coin zuerst liefert, gewinnt, der andere
   zählt nur mit; fällt eine Quelle aus (PumpPortal lieferte am 16.10. ab 12:37 UTC HTTP 502), läuft die andere ohne
   Lücke weiter. Beide verbinden sich mit Back-off neu und auch dann, wenn `SNIPER_STALE_SEC` (180 s) lang nichts kommt.
   Last des RPC-Launch-Datenstroms: ein Abo, etwa 10–30 Nachrichten pro Minute, keine HTTP-Calls, keine Credits.
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
   Ohne verlässlichen Preis (tote Curve nach Neustart, Pair nach Migration verschwunden oder seit 10 min
   eingefroren) feuern keine Kursregeln; Zeit-Regeln gelten weiter, und ohne Jupiter-Route wird zu 0 gebucht
   (`no_market`), nie zu einem geschätzten Altpreis. Guthaben und Positionszeile werden in einem Commit geschrieben.
7. **Ergebnis** nach 1, 5, 15, 60 min für **jeden** entschiedenen Launch: Datenstrom solange abonniert, sonst
   DexScreener im Stapel (30 Coins je Call). `rugged` siehe unten (Forschung).

### Kurven in Fremdwährung (seit 18.10.)

Rund 40 % der pump.fun-Launches werden nicht mehr in SOL, sondern in PUMP oder einem anderen pump.fun-Coin gehandelt. Ihre TradeEvents tragen 0 bei den SOL-Feldern; Menge und Reserven der Quote stehen am Ende des Events (IDL `pump-fun/pump-public-docs`, `idl/pump.json`: nach `ix_name` mayhem_mode, Cashback und Buyback, `shareholders`, dann `quote_mint`, `quote_amount`, `virtual_quote_reserves`, `real_quote_reserves`). `rpcws.decode_trade` rechnet sie mit dem SOL-Kurs der Quote in SOL um: ein Coin, den der Sniper selbst live verfolgt, bepreist sich aus seiner Kurve, sonst holt `quote_round` den Kurs alle 5 s bei DexScreener (`feed.token_prices_sol`). Die Nachkommastellen der Quote: 6 bei pump.fun-Coins, sonst einmal per `getTokenSupply` gelesen. Trades, deren Quote noch keinen Kurs oder keine bekannten Nachkommastellen hat, werden zurückgehalten und nachgereicht, sobald er da ist (höchstens 2 min alt). Die Gebühr kommt aus dem Event selbst (Protokoll + Creator, 1,25 % bei SOL-Kurven, 2,95–3,95 % bei den gequoteten) plus 1 % für den Tausch SOL ↔ Quote je Seite im Papierhandel. Der Dev-Kauf einer gequoteten Erstellung zählt auch ohne Kurs (`dev_buy_pct`). Spalte `quote_mint`, Merkmal `sniper.quote` (SOL, PUMP, coin) in der Gelegenheiten-Tabelle; der 5-min-Bericht zählt sie unter „Kurven in Fremdwährung“.

### Launch-Block sofort migrierter Launches (seit 18.10.)

Kauft der Creator die Curve schon beim Erstellen fast ganz (79,31 %), migriert der Coin sofort, und die Käufe danach laufen über den pump-AMM, nicht über die Curve. Der Datenstrom sieht sie nicht. `launch_block_round` (alle 15 s) liest für entschiedene Launches, die binnen 60 s nach dem Erstellen migriert sind, die ersten Signaturen des Mints (`getSignaturesForAddress`, öffentlicher RPC) und je Transaktion der ersten 3 Slots, wer den Token bekam und wie viel SOL er dafür zahlte (`sniper/launchblock.py`). Das Ergebnis landet in `sniper_launch_wallets` mit der Rolle `launch_block` und in `sniper_launches.block_buyers` und `block_buy_sol`, für das Masche-Veto der Rug-Prüfung. Der SOL-Einsatz je Käufer ist der Verlust an nativem plus gewrapptem SOL (WSOL-Konten desselben Besitzers): wer 5000 SOL wrappt und 4999 im WSOL-Konto liegen lässt, hat 1 SOL eingesetzt. `getTransaction` fragt mit `maxSupportedTransactionVersion: 1` (Launch-Block, Tiefencheck, bonk-Signer), weil es auf Mainnet v1-Transaktionen gibt und der RPC sonst mit -32015 ablehnt. Die Stundenzeile „Abdeckung“ läuft zur Uhrzeit (hh:07); kurz nach einem Neustart nennt sie das kürzere Fenster („22 min seit Start“).

### Frühmeldung in die Gelegenheiten-Tabelle (seit 18.10.)

Jeder pump.fun-Launch geht beim ersten Blick (20 s, `SNIPER_DECIDE_SEC`) in die gemeinsame Tabelle `opportunities` (`ref` = Mint), auch die, die in die Warteschlange zum Tiefencheck gehen (`source_action` = `queued`, Merkmale aus Stufe 1). Nach dem Tiefencheck, oder wenn ein wartender Launch aus Budgetgründen übersprungen wird, liefert `opportunity.report_decision(..., followup=True)` per `brain.opportunities.supplement` in dieselbe Zeile nach: Funder-Gruppen, Bundle und Insider nach Slot, Safety, `risk`, `rug_p` und das Endurteil (`sniper.final_action`, `sniper.final_reason`, `sniper.deep`). Aktivitätsmerkmale bleiben beim Stand von 20 s. Launches ohne Trades werden weiter beim zweiten Blick (75 s) gemeldet. Das Stundenlog zählt beides unter „Gelegenheiten seit Start“.

## Score

    momentum = 100 × (0,25 Käufer + 0,30 Netto-SOL + 0,15 Kaufdruck + 0,20 Smart Money + 0,10 Streuung)   (nur organische Käufer)
    risk     = 100 × (1 − Π (1 − r_i/100))   über bundle, dev, holders, bots, funders, dev_history, known_bad,
                                             bundle_sells, socials, authorities, safety
    score    = momentum × (1 − risk/100) × safety_factor   (+20 % Modellwahrscheinlichkeit, wenn vorhanden)

Kauf nur bei `score ≥ SNIPER_MIN_SCORE` (40), `risk ≤ SNIPER_MAX_RISK` (45) und ohne Bundle-, Insider- oder harten Befund.

| Flag | wann |
|---|---|
| bundle | ≥ `BUNDLE_BLOCK_PCT` (15 %) des Supplies im Erstellungs-Slot gekauft, oder ≥ `BUNDLE_MIN_WALLETS` (3) Wallets mit ≥ `BUNDLE_WARN_PCT` (5 %) |
| insider | Dev hat ≥ 50 % verkauft; erste Käufer mit gemeinsamer Geldquelle (≥ 5 % Supply oder vom Dev finanziert); ≥ 2 frühere Rugs des Devs; ≥ 2 bekannte Bundler/Rugger unter den ersten Käufern; Bundle-Wallets verkaufen schon |
| hard | Mint- oder Freeze-Authority aktiv; Rug-Schutz sperrt den Coin |

## Kosten und Last

- **Erkennung und Trades: 0 Calls** (ein PumpPortal-Websocket, keine Keys).
- **Trades**: RPC-Websocket-Abos (keine HTTP-Calls, keine Credits), höchstens `SNIPER_WS_MAX_SUBS` gleichzeitig,
  höchstens `SNIPER_WS_SUBS_PER_SEC` neue pro Sekunde. Der öffentliche RPC schließt bei zu vielen Abo-Versuchen mit
  1013 „Too many subscriptions“: dann wartet der Stream mindestens 15 s, und im Modus `auto` wechselt er nach 3
  solchen Abbrüchen in 10 min auf **ein** Abo auf das pump.fun-Programm und filtert die Trades lokal.
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
| `sniper_curve_live` | Letzter Kurvenstand je Mint (ts, pool, price_sol, v_sol, mcap_sol, n_trades, complete), alle 3 s vom laufenden Sniper geschrieben; Lesen aus anderen Prozessen mit `sniper.store.curve_now(conn, mint, max_age_s=30)` |
| `sniper_positions` | Papier-Positionen: Einstieg, Fill-Quelle, TP1, Peak, realisiert, Ausstiegsgrund, PnL, `params_json`, `events_json` |
| `sniper_state` | `paper_balance_sol`; `service_status` (Heartbeat alle 10 s, JSON, siehe unten) |

`service_status` (lesen mit `json.loads(store.get_state(conn, "service_status"))`): `ts` (Unix-Sekunden; älter als
30 s = Dienst steht), `connected` (irgendeine Quelle offen: PumpPortal, RPC-Launches oder RPC-Trades),
`pumpportal_connected`, `learned_rule` (Bedingungen der angenommenen Forschungsregel oder `null`), `research_ts`, `trade_sources: {pumpportal|rpc: {active, connected, trades_per_min, …}}` (rpc zusätzlich
`mode`, `subs`, `wanted`, `attempts`, `rejects`, `reconnects`), `trades_per_min` (5 min), `blind` (Launches ohne
Trades seit 5 min), `launch_sources`, `active_launch_source`, `launches_per_min`, `ws_msgs` (Nachrichten seit Start), `subscribed`
(Coins im Trade-Abo), `in_memory`, `queue` (Kandidaten in der Warteschlange für Stufe 2), `open_positions`,
`budget: {rpc|api|deep: {used_last_min, per_min, total}}`, `counters: {creates, trades, migrations, stage1, stage2,
entries}` (seit Start), `pump_api_ok` (`null`, wenn die pump.fun-API aus ist). Beim Beenden wird einmal `connected: false` geschrieben.

## Forschung an echten Ausgängen (`python -m sniper research`)

Stündlich im Dienst (Fenster `SNIPER_RESEARCH_HOURS`, Standard 72 h) und von Hand über die Kommandozeile:

- **Preisverlauf** je Launch in `sniper_paths` (5-s-Stufen mit Tief/Hoch für 15 min, dann 30 s bis 65 min). Damit
  werden Ausstiegsregeln exakt nachgespielt, inklusive der Frage, ob erst der Stop oder erst der Gewinn kam.
  Ältere Launches ohne Verlauf laufen über die Stände nach 1/5/15/60 min.
- **Merkmale**: Rangkorrelation mit dem Netto-Ergebnis, AUC für Verdopplung und Rug, Ergebnis je Fünftel.
- **Ausstiege**: Raster aus Take-Profit, Anteil, Trailing, Stop und Zeit-Stop, alles nach Kosten (Curve-Gebühr
  und Slippage auf beiden Seiten, Priority-Fee je Transaktion, wie im Papierbuch).
- **Risiko-Schwellen**: Rug- und Verdopplungsrate je Stufe von Bundle-Anteil, Curve-Schätzung, Dev-Anteil, Top-10,
  schnellen Käufern und Dev-Launches der letzten 24 h, auch nur unter Launches mit Pump.
- **Regelsuche** mit zeitlicher Trennung: Regeln (eine oder zwei Bedingungen plus Ausstieg) werden an den älteren
  70 % gewählt und einmal an den neueren 30 % gemessen. **Angenommen** wird eine Regel nur, wenn der Testteil nach
  Kosten im Plus ist (Bootstrap-Untergrenze über 0, mindestens 30 Trades). Erst dann kauft der Sniper damit im
  Papiermodus, mit ihren eigenen Ausstiegen; Bundles, Insider, Risiko, Rug-Modell und Rug-Schutz blocken weiter.
  Hält eine Regel in einem späteren Lauf nicht mehr, wird sie zurückgezogen.
- **Rug-Modell**: `rug_p` je Launch aus dem gelernten Modell des Rug-Schutzes (`safety.backtest`); ab
  `SNIPER_RUG_P_BLOCK` (0,59) wird nicht gekauft. In der Regelsuche bleibt es draußen, weil es an denselben
  Launches gelernt ist.

`max_ret_60m`/`min_ret_60m` zählen erst ab dem Entscheidungspreis (`outcome_fix_ts` in `sniper_state` markiert,
ab wann); davor enthielten sie den Pump vor dem Entscheid. Die Forschung nutzt bei älteren Zeilen nur die Stände.

`rugged` = 1, wenn der Kurs nach 60 min ≤ −80 % steht, innerhalb der Stunde (sauber gemessen) auf ≤ −90 % fiel,
oder ≤ −50 % nach Dev-/Insider-Abverkauf. Pump-Launches mit kaputtem Basispreis (< 27,9 SOL) bekommen kein Label
(NULL), damit kein Rug-Modell darauf lernt. Alte Zeilen korrigiert `store.relabel_rugs` einmal je `RUG_LABEL_VERSION`.


**Tempo** (seit 18.10., Plan „Hundertmal“ Hebel 5): Die Forschung spielt zusätzlich alle Launches mit Preisverlauf nach, als wären sie 2, 5, 10 oder 20 s nach der Erstellung gekauft worden. Der Kurs ist dabei das Hoch des 5-s-Fensters, also pessimistisch. Ausstieg und Kosten sind dieselben, und zum Vergleich steht derselbe Launch beim echten Entscheid. Die Logzeile „Tempo …“ zeigt so, was Geschwindigkeit allein bringt, bevor der Entscheid nach vorn wandert.
## Einstellungen (`.env`, alle optional)

| Variable | Standard | Bedeutung |
|---|---|---|
| `SNIPER_RPC_URL` | öffentlicher RPC | RPC für Launch-Slot und Geldquellen |
| `SNIPER_TRADE_SOURCE` | auto | `auto` (PumpPortal mit Key, sonst RPC-Logs), `pumpportal`, `rpc` |
| `SNIPER_PUMPPORTAL_API_KEY` | – | PumpPortal-Key mit ≥ 0,02 SOL Guthaben (nur der Nutzer legt ihn an) |
| `SNIPER_LAUNCH_SOURCES` / `SNIPER_STALE_SEC` | both / 180 | Launch-Quellen (`both`, `pumpportal`, `rpc`), Neuverbindung bei Stille |
| `SNIPER_WS_URL` / `SNIPER_WS_MAX_SUBS` | wss://api.mainnet-beta.solana.com / 150 | RPC-Log-Datenstrom |
| `SNIPER_WS_URL2` | wss://solana-rpc.publicnode.com | zweiter Programm-Datenstrom auf einem anderen RPC (füllt die Lücken der 1002-Trennungen; doppelte Trades fallen per Signatur weg); leer = aus |
| `SNIPER_WS_URL2_FALLBACKS` | wss://solana.drpc.org | weitere RPCs für Strom 2, kommagetrennt: nach 3 Verbindungen, die das Abo bestätigen und dann nichts liefern, wechselt Strom 2 auf den nächsten (im Kreis) |
| `SNIPER_WS_HELIUS` | 1 | Strom 2 zuerst über den Helius-Standard-Websocket, wenn `HELIUS_API_KEY` gesetzt ist (SNIPER_WS_URL2 und die Ersatz-RPCs folgen). Die Daten kosten 2 Credits je 0,1 MB, werden im gemeinsamen Credit-Buch gebucht und im 5-min-Bericht („Datenvolumen…“) hochgerechnet; läuft der Helius-Monat dem Kalender voraus (`core.http.plan_pace`, ratio > 1) oder erreicht er seine Reserve, wechselt Strom 2 auf die öffentlichen RPCs (zurück unter 0,95); Wal-Erkennung und Rug-Prüfung behalten Helius |
| `SNIPER_WS_HELIUS_CREDITS_DAY` | 0 | zusätzliche Tagesgrenze in Credits für Strom 2 auf Helius (0 = nur die Monatsreserve) |
| `SNIPER_BONK_WS_URL` | (leer) | Programm-Datenstrom von Raydium LaunchLab (bonk) auf einem dritten RPC; leer = aus, bonk-Launches dann `no_stream`. Nicht derselbe RPC wie `SNIPER_WS_URL`/`SNIPER_WS_URL2`: ein zweites Programm-Abo dort legte Strom 2 lahm |
| `SNIPER_BONK_RPC_URL` | https://solana-rpc.publicnode.com | RPC für die Trader der ersten bonk-Trades (`getTransaction`) |
| `SNIPER_BONK_WALLETS_PER_LAUNCH` | 30 | so viele Trader je bonk-Launch vor der Entscheidung |
| `SNIPER_BONK_WALLETS_PER_MIN` | 60 | Tempo dieser Abfragen |
| `SNIPER_WS_SUBS_PER_SEC` | 4 | neue logsSubscribe-Anfragen pro Sekunde |
| `SNIPER_USE_LEARNED_RULE` / `SNIPER_RESEARCH_HOURS` | 1 / 72 | angenommene Forschungsregel im Papiermodus nutzen / Fenster |
| `SNIPER_RUG_P_BLOCK` | 0.59 | kein Kauf ab dieser Rug-Wahrscheinlichkeit des Rug-Modells |
| `SNIPER_TRADE_SUB_MODE` | auto | `mints` (ein Abo je Coin), `program` (ein Abo, lokal gefiltert), `auto` (mints, bei Rate-Limit program) |
| `SNIPER_RPC_PER_MIN` / `SNIPER_MAX_RPC_PER_LAUNCH` | 10 / 5 | RPC-Deckel |
| `SNIPER_PUMP_API_PER_MIN` | 40 | pump.fun-API-Deckel |
| `SNIPER_DEEP_PER_MIN` | 4 | tief geprüfte Launches je Minute |
| `SNIPER_DECIDE_SEC` | 20 | erste Entscheidung nach der Erstellung |
| `SNIPER_RECHECK_SEC` | 75 | ohne einen einzigen Trade beim ersten Blick: zweiter Blick bis hierhin, danach `no_trades` (keine Daten, kein Urteil über Aktivität) |
| `SNIPER_MIN_BUYERS` / `SNIPER_MIN_INFLOW_SOL` / `SNIPER_PRESCORE_MIN` | 5 / 1,0 / 25 | Stufe 1 |
| `SNIPER_BUNDLE_WARN_PCT` / `_BLOCK_PCT` / `_MIN_WALLETS` / `SNIPER_BUNDLE_SLOTS` | 5 / 15 / 3 / 1 | Bundle |
| `SNIPER_DEV_WARN_PCT` / `_BLOCK_PCT`, `SNIPER_TOP10_WARN_PCT` / `_BLOCK_PCT` | 5 / 20, 30 / 60 | Dev, Konzentration |
| `SNIPER_DEV_MAX_LAUNCHES_24H` | 3 | Serien-Launcher |
| `SNIPER_MIN_SCORE` / `SNIPER_MAX_RISK` | 40 / 45 | Kaufschwelle |
| `SNIPER_SAFETY_GATE` / `SNIPER_SAFETY_DEEP` | an / an | Rug-Schutz vor jedem Papier-Kauf |
| `SNIPER_REQUIRE_LAUNCH_DATA` | an | ohne Slot-Daten kein Kauf |
| `SNIPER_SIZE_SOL` / `SNIPER_PAPER_START_SOL` / `SNIPER_MAX_OPEN` / `SNIPER_MAX_ENTRIES_PER_HOUR` | 0,1 / 5 / 5 / 12 | Papierkonto |
| `SNIPER_TP1_PCT` / `_TP1_FRAC` / `_TRAIL_PCT` / `_STOP_PCT` | 60 / 0,5 / 25 / 30 | Ausstieg |
| `SNIPER_TIME_STOP_MIN` / `SNIPER_MAX_HOLD_MIN` | 12 / 60 | Zeit-Stops |
| `SNIPER_EXIT_ON_INSIDER_SELL` / `SNIPER_INSIDER_SELL_MIN_PCT` | an / 25 | Verkauf, wenn Insider verkaufen |
| `SNIPER_USE_GECKOTERMINAL` / `SNIPER_TRADE_EXTERNAL` | an / aus | Pools außerhalb pump.fun nur beobachten |

Vollständige Liste: `sniper/config.py`. Tests: `sniper/tests/` (Fixtures, kein Netz).
