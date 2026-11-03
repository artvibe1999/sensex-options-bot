# Deploying

## Where: a small VM with a static IP (NOT Vercel)
Since 1 Apr 2026 Kite requires order-placing API calls to come from a registered static IP
(developer console -> Profile -> IP Whitelist; 2 IPs, one change per week). Vercel/Lambda-style serverless
has dynamic IPs and no long-running process, so it cannot host a WebSocket bot. Use e.g. an AWS Lightsail /
EC2 (Mumbai, Elastic IP), DigitalOcean (Bangalore, reserved IP) or any VPS with a fixed IPv4. 1 vCPU / 1-2 GB is enough.

## Steps
1. Create VM, attach static IP, register it in the Kite developer console.
2. Point a DNS A record (`BOT_DOMAIN`) at that IP. In the Kite app set Redirect URL to
   `https://<BOT_DOMAIN>/kite/callback`.
3. `git clone`/copy the project, `cp .env.example .env`, fill it in (keep `LIVE_TRADING_CONFIRM=NO`).
4. `mkdir -p data logs && sudo chown -R 10001:10001 data logs` (the containers run as user 10001),
   then `docker compose up -d --build` (services: bot, token, caddy with automatic HTTPS).
5. Run paper mode for 4+ weeks before any live money.

## Daily access token
Kite tokens expire ~06:00 IST and login needs password + TOTP, so it cannot be fully automated
(Zerodha does not support automating it). One-tap flow:
1. ~08:30 IST `token_service` sends a Telegram message with your login link (repeats every 15 min until done).
2. Tap, log in on Zerodha. Kite redirects to `/kite/callback`.
3. The service exchanges the request token, writes `data/token.json` (chmod 600); the bot picks it up
   and starts at 09:00. No restart, no copy-paste.
If you skip it, the bot stays idle and alerts you; it never trades without a fresh token.

Manual fallback: `docker compose exec token python token_service.py url`, log in, then
`python token_service.py exchange --request-token <request_token>`; `status` shows freshness.

## Operations
- Logs: `docker compose logs -f bot` (text) and `logs/sensexpro.jsonl` (JSON). Data: `./data` (journal `sxbot.db`,
  daily copy `sxbot-backup.db`, `token.json`, `settings.json`, `mode.json`, `state.json`).
