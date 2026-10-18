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
| `launch_mcA-B_*` | `sniper_paths` | beim Entscheid des Snipers, nach MC-Band in SOL | Ziel ×k / Stopp s (Fill 3 % unter der Marke) oder halten |
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
Nur eine tragende Tasche soll Einsatz im Hauptkonto bekommen.

Als Dienst `lab` in mayham.py: die nächste Runde zählt ab `at` im letzten Bericht (ein Deploy-Neustart rechnet nicht neu), wartet während `data/TRAINING` (Retrain) und läuft mit nice 10.

Einstellungen: `LAB_EVERY_H` (6), `LAB_WINDOW_H` (72, Rückblick), `LAB_STAKE_SOL` (0,1), `LAB_DIR` (data/lab).
