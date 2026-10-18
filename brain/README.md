# brain: ein lernender Entscheider statt Strategie-Versionen

Ein Dienst (`python -m brain run`, im Launcher als `brain`) mit eigenem **Papierkonto** (5 SOL) neben den
bisherigen Strategien, die als Vergleich weiterlaufen. Nie Echtgeld: es gibt keinen Live-Pfad.

## Was er tut

Jede Gelegenheit wird einzeln entschieden, mit allem, was der Bot in dem Moment weiß:

| Quelle | Gelegenheit | Kontext (Auszug) |
|---|---|---|
| `ct_swaps` (Copy-Tracker) | Wal-Kauf ≥ `BRAIN_MIN_WHALE_SOL`, höchstens `BRAIN_MAX_WHALE_DELAY_S` (300 s) alt, spätere zählt die Stundenzeile | Wal-Stufe und Cluster, Wal-Bilanz nach Kosten (`learn.wallet_edge`), Launch-Bot, Kaufgröße, weitere Smart Wallets im Coin (30 min), Signal auf dem Coin (6 h), Zentrale-Merkmale des Wals |
| `signals` | Smart-Money-Signal | Score, Smart Wallets, Ultra dabei, Signal-EV, Call-Nummer |
| `sniper_launches` + `sniper_positions` | Sniper-Einstieg | Sniper-Score und -Risiko, Bundle, Insider, Dev-Launches, Top-10, Socials, Modell-Chance |
| alle | | Liquidität, Market Cap, Coin-Alter, Kurs 5 min, Kaufdruck, Preis seit Auslöser, Verzögerung, Rug-Urteil (`safety.get`), Marktphase (`market.context.phase`), walk-forward-bestätigte Zentrale-Merkmale (`intel.trusted_at`), Tageszeit, eigene Form |

Für sechs Ausstiegspläne (`plans.py`: Schnell rein und raus, Erster Pump, Pump mitnehmen, Mittlerer Lauf, Laufen
lassen, Mondschuss) schätzt das Modell den **Netto-Gewinn nach Kosten** und wie sicher es sich ist. Der Ausstieg ist
Teil der Entscheidung: Ziel, Stop, Trailing, Haltezeit, „Ansturm vorbei“, und bei Wal-Kopien „Wal hat verkauft“.
Ziel, Stop und Trailing messen den Marktpreis gegen den Marktpreis beim Kauf (nicht gegen den Fill mit Slippage),
das Trailing scharf erst nach einer Minute.

- **Überzeugt** (Erwartung > 0 und zu ≥ `BRAIN_MIN_CONF` = 75 % sicher): Kauf, Größe 0,1 SOL bei 75 % bis 0,5 SOL
  bei 95 % Sicherheit.
- **Erkunden** (nicht überzeugt, aber eine Ziehung aus dem Posterior sagt „könnte sich lohnen“, Thompson Sampling,
  oder der beste Plan ist zu ≥ `BRAIN_EXPLORE_MIN_P` = 25 % im Plus):
  0,1 SOL, ohne Tageskontingent (Papier hat kein Budget; `BRAIN_EXPLORE_PER_DAY` > 0 setzt eins).
  Nicht erkundet wird in Segmenten, die laut Daten schon beim Einstieg verlieren (`BRAIN_NO_EXPLORE`, Standard
  `delay:30-90s,delay:>90s,liq:curve`; Auswertung 17.10.: Wal-Kopie ab 30 s erkannt −11,2 % gegen −4 % darunter,
  Curve-Coins −17 % gegen pumpswap −6,5 %). Dort kauft das Hirn nur überzeugt; gelernt wird jede Gelegenheit.
  `honest()` zeigt diese Segmente getrennt (`by_delay`, `by_liq`), das Tagebuch ebenso.
- Sonst **auslassen**. Auch ausgelassene Gelegenheiten werden später nachgespielt und gelernt.

