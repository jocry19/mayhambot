# mayhambot rund um die Uhr

Der Bot läuft auf deinem Mac (`~/Documents/WALLET/mayhambot`), ohne Claude. Claude ändert den Code über
GitHub (`jocry19/mayhambot`, nur Transport), der Mac holt neue Versionen und startet sich neu.

## Einmal einrichten (Mac)

```bash
cd ~/Documents/WALLET/mayhambot
bash deploy/mac/install.sh      # oder Doppelklick: deploy/mac/mayhambot dauerbetrieb einrichten.command
bash deploy/mac/energy.sh       # Energie-Einstellungen, fragt nach deinem Mac-Passwort
```

`install.sh` richtet vier launchd-Dienste ein (kein sudo nötig, überlebt Neustarts):

| Dienst | Was er tut |
|---|---|
| `de.mayhambot.app` | der Bot selbst (`mayham.py run`: Signale, Copy-Trader, Dashboard). launchd startet ihn nach Absturz neu, `mayham.py` startet einzelne abgestürzte Teile nach 10 s neu. |
| `de.mayhambot.watchdog` | alle 2 min: prüft Prozesse, Dashboard, frische Marktdaten, Copy-Trader-Polling, Speicherplatz, Backup-Alter, Netzteil. Startet bei Ausfall neu und schickt Telegram-Alarme. Rotiert die Logs. |
| `de.mayhambot.backup` | jede Stunde ein Backup nach `BACKUP_DIR` |
| `de.mayhambot.keepawake` | `caffeinate -i -s -m`: der Mac schläft nicht ein, solange er am Netzteil hängt (Bildschirm darf aus) |

## Was du am Mac einstellen musst

1. **Netzteil dran lassen.** Auf Akku schläft der Mac trotzdem ein; der Watchdog meldet "läuft auf Akku".
2. **Energie:** `bash deploy/mac/energy.sh` setzt: am Netzteil nie schlafen, Festplatte nie schlafen,
   Bildschirm nach 10 min aus, Neustart nach Stromausfall.
3. **Deckel:** Ein MacBook schläft beim Zuklappen immer ein, außer mit externem Bildschirm, oder du führst
   `bash deploy/mac/energy.sh --lid` aus (dann läuft er auch zugeklappt; nicht in die Tasche stecken, Wärme).
4. **Angemeldet bleiben.** Bildschirm sperren ist okay, abmelden nicht. Nach einem Neustart oder Stromausfall
   musst du dich einmal anmelden (wegen FileVault), danach startet alles von selbst.
5. **Ordnerzugriff:** Beim ersten Start fragt macOS evtl. "python möchte auf Dokumente zugreifen": **Erlauben**.
6. **Telegram:** `TELEGRAM_BOT_TOKEN` und `TELEGRAM_CHAT_ID` in `.env` eintragen, dann
   `.venv/bin/python deploy/ops.py test-alert`.

Ehrlich gesagt: Ist der Mac aus, zugeklappt ohne `--lid` oder ohne Internet, läuft der Bot nicht. Der Watchdog
merkt die Lücke danach und meldet "X min Pause erkannt". Für echten 24/7-Betrieb ohne Mac siehe "Später: Server".

## Bedienen

```bash
bash deploy/mac/status.sh                      # alles auf einen Blick (oder 'mayhambot status.command')
.venv/bin/python mayham.py status|logs|restart # App selbst
.venv/bin/python deploy/ops.py backup          # Backup sofort
.venv/bin/python deploy/ops.py list-backups
.venv/bin/python deploy/ops.py restore <datei> # stoppt, spielt ein, startet; alte DB bleibt als *.vor-restore-*
bash deploy/update.sh                          # sicheres Update: pull, Tests, Backup, Neustart, Health-Check
bash deploy/mac/uninstall.sh                   # Dauerbetrieb aus (Daten bleiben)
```

`deploy/update.sh` startet nur neu, wenn die Tests grün sind; sonst läuft die alte Version weiter.

## Stündlicher Bericht und Telegram einrichten

Der Watchdog schickt jede Stunde (`SUMMARY_EVERY_HOURS`, 0 = aus) einen Bericht per Telegram: laufende Dienste
und Laufzeit, Datenfeed, beobachtete Coins, neue Trades, Smart Wallets nach Tier, Signale (1 h / 24 h, bestes
Signal), Papier-/Echtgeld-Positionen, PnL und Kapital. Von Hand: `.venv/bin/python deploy/ops.py summary`.

Telegram einrichten (einmalig, 2 Minuten):
1. In Telegram `@BotFather` öffnen, `/newbot` schicken, Namen vergeben, den **Token** kopieren.
2. Deinem neuen Bot irgendeine Nachricht schicken, dann `https://api.telegram.org/bot<TOKEN>/getUpdates` im
   Browser öffnen und die Zahl bei `"chat":{"id":...` kopieren (deine **Chat-ID**).
