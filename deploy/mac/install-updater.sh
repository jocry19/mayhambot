#!/bin/bash
# Auto-Update einschalten (opt-in): launchd prüft alle 10 min main und spielt neue Versionen nur mit grünen
# Tests ein (deploy/autoupdate.py). Ausschalten: bash deploy/mac/install-updater.sh --off
# Kurz pausieren ohne Ausschalten: touch data/NO_AUTO_UPDATE (wieder an: rm data/NO_AUTO_UPDATE)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
LABEL=de.mayhambot.updater
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
UID_=$(id -u)
PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"

launchctl bootout "gui/$UID_/$LABEL" 2>/dev/null || true
for _ in $(seq 20); do launchctl print "gui/$UID_/$LABEL" >/dev/null 2>&1 || break; sleep 0.5; done
if [ "${1:-}" = "--off" ]; then
  rm -f "$PLIST"
  echo "Auto-Update aus."
  exit 0
fi
mkdir -p "$HOME/Library/LaunchAgents" logs data
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$PY</string><string>$ROOT/deploy/autoupdate.py</string></array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>RunAtLoad</key><true/>
  <key>StartInterval</key><integer>600</integer>
  <key>ProcessType</key><string>Standard</string>
  <key>StandardOutPath</key><string>$ROOT/logs/updater.log</string>
  <key>StandardErrorPath</key><string>$ROOT/logs/updater.log</string>
</dict></plist>
PL
for try in 1 2 3; do
  launchctl bootstrap "gui/$UID_" "$PLIST" && break
  [ $try = 3 ] && { echo "❌ $LABEL konnte nicht geladen werden"; exit 1; }
  sleep 2
done
echo "✅ Auto-Update an (alle 10 min). Stand: $PY deploy/autoupdate.py --status"