Harte Grenzen, die das Modell nie übergeht: Marktpreis vorhanden (ein Coin ohne DexScreener-Paar bekommt 30 s),
Liquidität ≥ `BRAIN_MIN_LIQ_USD` bzw. auf der pump.fun-Bonding-Curve (dort meldet DexScreener $0 Liquidität)
Market Cap ≥ `BRAIN_MIN_CURVE_MCAP_USD` (5000), Rug-Prüfung
(`safety.assess` quick, ohne Ergebnis kein Kauf), eine Position je Coin, technische Obergrenze `BRAIN_MAX_OPEN` (100
offene Positionen, wegen der Preisabfragen). Papier hat kein Budget: kein Tagesverlust-Limit, keine Guthaben-Prüfung
(das Papierkonto darf ins Minus); `BRAIN_MAX_DAILY_LOSS_SOL` > 0 schaltet ein Limit wieder ein. Dazu bei Wal-Käufen: kein Kopieren, wenn der Wal schon wieder verkauft hat
oder meist binnen 2 min wieder raus ist, wenn der Coin in den 5 min davor schon über 5 % gestiegen ist („schon gelaufen“), (≥ 60 % seiner letzten Käufe, mindestens 5), und kein zweiter Kauf in einem
Coin, den das Hirn in den letzten 6 h schon gehandelt hat. Die Flip-Quote des Wals ist zusätzlich ein Merkmal
(`flip`), aus dem das Modell lernt.

Jede Entscheidung schreibt 2 bis 4 Zeilen Begründung (`brain_opps.note`), zum Beispiel:

```
Wal 7xKp3a… (ultra, Cluster) kaufte 3.2 SOL BONK, erkannt nach 4 s, Preis seither +2%, 2 weitere Smart Wallets im Coin.
Dafür: weitere Smart Wallets im Coin (30 min) 2+ (+2.1%), Wal-Stufe ultra (+1.4%). Dagegen: Wash-Anteil 10-30% (-3.0%).
Erwartung mit „Pump mitnehmen“ (Ziel +40%, Stop −20%, Trailing ab +15%, max. 45 min, raus wenn der Ansturm abflaut): Ø +1.2% netto ± 1.4%, sicher im Plus zu 80%.
Kauf 0.18 SOL.
```

## Wie er lernt (`model.py`, `learner.py`)

Bayessche lineare Regression je Plan über Kontext-Token, nach **jedem einzelnen Ergebnis** aktualisiert, keine
Versionen. Alte Ergebnisse verblassen mit Halbwertszeit `BRAIN_HALF_LIFE_H` (120 h), damit er dem Markt folgt.

1. **Vorwissen:** alle Wal-Käufe und Signale, die copytrade vor dem Start schon auf Minutenkerzen nachgespielt hat
   (`ct_evals`), auf die sechs Pläne abgebildet (es sind Punkte von copytrades Gitter), ältere schwächer gewichtet.
2. **Jede Gelegenheit**, auch die ausgelassenen, in zwei Schritten: die kurzen Pläne (bis 45 min) etwa eine Stunde
   nach der Entscheidung, die langen, wenn der längste Plan vorbei ist (~4,2 h); jeder Schritt wird sofort gelernt. Jeder
   Plan wird nachgespielt (`copytrade.sim`, echte Wal-Verkäufe als Zwangsausstieg; Tracker-Ausfälle machen eine Wal-Gelegenheit
   unbewertbar). Kerzen aus den vorhandenen Caches, sonst höchstens `BRAIN_EVALS_PER_ROUND` Abrufe je Runde.
3. **Eigener Trade, sofort beim Schließen:** der echte Papier-Fill (Jupiter-Quote, Slippage, Gebühren) für den
   genommenen Plan, doppelt gewichtet; das spätere Nachspielen lässt diesen Plan aus.

Kosten stecken in jeder Bewertung: 3 % im Simulator plus `BRAIN_EXTRA_COST_COPY` (zusammen 5,5 % Rundreise für
Wale und Signale, mehr, wenn copytrades eigene Trades mehr gemessen haben) bzw. `BRAIN_EXTRA_COST_LAUNCH` (9 %).

**Ehrlich out of sample:** Vor dem Lernen wird die Vorhersage von damals mit dem Ergebnis verglichen (`honest()`):
genommen gegen ausgelassen gegen „alles stumpf kopiert“, plus die echten Papier-Trades. Für das Vorwissen dasselbe
in `prior_check`.

## Lerntagebuch

