#!/usr/bin/env bash
# Linux/macOS: run the desk in the background with auto-restart. (On Android use scripts/termux.sh.)
# Stop: kill $(cat data/desk.pid); kill $(cat data/py.pid)
set -u
cd "$(dirname "$0")/.."
mkdir -p data
nohup bash -c '
  while true; do
    python3 -m memedesk run >> data/overnight.log 2>&1 &
    echo $! > data/py.pid
    wait $! && break
    echo "$(date) desk crashed, restarting in 15s" >> data/overnight.log
    sleep 15
  done' > /dev/null 2>&1 &
echo $! > data/desk.pid
echo "memedesk running (pid $(cat data/desk.pid)); dashboard http://127.0.0.1:8084; tail -f data/overnight.log"
