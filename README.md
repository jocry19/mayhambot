# mayhambot

Solana-Memecoin Smart-Money-Tracker: findet Wallets mit nachweislich guter Bilanz, misst ihren
Geldfluss in neue Coins und schickt ein Signal, wenn viel Geld von vielen guten Wallets gleichzeitig
in einen Coin fließt. Dashboard mit Charts pro Coin und pro Wallet.

## App starten (Mac, ohne Claude)

Einmalig im Terminal im Ordner `mayhambot`:

```bash
./setup.sh                  # Python-Umgebung + Pakete, legt .env aus .env.example an
```

Danach per Doppelklick auf **`mayhambot starten.command`** / **`mayhambot stoppen.command`**, oder im Terminal:

```bash
./start.sh                                  # Signale + Copy-Trader (Papiermodus) + Research + Dashboard im Hintergrund
./stop.sh                                   # alles stoppen
.venv/bin/python mayham.py status           # was läuft
.venv/bin/python mayham.py logs             # Live-Logs (logs/*.log)
.venv/bin/python mayham.py install-autostart  # startet beim Login automatisch und nach Abstürzen neu
.venv/bin/python mayham.py remove-autostart
.venv/bin/python mayham.py update           # neueste Version von GitHub holen, Pakete aktualisieren, neu starten
```

Dashboard: http://localhost:8600 (`web/`, siehe `web/README.md`). Terminal-Ansicht: `.venv/bin/python -m cli`.
Einstellungen und Keys in `.env`. Echtgeld nur mit `LIVE_TRADING=true` plus Schlüssel (siehe `copytrade/README.md`).

## Einzelne Teile / Entwicklung

```bash
.venv/bin/python -m signals.run --once -v               # ein Signal-Zyklus
.venv/bin/python -m signals.run add-wallet <ADRESSE> --label X
.venv/bin/python -m signals.run test-telegram
.venv/bin/python -m copytrade stats                     # siehe copytrade/README.md
.venv/bin/python tests/test_logic.py && .venv/bin/python -m pytest tests -q
```

Code-Transport: privates GitHub-Repo `mayhambot` (nur Transport, der Bot läuft nur auf dem Mac in
`~/Documents/WALLET/mayhambot`). Claude pusht Änderungen nach `main`; `mayham.py update` holt sie und startet neu.
`.env`, Datenbank (`data/`), Logs und Schlüssel sind per `.gitignore` ausgeschlossen und bleiben nur auf dem Mac.

## Wie es funktioniert

Jeder Zyklus:
1. **Coins finden**: neue/trendende Solana-Coins von DexScreener (Profile, Boosts) und GeckoTerminal
   (trending, neue Pools), gefiltert nach Liquidität und Market Cap. Coins, die Smart Wallets gerade
   kaufen, kommen automatisch dazu.
2. **Trades laden**: die letzten Swaps jedes Pools (GeckoTerminal), Wallet = Signer der Transaktion.
3. **Wallets bewerten** (0–100) nur aus ihrer eigenen Bilanz: Trefferquote, frühe Einstiege (Preis danach
   ≥2x), ROI, Konstanz (Gewinn nicht nur aus einem Glückstreffer), gewichtet mit der Anzahl Coins.
   Bots (sehr viele Trades pro Coin) fliegen raus.
4. **Historie nachladen** für die vielversprechendsten Wallets (Helius oder öffentlicher RPC), damit
   ihre Bilanz über mehr Coins geht als die gerade beobachteten.
5. **Signal-Score** pro Coin im Fenster `SIGNAL_WINDOW_MIN`:
   `100 × (0,4 × Geld + 0,3 × Anzahl + 0,3 × Qualität)` aus Netto-Inflow der Smart Wallets,
   Anzahl netto kaufender Smart Wallets und deren USD-gewichtetem Score. Der Wallet-Score wird dabei
   **ohne** den bewerteten Coin berechnet, damit kein Zirkelschluss entsteht.
6. **Signal** wenn Score, Wallet-Anzahl und Netto-Inflow über den Schwellen liegen (mit Cooldown),
   an Konsole und Telegram. Danach wird gemessen, wie der Preis nach 5 min, 15 min und 1 h lief,
   damit man im Dashboard sieht, ob die Signale taugen.

## Ordner

- `core/` Datenquellen, SQLite, Wallet-Scoring (Schnittstellen: `core/README.md`)
- `signals/` Signal-Engine, Notifier, Runner
- `web/` Dashboard (FastAPI + Frontend, Port 8600), `cli/` Terminal-Ansicht
- `dashboard/` altes Streamlit-Dashboard, wird nicht mehr gestartet
- `data/mayhambot.db` die Datenbank

## Keys (optional)

| Variable | Wofür |
|---|---|
| `HELIUS_API_KEY` | Wallet-Historie über Helius statt langsamem öffentlichem RPC, schnellerer RPC |
| `SOLANA_RPC_URL` | eigener RPC-Endpunkt |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Signale aufs Handy (Bot über @BotFather anlegen) |

Keys gehören nur in `.env`, nie in den Chat oder ins Repo.
