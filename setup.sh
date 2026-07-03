#!/bin/bash
# setup.sh — Mission Control deployment on the Pi
# Usage: copy the whole mission-control/ folder to ~/mission-control on the Pi, then:
#   bash setup.sh

set -e
cd ~/mission-control

echo "=== Mission Control Setup ==="

# 1. Virtual environment + dependencies
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip --quiet
pip install fastapi "uvicorn[standard]" openai redis pydantic python-dotenv anthropic --quiet
echo "✅ Dependencies installed"

# 2. .env template (only if missing)
if [ ! -f .env ]; then
cat > .env << 'ENVEOF'
# ── Mission Control config ──
NVIDIA_API_KEY=nvapi-PUT_YOUR_KEY_HERE
UPSTASH_REDIS_REST_URL=rediss://PUT_CONNECTION_STRING_HERE
UPSTASH_REDIS_REST_TOKEN=PUT_TOKEN_HERE

# Optional — Claude escalation (leave empty to disable, falls back to NVIDIA)
ANTHROPIC_API_KEY=

# Claude daily budget cap in USD (escalation auto-disables above this)
DAILY_BUDGET_USD=2.00
ENVEOF
echo "⚠️  Created .env — EDIT IT with your real keys: nano ~/mission-control/.env"
else
echo "✅ .env already exists"
fi

# 3. systemd service (auto-start on boot, auto-restart on crash)
sudo tee /etc/systemd/system/mission-control.service > /dev/null << SVCEOF
[Unit]
Description=Mission Control dashboard + Janet
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=/home/$USER/mission-control
ExecStart=/home/$USER/mission-control/venv/bin/uvicorn app:app --host 0.0.0.0 --port 8080
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SVCEOF

sudo systemctl daemon-reload
sudo systemctl enable mission-control
echo "✅ systemd service installed (mission-control)"

# 4. Hourly vault reindex timer (pull mirror + rebuild index — keeps thinking fresh)
sudo tee /etc/systemd/system/mc-reindex.service > /dev/null << RSVCEOF
[Unit]
Description=Mission Control vault reindex

[Service]
Type=oneshot
User=$USER
WorkingDirectory=/home/$USER/mission-control
EnvironmentFile=/home/$USER/mission-control/.env
ExecStart=/home/$USER/mission-control/venv/bin/python index.py reindex
RSVCEOF

sudo tee /etc/systemd/system/mc-reindex.timer > /dev/null << RTMREOF
[Unit]
Description=Hourly Mission Control reindex

[Timer]
OnBootSec=2min
OnUnitActiveSec=1h

[Install]
WantedBy=timers.target
RTMREOF

sudo systemctl daemon-reload
sudo systemctl enable --now mc-reindex.timer
echo "✅ Hourly reindex timer installed (mc-reindex.timer)"

echo ""
echo "=== Next steps ==="
echo "1. nano ~/mission-control/.env    (βάλε τα πραγματικά keys)"
echo "2. sudo systemctl start mission-control"
echo "3. Άνοιξε: http://100.107.28.116:8080  (μέσω Tailscale, από οποιαδήποτε συσκευή)"
echo ""
echo "Logs: journalctl -u mission-control -f"
