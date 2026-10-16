#!/bin/bash
# Energie-Einstellungen für 24/7-Betrieb am Netzteil (einmalig, braucht dein Mac-Passwort).
#   deploy/mac/energy.sh              Mac schläft am Netzteil nie, Bildschirm darf aus
#   deploy/mac/energy.sh --lid        zusätzlich: läuft auch mit zugeklapptem Deckel weiter (Wärme beachten!)
#   deploy/mac/energy.sh --undo       zurück auf Apple-Standard
set -e
if [ "$1" = "--undo" ]; then
  sudo pmset -a disablesleep 0
  sudo pmset restoredefaults
  echo "Energie-Einstellungen zurückgesetzt."; exit 0
fi
sudo pmset -c sleep 0 disksleep 0 displaysleep 10 standby 0 autopoweroff 0 tcpkeepalive 1 womp 1
sudo pmset -a autorestart 1 2>/dev/null || true     # nach Stromausfall neu starten (wo unterstützt)
[ "$1" = "--lid" ] && sudo pmset -a disablesleep 1 && echo "Deckel zu = läuft weiter."
echo "Gesetzt. Aktuell:"; pmset -g | grep -E "^ (sleep|disksleep|displaysleep|standby|autorestart|SleepDisabled|tcpkeepalive|womp)"
