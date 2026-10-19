# lab: Prüfstand

Rechnet alle `LAB_EVERY_H` Stunden (Standard 6) feste Hypothesen auf der echten Datenbank nach und sagt, welche
Tasche nach Kosten trägt (Plan `plan/ueberarbeitung_2026-10-18.md`). Liest die Datenbank nur (`mode=ro`), schreibt
nichts hinein und handelt nicht.

```bash
python -m lab once     # eine Runde, Tabelle auf der Konsole
python -m lab run      # Dienst: alle LAB_EVERY_H Stunden
```

Ergebnis: `data/lab/report.json` (für Dashboard und Entscheider) und `data/lab/report.md`, dazu eine Logzeile
`lab: N hypotheses, carries: …, candidates: …`.

## Hypothesen (`hypotheses.py`)

| Familie | Quelle | Einstieg | Ergebnis |
|---|---|---|---|
| `ueberlebende_*`, `nach_pump_*`, `ruecksetzer_*`, `abgekuehlt_*`, `jung_abgekuehlt_*`, `*_regime_oben/unten_*` | `token_prices` (alle 5 min ein Entscheidungspunkt je Coin, je Coin höchstens alle 2 h ein Einstieg) | erster Kurs ≥ 60 s nach dem Punkt | Kurs nach 60 bzw. 240 min, Tief unter 10 % = Rug −100 % |
| `nach_migration_Nm_60m` | `sniper_launches.migrated_ts` + `token_prices` | N min nach der Migration | 60 min halten |
| `launch_mcA-B_*` | `sniper_paths`, ohne Marken-Masche (Ersteller ≥ 75 % oder Ersteller plus Start-Block ≥ 70 %, die safety hart sperrt) | beim Entscheid des Snipers, nach MC-Band in SOL | Ziel ×k / Stopp s (Fill 3 % unter der Marke) oder halten |
| `signal_<gruppe>_<einstieg>_<ausstieg>` | `signals` (Gruppe `call`) und `signal_shadow` (`schatten_<grund>`, vom Trichter abgelehnt) + `token_prices` | `sofort` (≥ 60 s nach dem Alarm) oder `limit15` / `limit30` (erst wenn der Kurs binnen 2 h 15 / 30 % unter den Alarmkurs fällt) | 60 bzw. 240 min halten oder `tp100_240m` (Verkauf bei ×2); eigene Art `signal`, gibt also keine Markt-Käufe frei |
| `launch_tempoNs_*` | `sniper_curve` + `sniper_launches.created_ts`, ohne Marken-Masche | N = 2 / 5 / 10 / 20 s nach dem Create, erster Kurvenpunkt (≤ 3 s später) | `halten60` (letzter Punkt in 60 min) oder `trail20` (Trail 20 % ab +20 %, Notstopp −50 %) |
| `label_p05_<quelle>_<regel>` | wie `label_*`, nur Gelegenheiten mit `pred_mean` ≥ 0,05 | Label-Einstieg | wie `label_*` |
| `label_p08_<pool|cs|quelle>_trail20@<a>` | pool = alle Quellen außer sniper, cs = copytrade+strategy, sonst je Quelle; `pred_mean` ≥ 0,08, nur Trail-Regel | Label-Einstieg | Trail 20 % |
| `label_<quelle>_<regel>` | `opportunities.label_rules` | Label-Einstieg | jede Ausstiegsregel der Labels, Kosten neu gerechnet |

Neue Hypothesen kommen als Eintrag in `PANEL_HYPS` oder als eigene Familie dazu; Namen nie umbenennen, damit die
Berichte vergleichbar bleiben.

## Kosten (`costs.py`)

Je Runde: 2 × (Gebühr der Börse + Preiswirkung Einsatz gegen halbe Liquidität + 0,0002 SOL Transaktionskosten je
Seite gegen den Einsatz) + Verzugs-Aufschlag (Launch 3 %, Kopie 2 %, sonst 1 %). Curve 1,25 % je Seite,
pumpswap 0,30 %, Raydium/Meteora 0,25 %. Beispiele bei 0,1 SOL: pumpswap mit 500 k$ Liquidität ≈ 2,1 %, Curve ≈ 6,5 %.

## Freigabe

`traegt`: nach Kosten in allen drei Zeitdritteln positiv, n ≥ 200, ≥ 50 Coins, t ≥ 3 über die Coin-Mittel
(ein Coin mit vielen Einstiegen zählt einmal). `kandidat`: im Mittel positiv und in zwei von drei Dritteln.
Nur eine tragende Tasche soll Einsatz im Hauptkonto bekommen. Jede Zeile im Bericht trägt `brain_kind` (die Art im
Entscheider: `market` für Kursreihen- und Migrations-Hypothesen, `launch` für `launch_*`, `pool` für `label_*`, dazu
`source` = die meldende Quelle), damit der Entscheider die Freigabe der richtigen Art zuordnet.

Als Dienst `lab` in mayham.py: die nächste Runde zählt ab `at` im letzten Bericht (ein Deploy-Neustart rechnet nicht neu), wartet während `data/TRAINING` (Retrain) und läuft mit nice 10; jede Runde rechnet in einem eigenen Prozess, damit der Speicher danach frei ist.

Einstellungen: `LAB_EVERY_H` (6), `LAB_WINDOW_H` (72, Rückblick), `LAB_STAKE_SOL` (0,1), `LAB_DIR` (data/lab).
