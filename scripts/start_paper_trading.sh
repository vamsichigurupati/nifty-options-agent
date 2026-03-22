#!/bin/bash
# Start paper trading during market hours
# Runs V1 + V3 with Rs.50,000 each, polls every 2 min

cd /Users/vamsikrishna.c/nifty-options-agent
source venv/bin/activate

LOG_DIR="data/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/paper_trading_$(date +%Y%m%d).log"

echo "$(date): Starting paper trading..." >> "$LOG_FILE"

# Run paper trading (will auto-stop after market hours)
python -m paper_trading.dhan_paper --live >> "$LOG_FILE" 2>&1 &
PID=$!
echo $PID > "$LOG_DIR/paper_trading.pid"
echo "$(date): Started with PID $PID" >> "$LOG_FILE"
