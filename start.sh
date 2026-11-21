#!/usr/bin/env bash
# Lance le dashboard. Options : --port 8765  --host 0.0.0.0 (VPS)
cd "$(dirname "$0")"
PY=python3
[ -x .venv/bin/python ] && PY=.venv/bin/python
exec "$PY" app.py "$@"
