#!/data/data/com.termux/files/usr/bin/bash
# memedesk on Android (Termux).
#   first time:  bash scripts/termux.sh setup
#   start:       bash scripts/termux.sh start     (dashboard: http://127.0.0.1:8084)
#   stop:        bash scripts/termux.sh stop
#   status/log:  bash scripts/termux.sh log
set -u
cd "$(dirname "$0")/.."
mkdir -p data
PID=data/desk.pid

case "${1:-start}" in
  setup)
    pkg update -y && pkg install -y python termux-api
    [ -f .env ] || cp .env.example .env
    echo "Done. Also: Android Settings > Apps > Termux > Battery > Unrestricted."
    ;;
  start)
    if [ -f "$PID" ] && kill -0 "$(cat $PID)" 2>/dev/null; then echo "already running"; exit 0; fi
    command -v termux-wake-lock >/dev/null && termux-wake-lock   # keep the CPU awake overnight
    nohup bash -c '
      while true; do
        python -m memedesk run >> data/overnight.log 2>&1 &
        echo $! > data/py.pid
        wait $! && break   # clean stop => exit
        echo "$(date) desk crashed, restarting in 20s" >> data/overnight.log
        sleep 20
      done' > /dev/null 2>&1 &
    echo $! > "$PID"
    echo "memedesk running (pid $(cat $PID)). Dashboard: http://127.0.0.1:8084"
    ;;
  stop)
    if [ -f "$PID" ]; then
      kill "$(cat $PID)" 2>/dev/null                              # stop the restart loop
      [ -f data/py.pid ] && kill -TERM "$(cat data/py.pid)" 2>/dev/null   # desk writes its final report
      rm -f "$PID" data/py.pid
    fi
    command -v termux-wake-unlock >/dev/null && termux-wake-unlock
    echo "stopped"
    ;;
  log)
    tail -n 40 -f data/overnight.log
    ;;
  *) echo "usage: $0 setup|start|stop|log"; exit 1 ;;
esac
