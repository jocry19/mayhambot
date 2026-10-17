#!/bin/bash
# Neue Code-Version übernehmen und sicher neu starten (Mac und Linux).
#   deploy/update.sh            git pull (falls ein Remote eingerichtet ist), Pakete, Tests, Backup, Neustart
#   deploy/update.sh --no-pull  Code liegt schon da (z.B. von Claude per Remote Control geändert)
# Schlägt ein Test fehl, wird NICHT neu gestartet: die laufende Version bleibt aktiv.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || ./setup.sh

if [ "${1:-}" != "--no-pull" ] && [ -d .git ] && git remote | grep -q .; then
  git diff --quiet && git diff --cached --quiet || { echo "Ungesicherte Änderungen im Ordner: erst committen."; exit 1; }
  # mayham.py update tests the new commit in a separate worktree first and only then moves this folder onto it
  # (a plain git pull put untested code on disk that the next service restart would have run after red tests)
  "$PY" mayham.py update
  sleep 20
  "$PY" deploy/ops.py health && echo "Update ok: $(git log -1 --format='%h %s')"
  exit 0
fi
"$PY" -m pip install -q -r requirements.txt
echo "Tests ..."
"$PY" tests/test_logic.py >/dev/null
"$PY" -m pytest tests -q -x
"$PY" deploy/ops.py backup            # Sicherung vor jedem Update
if [ "$(uname)" = Darwin ] && launchctl print "gui/$(id -u)/de.mayhambot.app" >/dev/null 2>&1; then
  launchctl kickstart -k "gui/$(id -u)/de.mayhambot.app"
elif [ -f deploy/docker/compose.yml ] && [ "${WATCHDOG_RUNTIME:-}" = docker ]; then
  docker compose -f deploy/docker/compose.yml up -d --build
else
  "$PY" mayham.py restart
fi
sleep 20
"$PY" deploy/ops.py health && echo "Update ok: $(git log -1 --format='%h %s' 2>/dev/null || date)"
