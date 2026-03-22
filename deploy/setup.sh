#!/bin/bash
# Oracle Cloud / Any Ubuntu Server — One-time Setup
# Run this after SSH-ing into the server

set -e

echo "=== NIFTY Options Agent — Server Setup ==="

# 1. Install Python 3.9+ and dependencies
sudo apt update
sudo apt install -y python3 python3-pip python3-venv git

# 2. Clone repo
cd ~
git clone https://github.com/vamsichigurupati/nifty-options-agent.git
cd nifty-options-agent

# 3. Create virtual environment
python3 -m venv venv
source venv/bin/activate

# 4. Install Python packages
pip install --upgrade pip
pip install -r requirements.txt
pip install dhanhq yfinance

# 5. Create .env file
cp .env.example .env
echo ""
echo "=== IMPORTANT: Edit .env with your API keys ==="
echo "Run: nano ~/nifty-options-agent/.env"
echo ""
echo "Required:"
echo "  DHAN_CLIENT_ID=your_client_id"
echo "  DHAN_ACCESS_TOKEN=your_access_token"
echo ""

# 6. Create data directories
mkdir -p data/logs data/paper_trades data/dhan_paper_trades data/reports

# 7. Set up systemd service for auto-start
sudo tee /etc/systemd/system/nifty-agent.service > /dev/null << 'SERVICEEOF'
[Unit]
Description=NIFTY Options Paper Trading Agent
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/nifty-options-agent
ExecStart=/home/ubuntu/nifty-options-agent/venv/bin/python -m paper_trading.dhan_paper --live
Restart=on-failure
RestartSec=60
StandardOutput=append:/home/ubuntu/nifty-options-agent/data/logs/service.log
StandardError=append:/home/ubuntu/nifty-options-agent/data/logs/service_error.log
Environment=TZ=Asia/Kolkata

[Install]
WantedBy=multi-user.target
SERVICEEOF

# 8. Enable service (starts on boot)
sudo systemctl daemon-reload
sudo systemctl enable nifty-agent

echo ""
echo "=== Setup Complete ==="
echo ""
echo "Next steps:"
echo "  1. Edit .env:  nano ~/nifty-options-agent/.env"
echo "  2. Start:      sudo systemctl start nifty-agent"
echo "  3. Check:      sudo systemctl status nifty-agent"
echo "  4. Logs:       tail -f ~/nifty-options-agent/data/logs/service.log"
echo "  5. Stop:       sudo systemctl stop nifty-agent"
echo ""
echo "The service auto-starts on server reboot."
echo "The trading script auto-sleeps outside market hours."
