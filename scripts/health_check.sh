#!/usr/bin/env bash
# Process, mode, data feed, database, last candle, open orders, reconciliation and kill-switch state.
#   bash scripts/health_check.sh
set -uo pipefail
DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$DIR"
echo "== containers"; sudo docker compose ps --format '{{.Name}} {{.Status}}'
echo "== commit"; git log --oneline -1
echo "== journal (mode file, kill switch, open orders/position, last candles)"
sudo docker compose exec -T bot python -m sensexpro.cli health
echo "== live status (dashboard)"
curl -fsS -m 5 http://127.0.0.1:8090/status.json | python3 -c '
import json,sys
d=json.load(sys.stdin)
print("mode", d["mode"], "| reconciled", d["reconciled"], "| kill", d["kill_switch"], "| day block", d["day_block"] or "none")
print("book", d["book"])
print("position", d["position"])
for k,v in d["indices"].items(): print(" ", k, v["regime"], "data_ok" if v["data_ok"] else "DATA NOT OK: "+str(v["why"]), v["spot"], v["last"])
' || echo "dashboard not reachable (bot not running or still starting)"
echo "== last errors"; sudo docker compose logs --since 30m bot 2>/dev/null | grep -E "ERROR|CRITICAL" | tail -5 || true
