#!/usr/bin/env bash
# Avvio con un comando (Linux/macOS): ./avvia.sh
cd "$(dirname "$0")" || exit 1
if command -v python3 >/dev/null 2>&1; then PY=python3; elif command -v python >/dev/null 2>&1; then PY=python; else
  echo "Python 3 non trovato. Installalo da https://www.python.org/downloads/ e riprova."; exit 1; fi
exec "$PY" run_webapp.py "$@"