3. In `.env` eintragen: `TELEGRAM_BOT_TOKEN=<Token>` und `TELEGRAM_CHAT_ID=<Chat-ID>`, dann
   `.venv/bin/python deploy/ops.py test-alert`. Signale, Alarme und Berichte kommen dann alle dort an.

## Alarme (Telegram)

Ein Problem muss in zwei Runden hintereinander auftauchen (≈4 min), dann kommt 🔴, bei Neustart 🔁,
wenn es behoben ist 🟢. Offene Probleme werden alle `ALERT_REPEAT_HOURS` (6 h) wiederholt.
Neustart höchstens alle 15 min. Geprüft wird:

- alle Prozesse aus (`app_down`) oder einzelne (`service_down`; überwacht werden automatisch alle Dienste aus `mayham.py`, auch der Forschungsdienst `research`), Dashboard antwortet nicht
- keine neuen Marktdaten seit `FEED_STALL_MIN` (15 min), Copy-Trader pollt nicht mehr
- weniger als 2 GB frei, letztes Backup älter als 3 h, Mac auf Akku
- Pause > 10 min seit dem letzten Watchdog-Lauf (Mac schlief / war aus)

## Backups

- `BACKUP_DIR` in `.env` (Standard `~/mayhambot-backups`). Besser ein zweiter Ort: externe Platte
  (`/Volumes/<Name>/mayhambot-backups`) oder iCloud Drive
  (`~/Library/Mobile Documents/com~apple~CloudDocs/mayhambot-backups`).
- Inhalt: konsistente Kopie der SQLite-DB (online per SQLite-Backup-API, mit Integritätsprüfung; enthält
  alle Trades, Wallets, Signale **und alle Modellversionen** `model_versions`), `.env`, übrige Dateien in `data/` (z.B. Forschungsberichte `data/reports/`),
  `copytrade/wallets.txt`.
- `SOLANA_PRIVATE_KEY` wird **nie** mitgesichert (steht als `<entfernt>` im Backup), Keypair-Dateien auch nicht.
  Deinen Wallet-Schlüssel sicherst du selbst (Seed-Phrase).
- Aufbewahrung: alles der letzten `BACKUP_KEEP_HOURS` (48 h), danach eins pro Tag für `BACKUP_KEEP_DAYS` (30).
- Achtung: liegt `~/Documents` in iCloud ("Schreibtisch & Dokumente"), sollte die Live-DB dort nicht synchronisiert
  werden: dann `MAYHAM_DB_PATH=~/mayhambot-data/mayhambot.db` in `.env` setzen.

## Entwickeln, während der Bot läuft

- Claude arbeitet im GitHub-Repo und pusht nach `main`; der Mac holt neue Versionen mit `mayham.py update`
  (bzw. `deploy/update.sh` für die Variante mit Tests und Backup vorher).
- Daten bleiben immer auf dem Mac (`data/`, nie im Repo). Für Analysen woanders: ein Backup nehmen und mit
  `ops.py restore <datei> --no-stop` in eine zweite Kopie einspielen.
- Nie zwei Bots gleichzeitig mit derselben Wallet live handeln lassen.

## Später: Server (Option)

Alles liegt bereit, falls der Bot einmal auch bei ausgeschaltetem Mac laufen soll (kleiner Linux-VPS, 2 GB RAM):

- `deploy/docker/Dockerfile` + `compose.yml`: ein Befehl `docker compose -f deploy/docker/compose.yml up -d --build`.
  Daten in `data/`, Dashboard nur auf `127.0.0.1:8600` (Zugriff: `ssh -L 8600:localhost:8600 user@server`).
- `deploy/linux/`: systemd-Units (App, Watchdog alle 2 min, Backup stündlich) und `server-setup.sh`
  (Docker, Firewall nur SSH, Swap, automatische Sicherheitsupdates).
- Umzug ohne Datenverlust: `bash deploy/push-to-server.sh user@server --with-data` überträgt Code und ein
  frisches Backup (DB inkl. Modellversionen), spielt es ein und startet. Danach auf dem Mac
  `bash deploy/mac/uninstall.sh`, damit nicht zwei Bots handeln.
- Zugang: du legst einen SSH-Schlüssel an (`ssh-keygen -t ed25519`) und trägst den **öffentlichen** Teil beim
  Anbieter ein. Private Schlüssel und Passwörter nie in den Chat.

Getestet (Linux-Container, ohne Marktzugang): Image baut, Container wird "healthy", Watchdog erkennt gestoppte
App/Container und startet neu, Datenstau-Erkennung, Backup während die App schreibt, Restore, Log-Rotation,
Backup-Aufbewahrung.