Täglich nach `BRAIN_DIARY_HOUR_UTC` (19 Uhr UTC) ein Eintrag in `brain_journal` und per Telegram: was er gesehen
und gekauft hat, die ehrliche Nachprüfung des Tages, was sich in seinen Annahmen bewegt hat, was für und gegen einen
Trade spricht, welcher Ausstieg bevorzugt war, wo er noch unsicher ist, und der Stand auf dem Weg zu
100 nachgeprüften Trades im Plus. Telegram: `BRAIN_TELEGRAM=trades` (Käufe, Verkäufe, Tagebuch), `diary`, `off`.

## Schnittstelle für web/ und cli/

```python
import brain
brain.status(conn)                 # Konto, offene/geschlossene Trades, honest(), Vorwissen-Prüfung
brain.decisions(conn, limit=50, kind=None, entered_only=False)   # Entscheidungen mit note, features, Ergebnis
brain.positions(conn, status=None) # Papier-Positionen
brain.diary(conn, limit=14)        # Lerntagebuch, neueste zuerst
brain.equity(conn, since=0)        # Equity-Kurve (jede Minute)
brain.honest(conn, since=0, evaluated_since=0)
```

Tabellen (nur brain schreibt sie): `brain_opps`, `brain_positions`, `brain_equity`, `brain_journal`, `brain_state`.
Das Modell liegt in `data/brain/model.npz` (alle 10 min gespeichert).

```bash
python -m brain status | decisions 20 | journal [--write] | beliefs
```

`BRAIN_ENABLED=false` hält den Dienst an. Ein LLM steckt nicht drin; die Entscheidung ist Rechnung in Millisekunden.

## Organischer Ausstieg (`exits.py`)

Statt fester Prozentmarken entscheidet ein zweites Modell einmal pro Minute aus dem Zustand des Coins: Plus/Minus seit
dem Kauf, Abstand zum Hoch, letzte Minute und letzte 5 Minuten, ob das Volumen trägt oder austrocknet, Haltedauer, ob
der kopierte Wal verkauft hat, ob die übrigen Smart Wallets zu- oder abfließen. Gefragt wird: was macht der Kurs in
den nächsten 10 Minuten? Halten, die Hälfte verkaufen (im Plus ≥ `BRAIN_EXIT_HALF_MIN_UP`, P(steigt) < `BRAIN_EXIT_HALF_P`)
oder den Rest verkaufen (P < `BRAIN_EXIT_SELL_P`). Harte Grenzen bleiben: Crash-Stop −30 %, Rug-Prüfung, max. 4 h, und nach einer Verdopplung ein Trailing-Stop von 30 %
unter dem Hoch (Gewinner-Studie 17.10.). Das Modell urteilt frühestens nach `GRACE_MIN` (3) Minuten und erst, wenn
der laufende Prozess den Coin selbst eine Minute beobachtet hat (auch nach einem Neustart): direkt nach dem Kauf ist
der Spread die einzige Bewegung (18.10.: Verkäufe nach 0–2 s mit −6 bis −8 %). Die Replays halten dieselbe Frist ein.

Gelernt wird aus jeder nachgespielten Gelegenheit (jede Minute ein Beispiel, dieselben Minutenkerzen wie die
Plan-Replays) und einmalig aus den schon fertigen (`backfill_exits`). Jede fertige Gelegenheit wird zusätzlich mit
dem organischen Ausstieg nachgespielt, mit dem Modell von vor diesem Coin (`brain_exit_evals`). Live übernimmt er
(`BRAIN_ORGANIC_EXIT=auto`) erst, wenn diese Replays (mind. 30) im Schnitt die gewählten Pläne schlagen; `on`/`off`
erzwingen. Das Tagebuch zeigt organisch gegen Pläne gegen 1 h halten, den Anteil am besten Kursanstieg
(Einfangquote), den Auswahl-Vorsprung und den Tagesfaktor des Kontos (Ziel ×2–4).

## Regime-Erkennung

Der Markt verhält sich nicht immer wie die Lerndaten. Jede Lernrunde vergleicht die Ergebnisse der letzten 6 Stunden
(mind. 30) mit dem, was das Modell beim Entscheiden für sie vorhergesagt hatte. Liegen sie systematisch daneben
(mehr als 3 Standardfehler und mindestens 5 Prozentpunkte je Trade), halbiert das Hirn sofort alles alte Wissen,
im Einstiegs- und im Ausstiegsmodell; die neuen Ergebnisse zählen dann doppelt so viel. Höchstens alle 3 Stunden,
das Tagebuch meldet „Regime gewechselt“.

