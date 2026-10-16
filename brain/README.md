# brain: ein lernender Entscheider statt Strategie-Versionen

Ein Dienst (`python -m brain run`, im Launcher als `brain`) mit eigenem **Papierkonto** (5 SOL) neben den
bisherigen Strategien, die als Vergleich weiterlaufen. Nie Echtgeld: es gibt keinen Live-Pfad.

## Was er tut

Jede Gelegenheit wird einzeln entschieden, mit allem, was der Bot in dem Moment weiß:

| Quelle | Gelegenheit | Kontext (Auszug) |
|---|---|---|
| `ct_swaps` (Copy-Tracker) | Wal-Kauf ≥ `BRAIN_MIN_WHALE_SOL`, höchstens `BRAIN_MAX_WHALE_DELAY_S` alt | Wal-Stufe und Cluster, Wal-Bilanz nach Kosten (`learn.wallet_edge`), Launch-Bot, Kaufgröße, weitere Smart Wallets im Coin (30 min), Signal auf dem Coin (6 h), Zentrale-Merkmale des Wals |
| `signals` | Smart-Money-Signal | Score, Smart Wallets, Ultra dabei, Signal-EV, Call-Nummer |
| `sniper_launches` + `sniper_positions` | Sniper-Einstieg | Sniper-Score und -Risiko, Bundle, Insider, Dev-Launches, Top-10, Socials, Modell-Chance |
| alle | | Liquidität, Market Cap, Coin-Alter, Kurs 5 min, Kaufdruck, Preis seit Auslöser, Verzögerung, Rug-Urteil (`safety.get`), Marktphase (`market.context.phase`), walk-forward-bestätigte Zentrale-Merkmale (`intel.trusted_at`), Tageszeit, eigene Form |

Für sechs Ausstiegspläne (`plans.py`: Schnell rein und raus, Erster Pump, Pump mitnehmen, Mittlerer Lauf, Laufen
lassen, Mondschuss) schätzt das Modell den **Netto-Gewinn nach Kosten** und wie sicher es sich ist. Der Ausstieg ist
Teil der Entscheidung: Ziel, Stop, Trailing, Haltezeit, „Ansturm vorbei“, und bei Wal-Kopien „Wal hat verkauft“.

- **Überzeugt** (Erwartung > 0 und zu ≥ `BRAIN_MIN_CONF` = 75 % sicher): Kauf, Größe 0,1 SOL bei 75 % bis 0,5 SOL
  bei 95 % Sicherheit.
- **Erkunden** (nicht überzeugt, aber eine Ziehung aus dem Posterior sagt „könnte sich lohnen“, Thompson Sampling):
  0,1 SOL, höchstens `BRAIN_EXPLORE_PER_DAY` (6) am Tag.
- Sonst **auslassen**. Auch ausgelassene Gelegenheiten werden später nachgespielt und gelernt.

Harte Grenzen, die das Modell nie übergeht: Marktpreis vorhanden, Liquidität ≥ `BRAIN_MIN_LIQ_USD`, Rug-Prüfung
(`safety.assess` quick, ohne Ergebnis kein Kauf), eine Position je Coin, `BRAIN_MAX_OPEN`, Tagesverlust
`BRAIN_MAX_DAILY_LOSS_SOL`, Guthaben.

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
2. **Jede Gelegenheit**, auch die ausgelassenen: wenn der längste Plan vorbei ist (~4,2 h), wird jeder Plan
   nachgespielt (`copytrade.sim`, echte Wal-Verkäufe als Zwangsausstieg; Tracker-Ausfälle machen eine Wal-Gelegenheit
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
