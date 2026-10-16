#!/bin/bash
# Einmalig: Python-Umgebung anlegen und Pakete installieren.
set -e
cd "$(dirname "$0")"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
[ -f .env ] || cp .env.example .env
echo "Fertig. Starten mit: ./start.sh"
