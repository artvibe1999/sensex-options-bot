#!/usr/bin/env bash
# One-time install of the auto-stop guard:  sudo bash deploy/install_autostop.sh
set -eu
DIR="$(cd "$(dirname "$0")/.." && pwd)"
chmod +x "$DIR/deploy/autostop.sh"
cat > /etc/systemd/system/sxbot-autostop.service <<UNIT
[Unit]
Description=SXBOT: power off after market close if no position is open

[Service]
Type=oneshot
ExecStart=$DIR/deploy/autostop.sh
UNIT
cat > /etc/systemd/system/sxbot-autostop.timer <<UNIT
[Unit]
Description=Run the SXBOT auto-stop guard every 5 minutes

[Timer]
OnCalendar=*:0/5
Persistent=false

[Install]
WantedBy=timers.target
UNIT
# Boot guard: bring the containers up on every boot, even if they were left in a "stopped" state.
cat > /etc/systemd/system/sxbot-compose.service <<UNIT
[Unit]
Description=SXBOT: start containers at boot
After=docker.service network-online.target
Wants=network-online.target
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=$DIR
ExecStart=/usr/bin/docker compose up -d
TimeoutStartSec=300

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable sxbot-compose.service
systemctl start sxbot-compose.service
systemctl enable --now sxbot-autostop.timer
echo "Installed (autostop timer + boot-start service). Check with: systemctl list-timers sxbot-autostop.timer ; journalctl -u sxbot-autostop -n 20"
