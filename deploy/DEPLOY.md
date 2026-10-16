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
1. In Telegram `@BotFather` öffnen, `/newbot` (oder für einen neuen Token `/revoke`), den **Token** kopieren.
2. `open -e .env` und `TELEGRAM_BOT_TOKEN=<Token>` eintragen, speichern. Den Token nie in einen Chat posten.
3. Deinem Bot in Telegram `/start` schicken.
4. `.venv/bin/python deploy/ops.py setup-telegram`: findet deine Chat-ID, trägt sie in `.env` ein und schickt
   eine Testnachricht plus den ersten Bericht. Danach `.venv/bin/python mayham.py restart`, damit auch die
   Signale über Telegram kommen.

Ist `TELEGRAM_CHAT_ID` schon gesetzt, schickt `setup-telegram` nur eine Testnachricht und ruft `getUpdates`
nicht mehr auf: der Dienst `live` liest die Bot-Nachrichten dauerhaft (für `/notaus`, `/alles_verkaufen`,
`/weiter`, `/live`), und Telegram erlaubt nur einen Leser je Bot (sonst 409 Conflict). Eine neue Chat-ID
sucht `setup-telegram --neu`, vorher die App mit `.venv/bin/python mayham.py stop` anhalten.

Geplante Neustarts: Beendet sich ein Dienst absichtlich mit Exit-Code 3 (z. B. `signals`, wenn nach einem
Modell-Retrain der Code im Speicher nicht mehr zum neuen Modell passt), startet ihn der Supervisor nach 10 s
neu und notiert das in `data/planned_restarts.jsonl`. Der Watchdog meldet das einmal als
"🔄 signals: geplanter Neustart nach Modellwechsel", nicht als Fehler. Der Health-Check nach einem Auto-Update
wertet einen solchen Neustart je Dienst nicht als Absturz (kein Rollback) und wartet dafür bis zu 60 s länger;
ein zweiter im selben Fenster gilt als Neustart-Schleife.

## Helius-Credits und neuer Key

Der Bot nutzt den kostenlosen Helius-Plan (1.000.000 Credits pro Monat). Der stündliche Bericht zeigt
"Helius-Credits: heute a von b, Monat c von 1.000.000". Bei 80 % und bei 100 % kommt je einmal eine
Telegram-Meldung (pro Monat und Key). Dazu je Dienst und Tag eine Meldung, wenn ein Dienst sein Tagesbudget aufgebraucht hat oder
hochgerechnet mehr als das 1,5-Fache davon verbrauchen würde. Ist das Kontingent leer, läuft der Bot über öffentliche RPCs weiter,
nur langsamer.

Neuen Key eintragen (nur du, nie in einen Chat oder ins Repo):
1. Auf helius.dev einen neuen kostenlosen Key anlegen.
2. `open -e ~/Documents/WALLET/mayhambot/.env` und `HELIUS_API_KEY=<neuer Key>` setzen. Steht dort auch
   `SOLANA_RPC_URL=https://mainnet.helius-rpc.com/?api-key=...`, den Key dort ebenfalls ersetzen (oder die Zeile leeren).
3. Speichern, dann `.venv/bin/python mayham.py stop` und `.venv/bin/python mayham.py start` (jeder Dienst liest
   `.env` nur beim Start, deshalb alle neu starten).
   Der Credit-Zähler startet für den neuen Key neu ("Helius key changed, credit ledger reset" im Log).

## Automatische Updates (opt-in)

Einschalten (einmal im Terminal): `bash deploy/mac/install-updater.sh`. Dann prüft launchd (`de.mayhambot.updater`)
alle 10 min, ob auf `main` eine neue Version liegt, und spielt sie mit `mayham.py update` ein: Tests müssen grün
sein, sonst geht es automatisch zurück auf die laufende Version. Danach `mayham.py install-autostart`.

- Telegram: "⬆️ Update eingespielt: <hash>, Tests grün, 9 von 9 Dienste laufen" bzw. bei roten Tests einmal "↩️ Update nicht eingespielt" (weitere rote Runden nur im Log, nach 3 Runden einmal "🔴 Auto-Update hängt" mit den Testnamen).
  Alles steht auch in `logs/update.log`.
- Höchstens ein Deploy pro 10 min; Commits dazwischen kommen gebündelt. Vorher ein Backup mit Integritätsprüfung,
  schlägt es fehl, wartet das Update. Ist das stündliche Backup jünger als `AUTO_UPDATE_BACKUP_MAX_AGE_MIN`
  (60 min), wird es genommen (ein Backup der großen Datenbank dauert auf dem Mac ~10 min).
- Die Tests laufen in einer frischen Kopie des Codes (git worktree) ohne `data/`, `logs/` und `.env` des Bots
  und ohne Zugangsdaten in der Umgebung: der Mac prüft also dieselbe Welt wie eine frische Kopie in der Cloud.
- Eine Runde (Pull, pip, Tests) darf höchstens `AUTO_UPDATE_TIMEOUT_MIN` (45) dauern, sonst wird sie samt allen
  Unterprozessen abgebrochen und wie ein roter Test behandelt. Der Updater verlässt dafür die gedrosselte
  Hintergrund-Priorität von launchd (sonst dauerte der Testlauf auf dem Mac über 16 min statt ~3).
