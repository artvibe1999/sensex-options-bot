#!/usr/bin/env bash
# Deploy a specific, reviewed commit of SENSEX PRO on the VM (docker compose in ~/sxbot).
#
#   bash scripts/deploy.sh <commit-or-tag>
#
# 1. refuses during market hours while a position is open (data/state.json);
# 2. records the running commit in data/deploy.prev (rollback target);
# 3. checks out the requested commit, builds the images, runs the full test suite in a throwaway container
#    (no network); on any failure it checks the previous commit out again and stops - the running bot is untouched;
# 4. restarts the services, waits for the dashboard health endpoint, prints the mode and kill-switch state.
# Never touches .env, data/ (token, database, settings, mode), logs/ or collected market data. No rm -rf.
# Rollback:  bash scripts/deploy.sh "$(cat data/deploy.prev)"
set -euo pipefail
TARGET="${1:?usage: deploy.sh <commit-or-tag>}"
DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$DIR"
export TZ=Asia/Kolkata

if sudo python3 deploy/can_stop.py data/state.json >/dev/null 2>&1; then :; else
  hm=$((10#$(date +%H%M))); dow=$(date +%u)
  if [ "$dow" -le 5 ] && [ "$hm" -ge 900 ] && [ "$hm" -lt 1540 ]; then
    echo "An open position is recorded during market hours: not deploying now."; exit 1
  fi
fi

PREV="$(git rev-parse HEAD)"
git fetch --quiet origin --tags || echo "git fetch failed (no network/credentials?): using local objects"
git cat-file -e "${TARGET}^{commit}" 2>/dev/null || { echo "Unknown commit $TARGET"; exit 1; }
echo "$PREV" > data/deploy.prev
git -c advice.detachedHead=false checkout --quiet "$TARGET"
echo "Checked out $(git rev-parse --short HEAD) (previous $(git rev-parse --short "$PREV"))"

restore() { echo "FAILED: restoring $PREV"; git -c advice.detachedHead=false checkout --quiet "$PREV"; sudo docker compose build --quiet bot token || true; exit 1; }
sudo docker compose build --quiet bot token || restore
sudo docker compose run --rm --no-deps -T --network none -e KITE_API_KEY=test --entrypoint python bot \
  -m unittest discover -s tests -t . || restore
sudo docker compose run --rm --no-deps -T --network none --entrypoint python bot -m sensexpro.cli check-config || restore

# the weekly approvals refresh of the earlier bot no longer exists
if systemctl list-unit-files 2>/dev/null | grep -q '^sxbot-intraday-refresh.timer'; then
  sudo systemctl disable --now sxbot-intraday-refresh.timer || true
fi

sudo docker compose up -d
for _ in $(seq 1 30); do
  if curl -fsS -m 3 http://127.0.0.1:8090/health >/dev/null 2>&1; then break; fi
  sleep 2
done
bash scripts/health_check.sh || true
sudo docker image prune -f >/dev/null || true
echo "Deployed $(git rev-parse --short HEAD). Rollback: bash scripts/deploy.sh $(git rev-parse --short "$PREV")"
