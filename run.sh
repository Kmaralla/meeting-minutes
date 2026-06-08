#!/bin/bash
# Usage:
#   ./run.sh                  — start recording (mic → Whisper → agents)
#   ./run.sh --stop           — stop a running session
#   ./run.sh --status         — check if a session is running
#   ./run.sh --server         — start the Actions UI at http://localhost:8000
#   ./run.sh --both           — start recorder + UI server together
#   ./run.sh --dispatch-only  — re-run agents on saved transcript (no mic)
#   ./run.sh --help           — show all options

cd "$(dirname "$0")"
PYTHON=meetingenv/bin/python3
PID_FILE=/tmp/meetingnotes.pid
PORT=${MEETINGNOTES_PORT:-8000}

server_up() {
  "$PYTHON" - "$PORT" <<'PY' >/dev/null 2>&1
import sys, urllib.request
try:
    urllib.request.urlopen(f"http://127.0.0.1:{int(sys.argv[1])}/health/app", timeout=0.5)
    raise SystemExit(0)
except Exception:
    raise SystemExit(1)
PY
}

port_taken() {
  "$PYTHON" - "$PORT" <<'PY' >/dev/null 2>&1
import socket, sys
s = socket.socket()
s.settimeout(0.5)
try:
    s.connect(("127.0.0.1", int(sys.argv[1])))
    raise SystemExit(0)
except OSError:
    raise SystemExit(1)
finally:
    s.close()
PY
}

case "$1" in
  --stop)
    if [ -f "$PID_FILE" ]; then
      PID=$(cat "$PID_FILE")
      if kill -0 "$PID" 2>/dev/null; then
        echo "Stopping meetingnotes (PID $PID)..."
        kill "$PID"
        echo "Stopped."
      else
        echo "PID $PID not running. Cleaning up stale PID file."
        rm -f "$PID_FILE"
      fi
    else
      echo "No meetingnotes session running."
    fi
    ;;
  --status)
    if [ -f "$PID_FILE" ]; then
      PID=$(cat "$PID_FILE")
      if kill -0 "$PID" 2>/dev/null; then
        echo "Running (PID $PID)"
        ps -p "$PID" -o pid,etime,command | tail -1
      else
        echo "Not running (stale PID file)"
        rm -f "$PID_FILE"
      fi
    else
      echo "Not running"
    fi
    ;;
  --server)
    if server_up; then
      echo "Meeting Notes UI already running at http://localhost:$PORT"
      exit 0
    fi
    if port_taken; then
      echo "Port $PORT is already in use by another app. Stop it or set MEETINGNOTES_PORT=8001." >&2
      exit 1
    fi
    while true; do
      $PYTHON server.py
      echo "[meetingnotes] server exited — restarting in 2s..." >&2
      sleep 2
    done
    ;;
  --both)
    echo "Starting recorder + Actions UI..."
    SERVER_PID=
    if server_up; then
      echo "Using existing Meeting Notes UI at http://localhost:$PORT"
    elif port_taken; then
      echo "Port $PORT is already in use by another app. Stop it or set MEETINGNOTES_PORT=8001." >&2
      exit 1
    else
      $PYTHON server.py &
      SERVER_PID=$!
    fi
    cleanup() {
      if [ -n "$SERVER_PID" ]; then kill "$SERVER_PID" 2>/dev/null; fi
    }
    trap cleanup EXIT INT TERM
    exec $PYTHON meetingnotes.py "${@:2}"
    ;;
  *)
    exec $PYTHON meetingnotes.py "$@"
    ;;
esac
