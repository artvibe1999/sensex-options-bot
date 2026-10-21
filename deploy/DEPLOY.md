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
4. `docker compose up -d --build` (services: bot, token, caddy with automatic HTTPS).
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