- Health: `bash scripts/health_check.sh`; dashboard over an SSH tunnel
  (`gcloud compute ssh sxbot --zone asia-south1-a -- -L 8090:127.0.0.1:8090`, then http://localhost:8090).
- Kill switch: Telegram `/kill`; reset with `/resetkill` + `/confirm` (or
  `docker compose exec bot python -m sensexpro.cli reset-kill --mode PAPER` and restart). Breakers latch it by themselves.
- In LIVE, broker positions that differ from the journal latch the kill switch at start-up and during a position.

## Optional: live orders in a Google Sheet
Tabs created automatically: **Orders** (every leg order with time, side, qty, LTP and fill price),
**Trades** (one row per completed trade with P&L) and **Status** (session start / day summary).
1. Create a Google Sheet. Extensions -> Apps Script -> paste `deploy/google_sheets_script.gs`.
   Change `SECRET` to a long random string.
2. Deploy -> New deployment -> Web app -> Execute as: Me, Who has access: Anyone -> Deploy
   (authorise when asked). Copy the web app URL (ends in `/exec`).
3. On the VM edit `.env`: `SHEETS_WEBHOOK_URL=<that URL>` and `SHEETS_SECRET=<same secret>`, then
   `docker compose up -d`. Test the link: `docker compose exec bot python sheets_sink.py`
   (writes a row to a `Status` tab and explains any problem). Real tabs appear when the bot sends its first row.
Rows are sent in the background, so a Sheets outage never affects trading. In paper mode the Orders tab
shows the simulated fills (LTP plus modelled slippage). The 164-variant shadow book is not sent here:
it stays in SQLite.

## Daily VM start / automatic stop (works for paper and live)
Start: a GCP instance schedule (start only) boots the VM at 08:00 IST Mon-Fri; the containers restart by themselves.
```bash
gcloud compute resource-policies create instance-schedule sxbot-start --region=asia-south1 \
  --vm-start-schedule="0 8 * * 1-5" --timezone="Asia/Kolkata"
gcloud compute instances add-resource-policies sxbot --zone=asia-south1-a --resource-policies=sxbot-start
```
Stop: do NOT use a scheduled stop (it would kill the bot even with a position open). Install the guard instead:
```bash
cd ~/sxbot && git pull && sudo bash deploy/install_autostop.sh
```
Every 5 minutes between 16:30 and 17:30 IST it checks `data/state.json`. No open position: containers are stopped
and the VM powers off (a plain OS shutdown; never `docker compose stop`, which would stop them coming back at boot). Open position (or unreadable state file): it does NOT stop, and sends one Telegram alert.
Keep the VM up on a given day: `touch data/NO_AUTOSTOP` (delete the file to resume). Logs: `journalctl -u sxbot-autostop -n 20`.
It only reads the bot's own record; with live money also check the broker's positions page if you get the alert.

`install_autostop.sh` also installs `sxbot-compose.service`, which runs `docker compose up -d` on every boot, so the bot, login service and web server always start with the VM.

## Deploying SENSEX PRO on the existing VM `sxbot`
Inspected read-only on 3 Nov 2026: project `eastern-augury-420203`, zone `asia-south1-a`, `e2-small` (2 vCPU shared,
2 GB), Debian 12, 40 GB disk (19 % used), service account `sxbot-report@eastern-augury-420203.iam.gserviceaccount.com`,
network tag `https-server`, start schedule `sxbot-start`, SSH user `amazoncentres`, app in `~/sxbot` (git checkout,
`docker compose`: bot / token / caddy), Docker 29.9. Timers: `sxbot-autostop.timer`, `sxbot-intraday-refresh.timer`
(the latter belongs to the earlier bot; `scripts/deploy.sh` disables it). No new VM, firewall or IAM change is needed:
the dashboard is published on the host loopback only.

Hardening already in place / recommended:
- containers run as uid 10001 (non-root); the code in the image is root-owned and read-only to the bot;
- `.env` lives outside git; **recommended**: `chmod 600 ~/sxbot/.env` (it is 644 today) and move the `.env.bak*` /
  `.env.save` copies out of the app directory;
- restart policy `unless-stopped`; Docker health check on the dashboard; journald via the Docker logging driver;
- the engine starts PAPER, checks the instrument master, broker login, kill switch and reconciles before any entry.

### Deploy a reviewed commit
```bash
cd ~/sxbot
git fetch origin
bash scripts/deploy.sh <commit-sha>      # builds, runs the 122 tests inside the image, then restarts; refuses
                                         # during market hours while a position is open
bash scripts/health_check.sh
```
The repo is public, so `git fetch` works on the VM. If it becomes private again (no GitHub credentials on the VM):
`git bundle create sp.bundle <branch>` locally, `gcloud compute scp sp.bundle sxbot:~/` and `git fetch ~/sp.bundle
<branch>` before `deploy.sh`.

### Rollback
`bash scripts/deploy.sh "$(cat data/deploy.prev)"` returns to the commit that ran before the last deploy (the journal,
token, settings and `.env` are never touched by a deploy). The earlier engine's last commit on `main` before SENSEX PRO
is `f625b14`. The new `sp_*` tables are additive, so the old engine runs on the same database.

### Paper -> live (only after review)
1. At least 30 sessions of paper operation; review the Trades sheet, the report page, `sp_signals`, slippage and
   recovery behaviour.
2. Sign off the live-readiness checklist (Zerodha API terms, static IP registration for orders, order tags, rate
   limits, capital).
3. Set `LIVE_TRADING_CONFIRM=YES` in `.env`, `docker compose up -d`, then Telegram `/mode live` + `/confirm CODE`.
   `/mode paper` switches back.
