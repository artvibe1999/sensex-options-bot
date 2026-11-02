#!/usr/bin/env bash
# One-time install of the weekly intraday-approval refresh:  sudo bash deploy/install_intraday_refresh.sh
# Runs deploy/refresh_intraday.sh every Friday at 16:05 IST (10:35 UTC), after the 15:40 options close and the 16:00
# report, before the auto-stop window (16:30-17:30), which it holds off while it works. Not catch-up (Persistent=false): if the VM is off on
# Friday afternoon the run is skipped, and the approvals (valid 60 days) simply stay as they are.
set -eu
DIR="$(cd "$(dirname "$0")/.." && pwd)"
chmod +x "$DIR/deploy/refresh_intraday.sh"
cat > /etc/systemd/system/sxbot-intraday-refresh.service <<UNIT
[Unit]
Description=SXBOT: weekly intraday backtest refresh (approved_intraday.json)
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
ExecStart=$DIR/deploy/refresh_intraday.sh
TimeoutStartSec=3600
UNIT
cat > /etc/systemd/system/sxbot-intraday-refresh.timer <<UNIT
[Unit]
Description=Run the SXBOT intraday refresh every Friday 16:05 IST

[Timer]
OnCalendar=Fri *-*-* 10:35:00 UTC
Persistent=false

[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl enable --now sxbot-intraday-refresh.timer
echo "Installed. Next run: systemctl list-timers sxbot-intraday-refresh.timer ; run now: sudo systemctl start sxbot-intraday-refresh.service"
