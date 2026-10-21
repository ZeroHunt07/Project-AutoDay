#!/usr/bin/env bash
# Start the daemon detached; exit if one is already running.
# Usage: sudo ./run-standalone.sh
set -euo pipefail

APP=auto-brightness
BIN=/usr/local/bin/$APP
PIDFILE=/run/$APP.pid
LOGFILE=/var/log/$APP.log

if [ "$(id -u)" -ne 0 ]; then
    exec sudo -E "$0" "$@"
fi

if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "already running: pid $(cat "$PIDFILE")"
    exit 0
fi

nohup "$BIN" >>"$LOGFILE" 2>&1 &
echo $! > "$PIDFILE"
echo "started pid $! - logging to $LOGFILE"
