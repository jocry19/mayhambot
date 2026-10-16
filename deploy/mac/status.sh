#!/bin/bash
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PY="$ROOT/.venv/bin/python"; [ -x "$PY" ] || PY=python3
"$PY" "$ROOT/deploy/ops.py" status
