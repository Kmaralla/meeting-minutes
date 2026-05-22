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
    exec $PYTHON server.py
    ;;
  --both)
    echo "Starting recorder + Actions UI..."
    $PYTHON server.py &
    SERVER_PID=$!
    trap "kill $SERVER_PID 2>/dev/null" EXIT INT TERM
    exec $PYTHON meetingnotes.py "${@:2}"
    ;;
  *)
    exec $PYTHON meetingnotes.py "$@"
    ;;
esac
