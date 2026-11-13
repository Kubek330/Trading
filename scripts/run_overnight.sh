#!/usr/bin/env bash
# Run the desk in the background with auto-restart. Logs: data/overnight.log
# Stop it with: kill $(cat data/desk.pid)
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p data
(
  while true; do
    python3 -m memedesk run >> data/overnight.log 2>&1 && break   # clean exit (SIGTERM) => stop
    echo "$(date -Is) desk crashed, restarting in 15s" >> data/overnight.log
    sleep 15
  done
) &
echo $! > data/desk.pid
echo "memedesk running (pid $(cat data/desk.pid)); tail -f data/overnight.log"
