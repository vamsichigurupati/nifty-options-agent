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

# 8. Set up token update web server (for daily Dhan token refresh)
sudo tee /etc/systemd/system/nifty-token-server.service > /dev/null << 'TOKENEOF'
[Unit]
Description=NIFTY Token Update Web Server
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/nifty-options-agent
ExecStart=/home/ubuntu/nifty-options-agent/venv/bin/python -m paper_trading.token_server
Restart=always
RestartSec=10
StandardOutput=append:/home/ubuntu/nifty-options-agent/data/logs/token_server.log
StandardError=append:/home/ubuntu/nifty-options-agent/data/logs/token_server_error.log
Environment=TZ=Asia/Kolkata
Environment=TOKEN_UPDATE_SECRET=nifty2026

[Install]
WantedBy=multi-user.target
TOKENEOF

# 9. Enable both services
sudo systemctl daemon-reload
sudo systemctl enable nifty-agent
sudo systemctl enable nifty-token-server

# 10. Open port 8080 for token server
sudo iptables -I INPUT -p tcp --dport 8080 -j ACCEPT

echo ""
echo "=== Setup Complete ==="
echo ""
echo "Next steps:"
echo "  1. Edit .env:  nano ~/nifty-options-agent/.env"
echo "     Add: TOKEN_UPDATE_SECRET=your_secret_password"
echo "  2. Start services:"
echo "     sudo systemctl start nifty-token-server"
echo "     sudo systemctl start nifty-agent"
echo "  3. Open in browser: http://<SERVER_IP>:8080/"
echo "     Use this page to update Dhan token daily from your phone"
echo "  4. Logs:  tail -f ~/nifty-options-agent/data/logs/service.log"
echo ""
echo "Daily routine:"
echo "  1. Open Dhan app -> generate new access token"
echo "  2. Open http://<SERVER_IP>:8080/ on your phone"
echo "  3. Paste token, hit Update"
echo "  4. Done. System restarts automatically."
echo ""
echo "The services auto-start on server reboot."
echo "The trading script auto-sleeps outside market hours."
