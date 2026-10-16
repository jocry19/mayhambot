#!/bin/bash
# Dauerbetrieb abschalten: Watchdog, Backups, Wachhalten und App-Autostart entfernen. Daten bleiben.
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
for l in de.mayhambot.watchdog de.mayhambot.backup de.mayhambot.keepawake; do
  launchctl bootout "gui/$(id -u)/$l" 2>/dev/null
  rm -f "$HOME/Library/LaunchAgents/$l.plist"
  echo "entfernt: $l"
done
"$ROOT/.venv/bin/python" "$ROOT/mayham.py" remove-autostart
