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
- Logs: `docker compose logs -f bot`. Data: `./data` volume (back up `sxbot.db`).
- Unknown positions in BFO halt the bot (`allow_foreign_positions=False`).
- Kill switch: max daily loss; or `docker compose stop bot` (then square off manually).

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
Every 5 minutes between 15:40 and 19:00 IST it checks `data/state.json`. No open position: containers are stopped
cleanly and the VM powers off. Open position (or unreadable state file): it does NOT stop, and sends one Telegram alert.
Keep the VM up on a given day: `touch data/NO_AUTOSTOP` (delete the file to resume). Logs: `journalctl -u sxbot-autostop -n 20`.
It only reads the bot's own record; with live money also check the broker's positions page if you get the alert.
