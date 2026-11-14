#!/data/data/com.termux/files/usr/bin/bash
# memedesk on Android (Termux).
#   first time:  bash scripts/termux.sh setup     (also creates home-screen buttons for Termux:Widget)
#   start:       bash scripts/termux.sh start     (dashboard: http://127.0.0.1:8084)
#   stop:        bash scripts/termux.sh stop
#   update:      bash scripts/termux.sh update    (stop -> git pull -> start)
#   profile:     bash scripts/termux.sh fast      (or active / strict; restarts the desk)
#   log:         bash scripts/termux.sh log
set -u
cd "$(dirname "$0")/.."
APP="$(pwd)"
mkdir -p data
PID=data/desk.pid

kill_all() {
  [ -f "$PID" ] && kill "$(cat $PID)" 2>/dev/null                       # restart loop
  [ -f data/py.pid ] && kill -TERM "$(cat data/py.pid)" 2>/dev/null      # desk writes its final report
  if command -v pkill >/dev/null; then                                   # leftovers from older versions
    pkill -f "memedesk run" 2>/dev/null
  fi
  rm -f "$PID" data/py.pid
  sleep 3
}

case "${1:-start}" in
  setup)
    pkg update -y && pkg install -y python git termux-api procps
    [ -f .env ] || cp .env.example .env
    # Home-screen buttons (install the Termux:Widget app from F-Droid, then add its widget)
    mkdir -p ~/.shortcuts ~/.shortcuts/tasks
    printf '#!/data/data/com.termux/files/usr/bin/bash\nbash "%s/scripts/termux.sh" start\nsleep 4\ntermux-open-url http://127.0.0.1:8084\n' "$APP" > ~/.shortcuts/MemeDesk-Start
    printf '#!/data/data/com.termux/files/usr/bin/bash\nbash "%s/scripts/termux.sh" stop\n' "$APP" > ~/.shortcuts/tasks/MemeDesk-Stop
    printf '#!/data/data/com.termux/files/usr/bin/bash\ntermux-open-url http://127.0.0.1:8084\n' > ~/.shortcuts/tasks/MemeDesk-Open
    printf '#!/data/data/com.termux/files/usr/bin/bash\nbash "%s/scripts/termux.sh" update\n' "$APP" > ~/.shortcuts/MemeDesk-Update
    chmod +x ~/.shortcuts/MemeDesk-* ~/.shortcuts/tasks/MemeDesk-*
    # Start automatically after a phone restart (needs the Termux:Boot app from F-Droid)
    mkdir -p ~/.termux/boot
    printf '#!/data/data/com.termux/files/usr/bin/bash\ntermux-wake-lock\nbash "%s/scripts/termux.sh" start\n' "$APP" > ~/.termux/boot/memedesk
    chmod +x ~/.termux/boot/memedesk
    echo "Done."
    echo " - Android Settings > Apps > Termux > Battery > Unrestricted"
    echo " - Optional: Termux:Widget (home-screen buttons) and Termux:Boot (autostart) from F-Droid"
    ;;
  start)
    if [ -f "$PID" ] && kill -0 "$(cat $PID)" 2>/dev/null; then echo "already running"; exit 0; fi
    kill_all
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
    echo "memedesk running ($(git log -1 --format=%h 2>/dev/null), $(grep -m1 '^DESK_PROFILE=' .env 2>/dev/null || echo 'DESK_PROFILE=active')). Dashboard: http://127.0.0.1:8084"
    ;;
  stop)
    kill_all
    command -v termux-wake-unlock >/dev/null && termux-wake-unlock
    echo "stopped"
    ;;
  update)
    kill_all
    git pull --ff-only && echo "updated to $(git log -1 --format='%h %s')"
    exec bash "$0" start
    ;;
  fast|active|strict)
    # switch profile and drop old .env lines that would override it
    [ -f .env ] || cp .env.example .env
    sed -i -e '/^DESK_PROFILE=/d' -e '/^DESK_MIN_SCORE=/d' -e '/^DESK_SCAN_INTERVAL_S=/d' \
           -e '/^DESK_SCORE_KILL_COOLDOWN_MIN=/d' -e '/^DESK_MIN_LIQ_MCAP_PCT=/d' .env
    echo "DESK_PROFILE=$1" >> .env
    echo "profile set to $1"
    kill_all
    exec bash "$0" start
    ;;
  log)
    tail -n 40 -f data/overnight.log
    ;;
  *) echo "usage: $0 setup|start|stop|update|fast|active|strict|log"; exit 1 ;;
esac
