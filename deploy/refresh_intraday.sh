#!/usr/bin/env bash
# Weekly refresh of the intraday approvals (data/approved_intraday.json): download fresh 5-minute SENSEX / India VIX
# history with today's Kite session and re-run the walk-forward backtest (intraday_backtest.py).
# Run by sxbot-intraday-refresh.timer on Fridays after the close (see install_intraday_refresh.sh). Safe to run by hand.
#   * never runs during market hours (it would compete with the bot for CPU);
#   * keeps the VM up while it runs (data/NO_AUTOSTOP), then hands control back to the auto-stop guard;
#   * on failure (e.g. no Kite login today) the previous approvals stay in force (valid 60 days);
#   * the bot loads the new file by itself at its next session start: no restart needed.
set -u
DIR="$(cd "$(dirname "$0")/.." && pwd)"
export TZ=Asia/Kolkata
dow=$(date +%u)
hm=$((10#$(date +%H%M)))
if [ "$dow" -le 5 ] && [ "$hm" -ge 900 ] && [ "$hm" -lt 1535 ]; then
  echo "$(date '+%F %T') market hours: refresh skipped"
  exit 0
fi

flag="$DIR/data/NO_AUTOSTOP"
made_flag=0
if [ ! -e "$flag" ]; then
  touch "$flag"
  made_flag=1
fi
trap '[ "$made_flag" = 1 ] && rm -f "$flag"' EXIT

cd "$DIR" || exit 1
LOG="$DIR/logs/intraday_refresh.log"
if timeout 45m docker compose exec -T bot python intraday_backtest.py --fetch-kite 1400 --workers 2 >"$LOG" 2>&1; then
  approved="$(grep -E '^Approved' "$LOG" | tail -1)"
  oos="$(grep -oE 'honest OOS estimate[^:]*: -?[0-9,]+' "$LOG" | tail -1)"
  msg="Weekly intraday refresh done. ${approved}. ${oos}. New approvals load at the next session start."
else
  code=$?
  msg="Weekly intraday refresh FAILED (exit ${code}): previous approvals kept. Was Kite logged in today? See logs/intraday_refresh.log"
fi
echo "$(date '+%F %T') $msg" | tee -a "$DIR/logs/intraday_refresh_history.log"

TELEGRAM_BOT_TOKEN="$(grep -m1 '^TELEGRAM_BOT_TOKEN=' "$DIR/.env" 2>/dev/null | cut -d= -f2- | tr -d "\"' \r")"
TELEGRAM_CHAT_ID="$(grep -m1 '^TELEGRAM_CHAT_ID=' "$DIR/.env" 2>/dev/null | cut -d= -f2- | tr -d "\"' \r")"
if [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
  curl -s -m 10 "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" --data-urlencode "text=SXBOT: ${msg}" >/dev/null
fi
exit 0