## Nebenquelle Markt-Coins (`kind="market"`)

Coins, die der Signal-Trichter angesehen, aber nicht gemeldet hat (Schattenbuch `signal_shadow`, auch ohne
Smart-Käufer), werden als eigene Gelegenheitsart entschieden: Handelsplatz, Kurs 5 min / 1 h, Trades 5 min,
Volumen 1 h, Kurs 15 min davor (Rücksetzer), Market Cap, Alter, Liquidität, Grund ohne Signal. Dazu Momentum-Coins
aus der `tokens`-Tabelle, die der Signal-Dienst von DexScreener frisch hält (Kurs 5 min ≥ +5 %, ≥ 30 Trades in 5 min,
Grund „momentum“; einmal pro Minute die in den letzten 3 min aktualisierten Zeilen, ohne Cursor auf dem
Zeitstempel); je Coin höchstens eine Gelegenheit in 6 h. Freigeschaltet wird nach dem, was es aus der Quelle gewollt
oder erkundet hat. Die Gewinner-Studie
(17.10.) fand die großen Läufer nicht über Smart Money, sondern nur über Zustand und Momentum. Gehandelt wird die
Quelle erst, wenn das, was das Hirn aus ihr gewollt hätte, in mindestens 30 nachgespielten Fällen nach Kosten im
Plus lag, gezählt über die letzten 3 Tage: lässt das Muster nach, schaltet sich die Quelle von selbst wieder ab
(`BRAIN_MARKET_TRADE=auto`, `on`/`off` erzwingen). Bis dahin wird nur beobachtet und gelernt, ohne
Rug-Prüfung per RPC und mit höchstens der Hälfte der Kerzen-Downloads je Lernrunde.

## Einsatz nach Kelly und Stundenfaktor

Überzeugte Käufe setzen einen Anteil des aktuellen Kontos: ein Viertel des Kelly-Einsatzes (erwarteter Vorsprung /
Streuung eines Trades, `BRAIN_KELLY_FRAC` 0,25), höchstens `BRAIN_MAX_STAKE` (20 %) des Kontos, mindestens
`BRAIN_MIN_SIZE_SOL`. Der Einsatz wächst also mit dem Konto (Zinseszins); Erkundungen bleiben beim Minimum.
`BRAIN_KELLY_FRAC=0` stellt auf die festen Größen zurück. Das Lernen ist davon unberührt (es lernt Renditen in %).
Die Stundenzeile im Log und das Tagebuch zeigen den Stundenfaktor des Kontos (Challenge des Nutzers: ×2 je Stunde).

## Challenge-Modus (×2 je Stunde, `BRAIN_CHALLENGE=on`)

Auf Wunsch des Nutzers (10/2026, „komme was wolle“): überzeugte Käufe mit halbem Kelly-Einsatz, bis 35 % des
Kontos; Erkundungen mit 2 % des Kontos; die noch unbelegten Nebenquellen werden sofort auf Papier erkundet (bis 12
Probekäufe je Stunde, Rug-Prüfung nur beim Kauf). Haben die letzten 3 vollen Stunden zusammen mehr als 15 % verloren
(`BRAIN_CHALLENGE_STOP` 0,85), pausiert der Modus 6 Stunden mit den normalen Einsätzen; das Tagebuch meldet es.

## Muster und Einsatz (weich, ohne Schwelle)

