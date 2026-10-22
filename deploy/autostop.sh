#!/usr/bin/env bash
# Powers the VM off after the market closes, but ONLY if the bot has no open position.
# Run every 5 minutes by a systemd timer (see install_autostop.sh). Safe to run by hand.
#   Window: 15:40-16:30 IST (the bot squares off at 15:15 and finishes its day at 15:30).
#   Skip:   create data/NO_AUTOSTOP to keep the VM up (delete it to resume).
set -u
DIR="$(cd "$(dirname "$0")/.." && pwd)"
export TZ=Asia/Kolkata
hm=$((10#$(date +%H%M)))
if [ "$hm" -lt 1540 ] || [ "$hm" -ge 1630 ]; then exit 0; fi
if [ -e "$DIR/data/NO_AUTOSTOP" ]; then echo "NO_AUTOSTOP present: staying up"; exit 0; fi

msg="$(python3 "$DIR/deploy/can_stop.py" "$DIR/data/state.json")"; code=$?
if [ "$code" -ne 0 ]; then
  echo "$(date '+%F %T') blocked: $msg"
  flag="$DIR/data/.autostop_alerted_$(date +%F)"
  if [ ! -e "$flag" ]; then                 # one Telegram alert per day
    touch "$flag"
    TELEGRAM_BOT_TOKEN="$(grep -m1 '^TELEGRAM_BOT_TOKEN=' "$DIR/.env" 2>/dev/null | cut -d= -f2- | tr -d "\"' \r")"
    TELEGRAM_CHAT_ID="$(grep -m1 '^TELEGRAM_CHAT_ID=' "$DIR/.env" 2>/dev/null | cut -d= -f2- | tr -d "\"' \r")"
    if [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
      curl -s -m 10 "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
        --data-urlencode "text=SXBOT: VM auto-stop BLOCKED - ${msg}. Check the position, then stop the VM yourself." >/dev/null
    fi
  fi
  exit 0
fi
# Give the containers a clean stop (they flush the database), then power off.
echo "$(date '+%F %T') $msg: stopping containers and powering off"
(cd "$DIR" && docker compose stop -t 30) || true
sync
systemctl poweroff