- Nach dem Neustart müssen alle Dienste 3 min stabil laufen (gleiche Prozess-ID, kein Neustart-Kreislauf) und das
  Dashboard antworten, sonst geht es automatisch zurück auf den alten Stand ("↩️ Update zurückgerollt: … Grund").
  Die Meldung nennt auch, wie oft 'database is locked' kam.
- Ein gescheiterter Commit wird nicht wieder probiert, erst der nächste.
- **Kein Update, solange Echtgeld im Spiel ist:** `LIVE_TRADING=true` in `.env` oder offene Echtgeld-Positionen.
  Dann kommt einmal "Update wartet", einspielen von Hand mit `.venv/bin/python mayham.py update`.
- Lokale Änderungen im Ordner blockieren das Update (Meldung kommt einmal).
- Not-Aus: `touch data/NO_AUTO_UPDATE` (wieder an: `rm data/NO_AUTO_UPDATE`) oder `AUTO_UPDATE=0` in `.env`.
  Ganz aus: `bash deploy/mac/install-updater.sh --off`. Stand: `.venv/bin/python deploy/autoupdate.py --status`.

## Alarme (Telegram)

Ein Problem muss in zwei Runden hintereinander auftauchen (≈4 min), dann kommt 🔴, bei Neustart 🔁,
wenn es behoben ist 🟢. Offene Probleme werden alle `ALERT_REPEAT_HOURS` (6 h) wiederholt.
Neustart höchstens alle 15 min. Geprüft wird:

- alle Prozesse aus (`app_down`) oder einzelne (`service_down`; überwacht werden automatisch alle Dienste aus `mayham.py`, auch der Forschungsdienst `research`), Dashboard antwortet nicht
- keine neuen Marktdaten seit `FEED_STALL_MIN` (15 min), Copy-Trader pollt nicht mehr
- weniger als 2 GB frei, letztes Backup älter als 3 h, Mac auf Akku
- Pause > 10 min seit dem letzten Watchdog-Lauf (Mac schlief / war aus)
- mehr als `LOCK_ALERT_PER_RUN` (20) 'database is locked' in den Logs innerhalb von 2 min (Datenbank überlastet)

## Backups

- `BACKUP_DIR` in `.env` (Standard `~/mayhambot-backups`). Besser ein zweiter Ort: externe Platte
  (`/Volumes/<Name>/mayhambot-backups`) oder iCloud Drive
  (`~/Library/Mobile Documents/com~apple~CloudDocs/mayhambot-backups`).
- Inhalt: konsistente Kopie der SQLite-DB (online per SQLite-Backup-API, mit Integritätsprüfung; enthält
  alle Trades, Wallets, Signale **und alle Modellversionen** `model_versions`), `.env`, übrige Dateien in `data/` (z.B. Forschungsberichte `data/reports/`),
  `copytrade/wallets.txt`.
- `SOLANA_PRIVATE_KEY` wird **nie** mitgesichert (steht als `<entfernt>` im Backup), Keypair-Dateien auch nicht.
  Deinen Wallet-Schlüssel sicherst du selbst (Seed-Phrase).
- Aufbewahrung: alles der letzten `BACKUP_KEEP_HOURS` (24 h), danach eins pro Tag für `BACKUP_KEEP_DAYS` (14), insgesamt höchstens `BACKUP_MAX_GB` (5 GB, die ältesten gehen zuerst, die neuesten 3 bleiben immer).
- Achtung: liegt `~/Documents` in iCloud ("Schreibtisch & Dokumente"), sollte die Live-DB dort nicht synchronisiert
  werden: dann `MAYHAM_DB_PATH=~/mayhambot-data/mayhambot.db` in `.env` setzen.

## Entwickeln, während der Bot läuft

- Claude arbeitet im GitHub-Repo und pusht nach `main`; der Mac holt neue Versionen mit `mayham.py update`
  (bzw. `deploy/update.sh` für die Variante mit Tests und Backup vorher), oder von selbst mit dem Auto-Update.
- Daten bleiben immer auf dem Mac (`data/`, nie im Repo). Für Analysen woanders: ein Backup nehmen und mit
  `ops.py restore <datei> --no-stop` in eine zweite Kopie einspielen.
- Nie zwei Bots gleichzeitig mit derselben Wallet live handeln lassen.
- Code kommt nur über `main` und das Auto-Update auf den Mac: keine Sitzung auf dem Mac ändert den Checkout
  (`git pull/reset/checkout`) oder ruft `mayham.py update` von Hand auf.
- Tests dürfen nicht von Zeitfenstern abhängen (kein `sleep` mit Millisekunden, keine knappen `window=`):
  der Testlauf im Auto-Update läuft unter launchd mit ProcessType Background, dort drosselt macOS die Timer
  stark. Zeit über eine Fake-Uhr (`monkeypatch.setattr(modul, "time", ...)`) oder Zähler steuern.

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
