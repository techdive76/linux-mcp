#!/bin/bash

# Run from the repo root so .env, config/, ssh/ and the PID/log files resolve
cd "$(dirname "$0")/.." || exit 1

# ── Config ─────────────────────────────────────────────────────────────────
APP_MODULE="linux_mcp.server:starlette_app"
HOST="0.0.0.0"
PORT=8030
LOG_FILE="mcp.log"
PID_FILE="server.pid"

# ── Guard: already running? ────────────────────────────────────────────────
if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    if ps -p "$PID" > /dev/null 2>&1; then
        echo "⚠️  Server is already running (PID $PID). Run scripts/shutdown.sh first."
        exit 1
    else
        echo "⚠️  Stale PID file found. Cleaning up..."
        rm -f "$PID_FILE"
    fi
fi

# ── Start ──────────────────────────────────────────────────────────────────
echo "🚀 Starting SSH Diagnostic MCP Server..."

set -a && source .env && set +a
# Import the package from src/ (no install needed)
export PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}"
nohup uvicorn "$APP_MODULE" \
    --host "$HOST" \
    --port "$PORT" \
    > "$LOG_FILE" 2>&1 &

echo $! > "$PID_FILE"

# ── Verify it actually started ─────────────────────────────────────────────
sleep 2
PID=$(cat "$PID_FILE")

if ps -p "$PID" > /dev/null 2>&1; then
    echo "✅ Server started successfully"
    echo "   PID  : $PID"
    echo "   URL  : http://$HOST:$PORT/mcp"
    echo "   Logs : tail -f $LOG_FILE"
else
    echo "❌ Server failed to start. Check logs:"
    cat "$LOG_FILE"
    rm -f "$PID_FILE"
    exit 1
fi
