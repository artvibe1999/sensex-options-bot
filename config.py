"""Environment configuration: secrets and deployment paths (from .env; nothing sensitive is hardcoded).

Trading, risk and strategy parameters live in config/*.yaml (sensexpro/settings.py). The env variables kept from the
earlier bot keep their meaning: KITE_*, TELEGRAM_*, BOT_DOMAIN, LOGIN_PROMPT_TIME, PRODUCT, PAPER_CAPITAL, MAX_LOTS,
LIVE_TRADING_CONFIRM, SHEETS_WEBHOOK_URL / SHEETS_SECRET, REPORT_TOKEN / REPORT_GCS_BUCKET. MAX_RISK_PER_TRADE_PCT,
MAX_DAILY_LOSS_PCT and CHAIN_INTERVAL_S are no longer read: config/risk.yaml sets the risk limits.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from costs import CostParams

try:  # optional dependency
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass

IST = ZoneInfo("Asia/Kolkata")


def _env(name: str, default: str | None = None, *, required: bool = False) -> str | None:
    value = os.getenv(name, default)
    if required and not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a number") from exc


@dataclass(frozen=True)
class Credentials:
    api_key: str
    api_secret: str | None
    access_token: str | None   # optional manual override; normally read from the token file
    request_token: str | None  # optional one-time token from the login redirect


@dataclass(frozen=True)
class Paths:
    data_dir: str = "data"
    token_file: str = "data/token.json"

    @property
    def db(self) -> str:
        return os.path.join(self.data_dir, "sxbot.db")

    @property
    def state(self) -> str:
        return os.path.join(self.data_dir, "state.json")


@dataclass(frozen=True)
class AlertParams:
    telegram_token: str | None = None
    telegram_chat_id: str | None = None


@dataclass(frozen=True)
class Config:
    credentials: Credentials
    paths: Paths = field(default_factory=Paths)
    alerts: AlertParams = field(default_factory=AlertParams)
    costs: CostParams = field(default_factory=CostParams)
    paper_capital: float = 200_000.0
    max_lots: int = 10
    sheets_url: str | None = None             # Google Apps Script web-app URL (optional live feed)
    sheets_secret: str | None = None
    log_dir: str = "logs"


def load_config() -> Config:
    creds = Credentials(
        api_key=_env("KITE_API_KEY", required=True),  # type: ignore[arg-type]
        api_secret=_env("KITE_API_SECRET"),
        access_token=_env("KITE_ACCESS_TOKEN"),
        request_token=_env("KITE_REQUEST_TOKEN"),
    )
    data_dir = _env("DATA_DIR", "data") or "data"
    return Config(
        credentials=creds,
        paths=Paths(data_dir=data_dir,
                    token_file=_env("TOKEN_FILE", os.path.join(data_dir, "token.json"))),  # type: ignore[arg-type]
        alerts=AlertParams(_env("TELEGRAM_BOT_TOKEN"), _env("TELEGRAM_CHAT_ID")),
        paper_capital=_env_float("PAPER_CAPITAL", 200_000.0),
        max_lots=max(1, int(_env_float("MAX_LOTS", 10))),
        sheets_url=_env("SHEETS_WEBHOOK_URL"),
        sheets_secret=_env("SHEETS_SECRET"),
        log_dir=_env("LOG_DIR", "logs") or "logs",
    )
