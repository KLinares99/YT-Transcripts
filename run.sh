#!/usr/bin/env bash
# Create a virtualenv (first run only), install dependencies and start the app.
set -euo pipefail
cd "$(dirname "$0")"
PORT="${PORT:-8000}"

# Find a Python that is new enough (3.10+). macOS's built-in one is often 3.9.
PY=""
for cand in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$cand" >/dev/null 2>&1 && "$cand" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
    PY="$cand"; break
  fi
done
if [ -z "$PY" ]; then
  echo ""
  echo "  This app needs Python 3.10 or newer."
  echo "  Download it from https://www.python.org/downloads/ , install it, then run this again."
  echo ""
  exit 1
fi

if [ ! -d .venv ]; then
  echo "First run: setting things up (takes a minute)..."
  "$PY" -m venv .venv
  .venv/bin/pip install -q --upgrade pip
fi
.venv/bin/pip install -q -r requirements.txt

URL="http://localhost:$PORT"
echo ""
echo "  Channel Transcriber is running at $URL"
echo "  Keep this window open while you use it. Press Ctrl+C to stop."
echo ""
# Open the browser once the server is up (macOS: open, Linux: xdg-open).
( sleep 3; (command -v open >/dev/null && open "$URL") || (command -v xdg-open >/dev/null && xdg-open "$URL") || true ) >/dev/null 2>&1 &
exec .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port "$PORT"
