#!/bin/bash
# Pull latest code and restart the service

cd ~/nifty-options-agent
git pull origin main
source venv/bin/activate
pip install -r requirements.txt --quiet
sudo systemctl restart nifty-agent
echo "Updated and restarted. Status:"
sudo systemctl status nifty-agent --no-pager