Jede Gelegenheit jeder Quelle steht mit ihrem nachgespielten Ergebnis nach Kosten in `brain_opps`, genommen oder
nicht (Schatten-Buch). `pockets.py` prüft alle 10 min jede Quelle allein und jede Quelle mit einem Merkmal
(`market|dex=pumpswap`) über 7 Tage und gibt jedem Muster ab 20 Fällen ein Vertrauen: die Wahrscheinlichkeit, dass
sein Schnitt nach Kosten über null liegt, die neuere Hälfte doppelt gewichtet. Es gibt kein Tor: das Modell entscheidet
jede Gelegenheit selbst. Im Challenge-Modus gleitet der Einsatz mit dem Vertrauen des besten passenden Musters vom
normalen Kelly-Anteil (50 %) zum großen (½-Kelly bis 35 %, ab 95 %) und fällt genauso zurück, wenn die jüngsten
Ergebnisse kippen. Kelly selbst rechnet die eigene Unsicherheit des Modells mit ein (Varianz plus sd²): ein unsicherer
Vorteil setzt von allein weniger (Nutzer 18.10.: freier, organischer, keine festen Regeln; nichts schönrechnen).
„Freigegeben“ (≥ 100 Fälle, 95-%-Untergrenze in beiden Hälften positiv, letzte 50 positiv) ist nur noch ein Etikett
fürs Dashboard. Stand in `brain_state.pockets` (Zeilen mit key, label, source, n, mean, lower95, Hälften,
last50_mean, trust, released, released_at, demoted_at, reason), lesend über `pockets.table(conn)`.

## Alle Quellen in einem Modell (`opportunities`, `pool.py`)

Sniper, Copytrading, Signale, Wallet-Modell und Strategie melden jede Gelegenheit über
`brain.opportunities.report()` in die gemeinsame Tabelle `opportunities` (Schema:
plan/gelegenheiten_schema.md). Das Hirn liest sie per Cursor (`cursor_pool`, nur frische Zeilen bis
`BRAIN_MAX_POOL_AGE_S`), bildet aus den Rohwerten generische Stufen (halbe Zehnerpotenzen, Text, ja/nein) und
schickt sie als Art `pool` durch dieselbe Entscheidung: Ziehung aus der vorhergesagten Verteilung je Ausstiegsplan,
gekauft wird, was die Ziehung über die Kosten bringt. Ohne Tor, ohne Quote. Der Einsatz einer Ziehung ist das
Lernkonto (`BRAIN_LEARN_SIZE_SOL`, 0,03 SOL); ist das Modell sicher, rechnet Kelly mit seiner Unsicherheit. Das Lernkonto
hat eine eigene technische Obergrenze offener Positionen (`BRAIN_POOL_MAX_OPEN`, 120; Helius darf laut Nutzer 18.10. so viel wie nötig), damit es die eigenen Quellen
nie verdrängt (je Kauf ~4 RPC-Aufrufe Rug-Prüfung), und bekommt höchstens die Hälfte der Kerzen-Downloads je Runde.

Das Modell (`pool.py`) hält je Merkmal und Plan eine eigene Normalverteilung (Assumed Density Filtering), damit
Tausende Merkmale möglich sind; jedes Merkmal steht einmal quellenübergreifend und einmal mit Quelle im Modell.
Halbwertszeit 72 h, vergessene Muster fallen auf den Prior zurück. Gespeichert in `data/brain/pool_model.json`.

Das Hirn schreibt in jede Zeile zurück: `model`, `pred_mean`, `pred_sd`, `pred_q10`, `draw`, `decision`
(learn/perform/skip), Einsatz und Position, später `outcome_net` nach Kosten mit dem gewählten Plan (Rug −100 %),
`outcomes` je Plan und `rug`. Rug-Prüfung vor jedem Kauf bleibt Pflicht. `BRAIN_POOL_TRADE=off` schaltet nur das
Lesen ab.

## Zweite Bremse: Trennschärfe je Quelle, Rug- und Pump-Ausstieg

Der Einsatz gleitet nur dann über 3 % des Kontos, wenn zusätzlich die Vorhersagen dieser Quelle Gewinner von
Verlierern trennen: Rangkorrelation Vorhersage gegen Ergebnis nach Kosten der letzten 7 Tage, ab 30 Fällen, voll ab
0,2 (`pockets.discrimination`, in `brain_state.pockets.discrimination`). Heute (−0,01) bleibt jede Quelle beim kleinen
Einsatz, egal wie gut ein Muster aussieht.

Ausstieg vor dem Preis-Stopp: fällt die Liquidität (Curve-Coins: Tiefe der Bonding Curve, `curve_liq_usd`) 60 % unter
den Kauf, verkauft das Hirn als „rug“. Meldet die Rug-Prüfung einen bestätigten inszenierten Pump (Check `staged`,
`data.pattern`), verkauft es 10 % unter dem Hoch oder 15 min nach dem Hoch („staged“), in allen Konten.

