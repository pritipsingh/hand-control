#!/bin/bash
# Start Hand Control (creates the virtualenv on first run).
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo "Setting up (first run)..."
  uv venv --python 3.11 .venv -q && uv pip install --python .venv -r requirements.txt
fi
exec .venv/bin/python hand_control.py "$@"
