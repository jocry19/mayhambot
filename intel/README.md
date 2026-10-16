# intel/ — Zentrale: was der Bot sieht und weiß

Owned by the thread "Zentrale". Führt alles zusammen, was die anderen Dienste sammeln, als **Merkmale zu einem
Zeitpunkt** mit **Abdeckungsgrad**, dazu ein Live-Ticker und die Messung, ob jedes Merkmal Vorhersagen verbessert.
**Keine Netz-Abfragen, keine Helius-Credits**: nur Lesen der gemeinsamen DB, Schreiben nur in `intel_*`.

```bash
.venv/bin/python -m intel run                 # Dauerbetrieb (Launcher-Dienst "intel", Log logs/intel.log)
.venv/bin/python -m intel once                # eine Runde
.venv/bin/python -m intel coin <MINT> [--ts T] | wallet <ADDR> [--ts T] | market
.venv/bin/python -m intel events [-n 30]
.venv/bin/python -m intel eval [--days 14] [--json]
```

## Interface (nur lesend, für das Gehirn in copytrade/, model/, signals/, web/, cli/)

```python
import intel
intel.features_at(kind, key, ts, conn=None)
#  kind 'coin' (key = mint) | 'wallet' (key = address) | 'market' (key 'solana')
#  -> {kind, key, ts, features {name: float|None}, coverage {group: 0..1, all}, meta}
#  nur Daten mit Zeitstempel <= ts (walk-forward). None = unbekannt, nie 0.
intel.feature_table(conn, kind, since, until=None, keys=None) -> [(key, ts, feature, value)]
#  gespeicherte Historie: Coin und Markt alle 15 min, Wallets stündlich (Tabelle intel_snapshots)
intel.latest(conn, kind, key) -> dict | None      # jüngster Stand des Dienstes (Coins alle 5 min)
intel.FEATURES -> {"coin": {name: (group, label_de, unit)}, "wallet": {...}, "market": {...}}
intel.evaluation(conn, days=14) -> dict           # Messung je Merkmal (siehe unten)
intel.view(conn) / intel.coin_view(conn, mint)    # GET /api/intel, /api/intel/coin/<mint>
```

## Merkmale je Coin (Gruppen)

| Gruppe | Merkmale | Quelle |
|---|---|---|
| vol | Trades, Volumen, Wallets und Personen 60 min (Cluster zählen einmal), Wash/Hin-und-her-Anteil, Top-3-Anteil | `trades` |
| flow | Kaufdruck 15 min, Netto 15/60 min, Käufer und Beschleunigung, Wal-Anteil, Kleinanleger, Smart Money netto nach Personen, Smart-Käufer die aussteigen, Verkäufe von Dev/Bundle/Insider | `trades`, Stufen, Cluster, `sniper_launch_wallets` |
| micro | Order-Flow-Imbalance 5/15 min, Kyle's Lambda (log-Kursänderung je $1.000 Netto-Fluss pro Minute), VPIN (20 Volumen-Buckets), Absorption (Kaufdruck ohne Kursreaktion), Kauftempo (< 1 = Erschöpfung) | `trades` |
| liq | Liquidität, 7-Tage-Hoch, Abstand, Änderung 15/60 min, Liquidität/MC | `token_prices` |
| exec | Kosten hin und zurück für 0,1/0,25/0,5 SOL: Pool-Modell (AMM: Impact size/(Seite+size); pump.fun-Curve aus MC) plus Gebühren; Stufe 3 misst mit Jupiter | `token_prices` |
| hold | Top-10, Top-1, Insider, Dev, Launch-Bundle, Änderung über 1 h, Rug-Risiko | `intel_holders` (Kopie jedes Rug-Checks) |
| px | Kurs und realisierte Volatilität 5/15/60 min, Abstand zum 24-h-Hoch, Alter, MC | `token_prices`, `tokens` |
| mkt | Marktphase, Sektor- und Narrativ-Hitze, Stärke ggü. Sektor | `market.features` |

**Je Wallet** (30 Tage bis ts): Coins, abgeschlossene Positionen, mittlere Haltedauer, Scalp-Anteil (< 5 min),
Konzentration (Anteil der Käufe im Lieblings-Coin), Gruppengröße, Käufe mit Mitkäufern binnen 60 s und feste
Mitkäufer, Exit-Capture (Verkaufspreis / Hoch seit Einstieg) gesamt und je Marktphase beim Einstieg, Verspätung
unserer Erkennung (`ct_swaps.seen_at − ts`, Median und 90 %).

**Markt**: Phase, heiß/Risk-off, Volumen 1 h und ggü. Median, SOL 1 h/24 h/7 d, Breite, Launches, 2x-, Rug- und
Migrationsrate, Smart Money netto, Hitze von Sektor und Narrativ, Kapitalrotation (aus `market_snapshots`).

Zeitpunkt-Regeln: Stufen kommen aus der Modellversion, die zu ts aktiv war (`model_tier_history`), sonst aus
`wallets.tier` (`meta.tier_pit = False`). Cluster sind die aktuellen des Modells. Holder-Stände zählen ab ihrem
Rug-Check-Zeitpunkt. Ein Kurs älter als 30 min gilt als unbekannt.

## Ticker (`intel_events`)

ultra_buy, smart_entry (≥ 2 Smart-Personen in 15 min), smart_exit, insider_sell, rotation (Smart-Wallet verkauft A
und kauft binnen 30 min B), liq_drop (≥ 30 % in 20 min), wash, holder_shift (≥ 10 Punkte in 1 h), risk_jump.
Jedes Ereignis hat einen Dedup-Schlüssel.

## Taugt es? (`intel.evaluation`)

Jeder Coin-Stand bekommt nach einer Stunde sein Ergebnis aus `token_prices` (ret_1h, max_ret_1h, min_ret_1h).
Je Coin und Stunde zählt eine Stichprobe. Je Merkmal: Spearman-Korrelation mit dem Höchststand, Pump-Rate
(max ≥ +50 %) im oberen gegen das untere Fünftel, Zwei-Stichproben-z-Test, Absturzrate (≤ −50 %).
**Klar** bei |z| ≥ 2 und ≥ 40 Stichproben je Fünftel. Das Gehirn nimmt nur Merkmale auf, die zusätzlich im
eigenen Walk-forward-Test nach Kosten wirken.

## Kosten

Eine Runde pro Minute, Coins alle 5 min (≤ 150), Wallets stündlich (≤ 400), jeweils ein paar Sekunden CPU.
Speicher: ~100 Zeilen je 15 min in `intel_snapshots`, Aufbewahrung `INTEL_KEEP_DAYS` (21).

## Tabellen

| Tabelle | Inhalt |
|---|---|
| `intel_snapshots` | (kind, key, ts) PK, coverage, features_json, meta_json, ret_1h, max_ret_1h, min_ret_1h, labeled_at |
| `intel_latest` | (kind, key) PK, jüngster Stand |
| `intel_holders` | (mint, safety_ts) PK, top10/top1/insider/dev/bundle/launch %, roundtrip_share, safety_score, blocked |
| `intel_events` | id, ts, kind, mint, wallet, severity, title, text, data_json, dedup UNIQUE |
| `intel_state` | `service_status`, `evaluation` |
