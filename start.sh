#!/bin/bash
cd "$(dirname "$0")"
[ -d .venv ] || ./setup.sh
.venv/bin/python mayham.py start
sleep 3 && open "http://localhost:${WEB_PORT:-8600}" 2>/dev/null || true
