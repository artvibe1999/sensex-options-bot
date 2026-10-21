"""Configuration for the SENSEX options bot.

Secrets come from environment variables (optionally a local .env file);
nothing sensitive is hardcoded. Numeric risk/strategy values are PLACEHOLDERS
until validated by ``backtest.py`` and forward-tested in paper mode.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import time
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
    return float(os.getenv(name, str(default)))


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

    @property
    def approved_backtest(self) -> str:
        return os.path.join(self.data_dir, "approved_backtest.json")

    @property
    def approved_live(self) -> str:
        return os.path.join(self.data_dir, "approved_live.json")

    @property
    def daily_cache(self) -> str:
        return os.path.join(self.data_dir, "daily_cache.json")


@dataclass(frozen=True)
class AlertParams:
    telegram_token: str | None = None
    telegram_chat_id: str | None = None


@dataclass(frozen=True)
class RiskParams:
    max_risk_per_trade_pct: float = 0.02      # worst-case loss per trade / capital
    max_daily_loss_pct: float = 0.03          # portfolio kill switch
    max_lots: int = 10
    margin_utilisation: float = 0.60          # max share of capital used as margin
    max_trades_per_day: int = 2
    stop_loss_pct_of_max_loss: float = 0.60   # default hard stop (variants may override)
    take_profit_pct_of_max_profit: float = 0.60
    trail_activation_pct_of_max_profit: float = 0.30
    trail_giveback_pct: float = 0.40          # exit after giving back 40% of peak
    portfolio_trail_activation_pct: float = 0.015
    portfolio_trail_giveback_pct: float = 0.50


@dataclass(frozen=True)
class StrategyParams:
    risk_free_rate: float = 0.065
    # Regime thresholds (VIX and realised vol in annualised %, ATR in % of spot)
    vix_low: float = 14.0
    vix_high: float = 18.0
    atr_pct_low: float = 0.60
    atr_pct_high: float = 1.10
    rv_low: float = 10.0
    rv_high: float = 16.0
    vix_spike_pct: float = 0.10               # +10% in 15 min blocks new entries
    # Expiry selection (calendar days to expiry); 0-DTE is deliberately excluded
    min_dte: int = 1
    max_dte: int = 7
    chain_window_strikes: int = 40            # strikes each side of ATM to quote
    ema_fast: int = 9
    ema_slow: int = 21
    trend_buffer_pct: float = 0.0005


@dataclass(frozen=True)
class BacktestParams:
    """Option-pricing model used when real historical option prices are unavailable.

    Kite does not serve historical prices for expired option contracts, so the
    backtester prices options with Black-Scholes from index + India VIX data.
    These are APPROXIMATIONS: calibrate iv_scale/skew_slope from the data the
    bot records (table ``chain``) once you have a few weeks of it.
    """
    strike_step: float = 100.0      # VERIFY against the instruments dump
    lot_size: int = 20              # VERIFY (the live bot reads it from instruments)
    expiry_weekday: int = 3         # 0=Mon. Thursday is ASSUMED for SENSEX weekly: VERIFY
    iv_scale: float = 0.95          # option ATM IV ~= India VIX * scale. This sets the variance risk
                                    # premium the model assumes (VIX is NIFTY-based): CALIBRATE it from
                                    # recorded chain data (table `chain`) before trusting results
    skew_slope: float = 1.5         # put-side skew steepness
    slippage_pct: float = 0.01      # per-fill slippage as % of premium
    window: int = 40                # strikes each side of ATM in the model chain


@dataclass(frozen=True)
class ExecutionParams:
    product: str = "MIS"                      # "MIS" or "NRML"
    limit_buffer_pct: float = 0.01            # marketable-limit offset from LTP
    max_exit_buffer_pct: float = 0.08         # cap when escalating failed exits
    fill_timeout_s: float = 15.0
    tag: str = "SXALGO"                       # Kite tag, max 20 chars
    paper_slippage_pct: float = 0.005


@dataclass(frozen=True)
class Schedule:
    prep_time: time = time(9, 0)              # authenticate, load instruments, start stream
    market_open: time = time(9, 15)
    entry_start: time = time(9, 30)
    entry_end: time = time(14, 30)
    square_off: time = time(15, 15)
    market_close: time = time(15, 30)


@dataclass(frozen=True)
class Config:
    credentials: Credentials
    paths: Paths = field(default_factory=Paths)
    alerts: AlertParams = field(default_factory=AlertParams)
    risk: RiskParams = field(default_factory=RiskParams)
    strategy: StrategyParams = field(default_factory=StrategyParams)
    backtest: BacktestParams = field(default_factory=BacktestParams)
    costs: CostParams = field(default_factory=CostParams)
    execution: ExecutionParams = field(default_factory=ExecutionParams)
    schedule: Schedule = field(default_factory=Schedule)
    paper_capital: float = 500_000.0
    sheets_url: str | None = None             # Google Apps Script web-app URL (optional live feed)
    sheets_secret: str | None = None
    loop_interval_s: float = 3.0
    chain_interval_s: float = 10.0            # option-chain refresh + recording cadence
    allow_foreign_positions: bool = False     # False: halt if unknown BFO positions exist
    log_dir: str = "logs"


def load_config() -> Config:
    creds = Credentials(
        api_key=_env("KITE_API_KEY", required=True),  # type: ignore[arg-type]
        api_secret=_env("KITE_API_SECRET"),
        access_token=_env("KITE_ACCESS_TOKEN"),
        request_token=_env("KITE_REQUEST_TOKEN"),
    )
    product = (_env("PRODUCT", "MIS") or "MIS").upper()
    if product not in ("MIS", "NRML"):
        raise RuntimeError("PRODUCT must be MIS or NRML")
    data_dir = _env("DATA_DIR", "data") or "data"
    return Config(
        credentials=creds,
        paths=Paths(data_dir=data_dir,
                    token_file=_env("TOKEN_FILE", os.path.join(data_dir, "token.json"))  # type: ignore[arg-type]
                    ),
        alerts=AlertParams(_env("TELEGRAM_BOT_TOKEN"), _env("TELEGRAM_CHAT_ID")),
        risk=RiskParams(
            max_risk_per_trade_pct=_env_float("MAX_RISK_PER_TRADE_PCT", 0.02),
            max_daily_loss_pct=_env_float("MAX_DAILY_LOSS_PCT", 0.03),
            max_lots=int(_env_float("MAX_LOTS", 10)),
        ),
        execution=ExecutionParams(product=product),
        paper_capital=_env_float("PAPER_CAPITAL", 500_000.0),
        sheets_url=_env("SHEETS_WEBHOOK_URL"),
        sheets_secret=_env("SHEETS_SECRET"),
        chain_interval_s=_env_float("CHAIN_INTERVAL_S", 10.0),
        log_dir=_env("LOG_DIR", "logs") or "logs",
    )
