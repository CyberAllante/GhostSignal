#!/bin/sh
# Run GhostSignal without installing anything: ./gs demo, ./gs serve, ./gs setup
DIR="$(cd "$(dirname "$0")" && pwd)"
PY="$(command -v python3 || command -v python)"
if [ -z "$PY" ]; then
  echo "Python 3 not found. Install it from https://www.python.org/downloads/ and try again."
  exit 1
fi
PYTHONPATH="$DIR${PYTHONPATH:+:$PYTHONPATH}" exec "$PY" -m ghostsignal.cli "$@"
