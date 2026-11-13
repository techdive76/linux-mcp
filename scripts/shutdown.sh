#!/bin/bash

cd "$(dirname "$0")/.." || exit 1

PID_FILE="server.pid"

# ── Guard: not running? ────────────────────────────────────────────────────
if [ ! -f "$PID_FILE" ]; then
    echo "⚠️  No PID file found. Server may not be running."
    exit 1
fi

PID=$(cat "$PID_FILE")

if ! ps -p "$PID" > /dev/null 2>&1; then
    echo "⚠️  Process $PID is not running. Cleaning up stale PID file."
    rm -f "$PID_FILE"
    exit 0
fi

# ── Graceful shutdown ──────────────────────────────────────────────────────
echo "🛑 Stopping SSH Diagnostic MCP Server (PID $PID)..."
kill -SIGTERM "$PID"

# Wait up to 10 seconds for graceful exit
for i in $(seq 1 10); do
    if ! ps -p "$PID" > /dev/null 2>&1; then
        echo "✅ Server stopped gracefully."
        rm -f "$PID_FILE"
        exit 0
    fi
    sleep 1
done

# Force kill if still alive
echo "⚠️  Server did not stop gracefully. Force killing..."
kill -SIGKILL "$PID"
rm -f "$PID_FILE"
echo "✅ Server force stopped."
