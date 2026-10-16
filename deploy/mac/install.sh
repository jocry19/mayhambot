#!/bin/bash
# mayhambot Dauerbetrieb auf dem Mac einrichten (idempotent, ohne sudo):
#   - App-Autostart (launchd, Neustart nach Absturz)        de.mayhambot.app
#   - Watchdog alle 2 min (Neustart + Telegram-Alarm)        de.mayhambot.watchdog
#   - Backup jede Stunde nach BACKUP_DIR                     de.mayhambot.backup
#   - Mac wach halten, solange er am Netzteil hängt          de.mayhambot.keepawake
# Energie-Einstellungen (einmalig, mit Passwort): deploy/mac/energy.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
[ -x .venv/bin/python ] || ./setup.sh
PY="$ROOT/.venv/bin/python"
LA="$HOME/Library/LaunchAgents"
UID_=$(id -u)
mkdir -p "$LA" logs data

plist() {  # label interval|keepalive program args...
  local label=$1 mode=$2; shift 2
  local args="" a
  for a in "$@"; do args+="<string>$a</string>"; done
  local sched
  if [ "$mode" = keepalive ]; then sched="<key>KeepAlive</key><true/>"
  else sched="<key>StartInterval</key><integer>$mode</integer>"; fi
  cat > "$LA/$label.plist" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key><array>$args</array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>RunAtLoad</key><true/>
  $sched
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$ROOT/logs/${label#de.mayhambot.}.log</string>
  <key>StandardErrorPath</key><string>$ROOT/logs/${label#de.mayhambot.}.log</string>
</dict></plist>
PL
  launchctl bootout "gui/$UID_/$label" 2>/dev/null || true
  # bootout returns before teardown is finished; bootstrapping too early fails with "5: Bad request"
  for _ in $(seq 20); do launchctl print "gui/$UID_/$label" >/dev/null 2>&1 || break; sleep 0.5; done
  for try in 1 2 3; do
    launchctl bootstrap "gui/$UID_" "$LA/$label.plist" && break
    [ $try = 3 ] && { echo "❌ $label konnte nicht geladen werden"; return 1; }
    sleep 2
  done
  echo "✅ $label"
}

# 1) the app itself (plist written by mayham.py, KeepAlive restarts it after crashes)
if launchctl print "gui/$UID_/de.mayhambot.app" >/dev/null 2>&1; then
  echo "✅ de.mayhambot.app (läuft schon)"
else
  "$PY" mayham.py stop >/dev/null 2>&1 || true   # no second copy next to the launchd one
  "$PY" mayham.py install-autostart
fi
# 2-4) operations
plist de.mayhambot.watchdog 120 "$PY" "$ROOT/deploy/ops.py" watchdog
plist de.mayhambot.backup 3600 "$PY" "$ROOT/deploy/ops.py" backup
# -i: no idle sleep, -s: no system sleep while on AC power, -m: no disk sleep. Display may sleep.
plist de.mayhambot.keepawake keepalive /usr/bin/caffeinate -i -s -m

echo
echo "Fertig. Status: deploy/mac/status.sh  (oder Doppelklick auf 'mayhambot status.command')"
echo "Einmalig noch: deploy/mac/energy.sh  (Energie-Einstellungen, fragt nach deinem Mac-Passwort)"
