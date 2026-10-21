"""Daily Kite login helper: one tap on your phone, token lands where the bot reads it.

Zerodha requires a manual login (password + TOTP) once a day and does not
support automating it, so this does NOT log in for you. It removes the
friction instead:

  1. Every weekday from LOGIN_PROMPT_TIME (default 08:30 IST) it sends the Kite
     login link to your Telegram until a fresh token exists.
  2. You tap it and log in on Zerodha's own page (password + TOTP).
  3. Zerodha redirects to https://<your-domain>/kite/callback, which this
     service receives, exchanges for an access token (using your API secret,
     which never leaves the server) and writes to the token file (chmod 600).
  4. The bot, waiting for a session, picks the token up within ~30 seconds.

Set the redirect URL of your Kite Connect app to that callback URL.

Commands:
  python token_service.py serve                          # callback server + daily Telegram prompts
  python token_service.py url                            # print a login link (manual use)
  python token_service.py exchange --request-token XXXX  # no public endpoint? paste request_token from the redirect URL
  python token_service.py status
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import hmac
import logging
import os
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import parse_qs, quote, urlparse

from kiteconnect import KiteConnect

from alerts import Alerter
from config import IST, Config, load_config
from token_store import is_fresh, read_token, write_token

log = logging.getLogger("token_service")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9]{10,64}$")


class TokenService:
    def __init__(self, cfg: Config) -> None:
        if not cfg.credentials.api_secret:
            raise SystemExit("KITE_API_SECRET is required for the token service")
        self.cfg = cfg
        self.kite = KiteConnect(api_key=cfg.credentials.api_key)
        self.alerts = Alerter(cfg.alerts.telegram_token, cfg.alerts.telegram_chat_id, dedupe_s=0)
        self._lock = threading.Lock()
        self._last_rt: Optional[str] = None      # request_token already exchanged (guards browser reloads)
        self._last_user = ""

    # Login "state": stateless and self-verifying (HMAC with the API secret + timestamp), so a link
    # made by `token_service.py url` (another process) is accepted by the running server without
    # any shared file. Valid for 30 minutes. Replay is harmless on its own: the callback is useless
    # without a fresh single-use request_token that only Zerodha can issue.
    STATE_TTL_S = 1800

    def _sign(self, body: str) -> str:
        key = (self.cfg.credentials.api_secret or "").encode()
        return hmac.new(key, body.encode(), hashlib.sha256).hexdigest()[:24]

    def make_state(self, now: Optional[float] = None) -> str:
        body = f"{int(now if now is not None else time.time())}x{secrets.token_hex(4)}"
        return f"{body}x{self._sign(body)}"

    def check_state(self, state: Optional[str], now: Optional[float] = None) -> bool:
        if not state or state.count("x") != 2:
            return False
        ts_s, rnd, sig = state.split("x")
        if not (ts_s.isdigit() and rnd and sig):
            return False
        if not hmac.compare_digest(sig, self._sign(f"{ts_s}x{rnd}")):
            return False
        age = (now if now is not None else time.time()) - int(ts_s)
        return -60 <= age <= self.STATE_TTL_S

    def login_url(self) -> str:
        return f"{self.kite.login_url()}&redirect_params={quote('state=' + self.make_state())}"

    def fresh(self) -> bool:
        return is_fresh(read_token(self.cfg.paths.token_file))

    def exchange(self, request_token: str, state: Optional[str], check_state: bool = True) -> str:
        if not _TOKEN_RE.match(request_token):
            raise ValueError("malformed request_token")
        if check_state:
            if not self.check_state(state):
                raise PermissionError("login state invalid or older than 30 minutes: "
                                      "generate a fresh link and open it")
        if request_token == self._last_rt and self.fresh():
            return self._last_user                      # reload/replay of a login that already worked
        data = self.kite.generate_session(request_token, api_secret=self.cfg.credentials.api_secret)
        self._last_rt, self._last_user = request_token, data.get("user_id", "")
        write_token(self.cfg.paths.token_file, data["access_token"], data.get("user_id", ""))
        self.alerts.send("Kite login complete: the bot will start its session shortly.")
        return data.get("user_id", "")

    # ---------------------------------------------------------------- prompts
    def prompt_loop(self, prompt_time: dt.time, every_s: int = 900) -> None:
        last = 0.0
        while True:
            now = dt.datetime.now(IST)
            due = (now.weekday() < 5 and prompt_time <= now.time() <= dt.time(15, 0)
                   and not self.fresh() and time.monotonic() - last >= every_s)
            if due:
                last = time.monotonic()
                self.alerts.send(f"Tap to log in to Kite for today's trading session:\n{self.login_url()}")
            time.sleep(30)

    # ----------------------------------------------------------------- server
    def serve(self, host: str, port: int, prompt_time: dt.time) -> None:
        svc = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):      # never log the query string (holds request_token)
                log.info("%s %s", self.command, urlparse(self.path).path)

            def _reply(self, code: int, body: str) -> None:
                data = body.encode()
                self.send_response(code)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                u = urlparse(self.path)
                if u.path == "/health":
                    return self._reply(200, "token fresh" if svc.fresh() else "token missing or expired")
                if u.path != "/kite/callback":
                    return self._reply(404, "not found")
                q = parse_qs(u.query)
                if q.get("status", [""])[0] != "success" or "request_token" not in q:
                    return self._reply(400, "Login was not successful. Open the link again.")
                try:
                    user = svc.exchange(q["request_token"][0], q.get("state", [None])[0])
                except (ValueError, PermissionError) as exc:
                    if isinstance(exc, PermissionError) and svc.fresh():
                        # Typically a browser reload/replay of a link that already worked.
                        log.info("Repeated callback ignored: a fresh token already exists")
                        return self._reply(200, "Already logged in today (this link was already used). "
                                                "You can close this tab.")
                    log.warning("Rejected callback: %s (state_in_url=%s)", exc, "state" in q)
                    return self._reply(403, f"Rejected: {exc}")
                except Exception:  # noqa: BLE001
                    log.exception("Token exchange failed")
                    svc.alerts.send("Kite token exchange FAILED; check token_service logs", level="ERROR")
                    return self._reply(500, "Token exchange failed. See server logs.")
                self._reply(200, f"Logged in{' as ' + user if user else ''}. You can close this tab.")

        threading.Thread(target=self.prompt_loop, args=(prompt_time,), daemon=True).start()
        log.info("Token service listening on %s:%d", host, port)
        ThreadingHTTPServer((host, port), Handler).serve_forever()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve")
    sub.add_parser("url")
    sub.add_parser("status")
    ex = sub.add_parser("exchange")
    ex.add_argument("--request-token", required=True)
    args = ap.parse_args()

    cfg = load_config()
    if args.cmd == "status":
        rec = read_token(cfg.paths.token_file)
        print("fresh" if is_fresh(rec) else "missing or expired",
              f"(issued {rec['issued_at']})" if rec else "")
        return 0
    svc = TokenService(cfg)
    if args.cmd == "url":
        print(svc.login_url())
    elif args.cmd == "exchange":
        user = svc.exchange(args.request_token, None, check_state=False)
        print(f"Token saved to {cfg.paths.token_file}" + (f" for {user}" if user else ""))
    else:
        hh, mm = (os.getenv("LOGIN_PROMPT_TIME", "08:30")).split(":")
        svc.serve(os.getenv("TOKEN_SERVICE_HOST", "127.0.0.1"), int(os.getenv("TOKEN_SERVICE_PORT", "8765")),
                  dt.time(int(hh), int(mm)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