## Lernkonto getrennt, Curve-Kurs, echte Ergebnisse (18.10., Lesung 05:07)

- **Zwei Konten.** Die Käufe aus `opportunities` (`kind='pool'`) buchen auf das Lernkonto (`brain_state.learn_balance_sol`, Start 0 SOL), alles andere auf das eigene Konto des Hirns (`balance_sol`). `brain_equity` führt beide: die ersten Spalten sind das eigene Konto (Stundenfaktor, Challenge, Kelly), `learn_balance_sol`, `learn_open_sol`, `learn_equity_sol` das Lernkonto. Die einmalige Umbuchung der Pool-Trades von vorher steht in `brain_state.learn_split` und in `equity_transfers`; der Stundenfaktor rechnet sie heraus, sie ist kein Gewinn.
- **Stichprobe.** Das Lernkonto kauft nur `BRAIN_POOL_SAMPLE` (Standard 10 %) der positiven Ziehungen. Echte Calls (`source_action='alerted'`) und Band-Kandidaten (`signals.band`) kauft es immer in Lerngröße; Rug-Prüfung und Liquidität entscheiden weiter.
- **Curve-Kurs.** Für Coins auf der Bonding Curve gilt der Live-Kurs des Snipers (`sniper.store.curve_now`, bis 120 s alt) statt DexScreener, beim Referenzkurs des Kaufs wie bei allen Ausstiegen. Zwischen zwei Preisrunden prüft `manage_curves()` diese Positionen in jedem Takt aus der Datenbank, ohne HTTP.
- **Echtes Ergebnis sofort.** Schließt eine Lernkonto-Position, steht ihr Netto nach Kosten sofort in `opportunities` (`exit_plan='real'`, `outcome_net`, `rug`), und das Pool-Modell lernt daraus, auch bei organischem Ausstieg. Die spätere Nachspielung ergänzt dort nur noch `outcomes`.
- **Stundenzeile.** Sie kommt als erster Schritt jeder Runde, in eigenem try, und nennt Lernkonto-Equity, offene und in der Stunde geschlossene Lernpositionen.

## Feste Labels für jede Gelegenheit (`labels.py`)

Jede Zeile in `opportunities` wird ab ihrer Meldung eine Stunde verfolgt, ob gekauft oder nicht: Curve-Coins zum Live-Kurs des Snipers (`curve_now`, aus der DB), alle anderen per DexScreener in 30er-Bündeln, je Coin alle 3 min, höchstens 10 Abrufe je Minute. Danach stehen in der Zeile (nur ergänzte Spalten) `label_max15`, `label_max60`, `label_ret60`, `label_min60` (alle nach Kosten), `label_rug` (Tief unter 10 % des Einstiegs, dann `label_ret60` = −100 %), `label_outcomes` (Netto je Plan auf diesem Pfad), `label_n`, `label_entry_sol`, `label_horizon_min`, `label_at`. Das Pool-Modell lernt jede gelabelte Zeile mit Gewicht 0,5 (`learner.learn_labels`), mit den Merkmalen der Hirn-Entscheidung, sonst denen der Quelle. Läuft in eigenem Thread (`BRAIN_LABELS=off` schaltet ab); die Stundenzeile nennt geschriebene und verfolgte Labels.

## Takt, Gates und Alarm (18.10., Lesung 06:24)

- **Kerzen im Hintergrund.** Die Lernrunde liest Kerzen nur noch aus den Caches; fehlt ein Fenster, holt es der Thread `brain-candles` (GeckoTerminal, 4 s je Abruf, geteilt) und die Nachspielung versucht es in einer späteren Runde. Vorher hielt die Lernrunde den 2-s-Takt den größten Teil jeder Minute auf, Wal-Käufe wurden Minuten zu spät gelesen.
- **Stundenzeile** nennt zusätzlich die häufigsten Gates, die gewollte Käufe aufhielten (`brain_opps.raw.gate`), die Lernrunden der Stunde (Anzahl, Sekunden, längste) und die längste Lücke zwischen zwei Schleifenrunden.
- **Alarm.** Liegt das eigene Konto (ohne Lernkonto) unter `BRAIN_EQUITY_ALARM_SOL` (3,5), kommt einmal je Stunde `brain alarm: equity … under …` ins Log und, wo Trades gemeldet werden, nach Telegram.
