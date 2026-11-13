#!/bin/bash

cd "$(dirname "$0")/.." || exit 1

PID_FILE="server.pid"
LOG_FILE="mcp.log"

if [ ! -f "$PID_FILE" ]; then
    echo "🔴 Server is NOT running (no PID file)"
    exit 1
fi

PID=$(cat "$PID_FILE")

if ps -p "$PID" > /dev/null 2>&1; then
    echo "🟢 Server is running"
    echo "   PID     : $PID"
    echo "   Uptime  : $(ps -p $PID -o etime= | tr -d ' ')"
    echo "   Memory  : $(ps -p $PID -o rss= | awk '{printf "%.1f MB", $1/1024}')"
    echo ""
    echo "── Last 10 log lines ──────────────────────────────"
    tail -n 10 "$LOG_FILE"
else
    echo "🔴 Server is NOT running (stale PID $PID)"
    rm -f "$PID_FILE"
    exit 1
fi
