# web/ und cli/ – Dashboard und Terminal

Beide lesen die gemeinsame SQLite-Datenbank (`core.config` → `data/…db`, oder `MAYHAM_DB_PATH`).
Fehlende Tabellen sind kein Fehler: was signals/, copytrade/ und model/ noch nicht geschrieben haben,
erscheint als leerer Zustand und füllt sich von selbst.

## Starten

```bash
.venv/bin/pip install -r requirements.txt        # enthält fastapi, uvicorn, textual, rich
.venv/bin/python -m web                          # Dashboard: http://localhost:8600
.venv/bin/python -m cli                          # Terminal-Oberfläche (Textual)
```

Zur Laufzeit wird nur Python gebraucht; das Frontend liegt fertig gebaut in `web/static/`.

| Variable | Standard | Bedeutung |
|---|---|---|
| `WEB_HOST` | `127.0.0.1` | `0.0.0.0` macht das Dashboard im Netz erreichbar |
| `WEB_PORT` | `8600` | Port |
| `WEB_PASSWORD` | – | HTTP-Login (Benutzer `mayham`). Ohne Passwort sind Änderungen nur von localhost erlaubt |
| `MAYHAM_DB_PATH` | aus `core.config` | andere Datenbank lesen (z. B. Testkopie) |
| `WEB_PUMP_1H` | `0.5` | ab welchem Anstieg in 1 h ein Signal als Treffer zählt |

## Terminal

```
python -m cli                    Live-Oberfläche: 1 Übersicht · 2 Signale · 3 Positionen · 4 Wallets · 5 Hinweise
                                 w/m = Modus 1/2 wechseln · p = Pause · k k = Signal-Coin kaufen · v v = Position verkaufen
                                 b = Mint kaufen · q = Ende
python -m cli status             Kurzbericht (auch ohne TTY, z. B. per SSH oder für Claude)
python -m cli signals -n 20      Signale mit Gewichtung der Eingänge
python -m cli wallets --tier ultra
python -m cli positions
python -m cli mode signal semi   off | manual | semi | auto
python -m cli pause on|off
python -m cli preset whale aggressiv
python -m cli sell 125
python -m cli buy <MINT> --sol 0.3
```

## Kaufen aus einem Signal

Jedes Signal hat einen Kaufen-Knopf (Dashboard: Signale-Seite und Coin-Seite, Terminal: `k` zweimal). Er zeigt vorher
Konto (Papier/Echtgeld), Preset von Modus 2, Betrag sowie Ziel und Stop und schickt dann einen Auftrag an den laufenden Bot
(`control.request_buy`, Strategie `signal`). Der Dialog zeigt danach die eröffnete Position oder den Grund, warum nicht gekauft
wurde. Gesperrt ist der Knopf, wenn die Notbremse an ist, der Coin schon gehalten wird oder ein Auftrag für ihn wartet.
Echtgeld nur, wenn Echtgeld für Modus 2 an ist und der Hauptschalter in `.env` steht; sonst Papier.


## Rug-Schutz (safety/)

- Signal-Feed, Coins-Liste, Copy-Trading (offene/abgeschlossene Positionen, Einstiege) und Kauf-Dialog zeigen das Rug-Risiko
  als Badge (niedrig/erhöht/hoch/kritisch + Score 0–100, "gesperrt" wenn der Bot den Coin nicht kauft). Signale und Positionen
  zeigen den Stand beim Signal bzw. Kauf (`signals.safety_*`, `ct_positions.safety_*`), Sperre und Kaufknopf folgen dem
  aktuellen Stand (`safety_tokens` + `safety_overrides`).
- Coin-Seite: Score, Gründe, positive Befunde, alle Prüfungen, Verlauf (`safety_history`) und die Knöpfe Sperren /
  Trotzdem freigeben / Automatisch entscheiden (mit Bestätigung). Steuerung: Liste aller gesperrten und manuell entschiedenen Coins.
- Geschrieben wird nur über `safety.block`, `safety.allow`, `safety.clear_override` (`POST /api/safety/{mint}`, nur von localhost).
- Terminal: `python -m cli safety [MINT]`, `python -m cli safety block|allow|clear MINT [Grund]`.
## Logs

`python -m web` schreibt mit Zeitstempel zeilenweise nach stdout (unter dem Starter: `logs/web.log`): Start, Fehler,
jede Änderung (POST) und jeder Fehler-Request. Das Abfragen der Daten durch das Dashboard selbst wird weggelassen;
`WEB_ACCESS_LOG=all` loggt alles, `WEB_LOG_LEVEL=debug` mehr Details.

## Sicherheit

- Steuerung schreibt nur über `copytrade.control` (Modus, Preset, Pause, Kauf-/Verkaufsaufträge).
  Der Echtgeld-Hauptschalter (`LIVE_TRADING` + Key in `.env`) wird nie geändert oder angezeigt.
  Echtgeld je Modus lässt sich im Dashboard nur einschalten, wenn der Hauptschalter an ist, und nur mit Bestätigung „ECHTGELD“.
- API und Terminal geben keine `.env`-Werte, Keys oder RPC-URLs aus; `core.http` wird früh importiert und schwärzt `api-key=`/`token=` in Logs.

## Entwickeln

```bash
cd web/frontend && npm install && npm run dev     # Vite auf :5173, /api geht an :8600
npm run build                                      # schreibt web/static (wird mit committet)
python -m web.devdata /tmp/demo.db                 # synthetische Demo-DB (nur zum Testen, nie die echte DB)
MAYHAM_DB_PATH=/tmp/demo.db python -m web
python -m pytest web/tests -q                      # braucht kein httpx/httpx2: die Tests rufen die ASGI-App direkt auf
```

Aufbau: `db.py` (Lesezugriff, WAL, Cache), `queries.py` (alle Auswertungen, von API und Terminal genutzt),
`server.py` (FastAPI), `frontend/` (React + ECharts), `cli/` (Textual + Rich).
