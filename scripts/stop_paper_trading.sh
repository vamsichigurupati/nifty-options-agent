#!/bin/bash
# Stop paper trading gracefully

PID_FILE="/Users/vamsikrishna.c/nifty-options-agent/data/logs/paper_trading.pid"
if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    kill "$PID" 2>/dev/null
    rm "$PID_FILE"
    echo "Stopped paper trading (PID $PID)"
else
    echo "No paper trading process found"
fi
