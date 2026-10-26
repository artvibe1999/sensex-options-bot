"""Configuration for the SENSEX options bot.

Secrets come from environment variables (optionally a local .env file);
nothing sensitive is hardcoded. Numeric risk/strategy values are PLACEHOLDERS
until validated by ``backtest.py`` and forward-tested in paper mode.
"""
from __future__ import annotations

import datetime as dt

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
    # Rupee rules on the COMBINED P&L of the open multi-leg position (0 = off):
    #  - trail_min_profit_rs: trailing profit stop only arms once combined profit reaches this, and it never
    #    exits below it (floor = max(peak x (1 - giveback), this)); the day-level trailing stop obeys it too.
    #  - position_stop_rs: combined stop loss; applied as a CEILING, so a variant's tighter stop still wins.
    trail_min_profit_rs: float = 2000.0
    position_stop_rs: float = 2000.0
    # Small accounts: one SENSEX lot can risk more than max_risk_per_trade_pct of capital. If
    # one_lot_risk_pct > 0 a plan whose worst-case loss for ONE lot is <= this share of capital
    # may still trade 1 lot. 0 = off (standard accounts never exceed max_risk_per_trade_pct).
    one_lot_risk_pct: float = 0.0


# Capital profiles. A SENSEX lot cannot be split, so on a small account even the narrowest defined-risk
# spread can risk more than the standard 2%-per-trade budget. Below SMALL_ACCOUNT_BELOW the bot may trade ONE lot
# whose worst-case loss is at most min(15% of capital, Rs 7,500). The daily loss limit is widened to match, so a
# normal stop-loss (60% of max loss) is not pre-empted. Standard accounts are unchanged.
SMALL_ACCOUNT_BELOW = 375_000.0
ONE_LOT_MAX_PCT = 0.15
ONE_LOT_MAX_RUPEES = 7_500.0


# Capital tiers: backtests and approvals are evaluated at a representative account size per tier.
TIER_CAPITAL = {"small": 15_000.0, "medium": 100_000.0, "large": 500_000.0}


def capital_tier(capital: float) -> str:
    """small below Rs 30k, medium below Rs 3L, otherwise large."""
    return "small" if capital < 30_000 else ("medium" if capital < 300_000 else "large")


def risk_for_capital(base: RiskParams, capital: float, profile: str = "auto") -> RiskParams:
    """Return the risk parameters to use for this account size. profile: auto | standard | small."""
    import dataclasses
    small = profile == "small" or (profile == "auto" and capital < SMALL_ACCOUNT_BELOW)
    if not small or capital <= 0:
        return base
    one_lot = max(base.one_lot_risk_pct, min(ONE_LOT_MAX_PCT, ONE_LOT_MAX_RUPEES / capital))
    return dataclasses.replace(
        base, one_lot_risk_pct=one_lot,
        max_daily_loss_pct=max(base.max_daily_loss_pct, 0.8 * one_lot),
        margin_utilisation=max(base.margin_utilisation, 0.80))


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
    stream_min_coverage: float = 0.8          # share of the window that must have ticked to use the stream
    stream_near_age_s: float = 60.0           # strikes around ATM must have ticked this recently
    rest_min_gap_s: float = 2.0               # min gap between REST quote calls (Kite: ~1 req/s)
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
    expiry_weekday: int = -1        # -1 = follow `expiry_schedule` (the real history); 0-6 forces one weekday (0=Mon)
    # SENSEX weekly expiry weekday by effective date: Friday from May 2023, Tuesday from 1 Jan 2025,
    # Thursday from 1 Sep 2025 (BSE circulars / press reports). VERIFY against the instruments dump.
    expiry_schedule: tuple = ((dt.date(2023, 5, 1), 4), (dt.date(2025, 1, 1), 1), (dt.date(2025, 9, 1), 3))
    stop_overshoot: float = 0.10    # a stop fills this fraction worse than its trigger (capped at max loss)
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
    market_exit_after_failures: int = 3       # exits switch to MARKET orders after this many failed attempts
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
    paper_explore: bool = True                # paper only: also trade the scanner's best setups (see explorer.py)
    explore_min_score: float = 60.0
    explore_max_trades: int = 4               # paper exploration trades per day (separate from the approved-variant cap)
    sheets_url: str | None = None             # Google Apps Script web-app URL (optional live feed)
    sheets_secret: str | None = None
    loop_interval_s: float = 1.0              # decision cadence (seconds)
    chain_interval_s: float = 1.0             # option-chain rebuild cadence (stream-built)
    chain_record_every_s: float = 10.0        # full chain rows stored this often (every second would be ~3.6M rows/day)
    flow_record_every_s: float = 1.0          # ATM+/-flow_strikes rows (ltp, oi, volume, bid, ask) stored this often
    flow_strikes: int = 5
    stream_chain: bool = True                 # False: REST polling only (then use CHAIN_INTERVAL_S >= 2)
    flow_retention_days: int = 30             # flow rows older than this are deleted at start of day (0 = keep)
    capital_profile: str = "auto"            # auto | standard | small (see risk_for_capital)
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
            trail_min_profit_rs=_env_float("TRAIL_MIN_PROFIT_RS", 2000.0),
            position_stop_rs=_env_float("POSITION_STOP_RS", 2000.0),
        ),
        backtest=BacktestParams(iv_scale=_env_float("BT_IV_SCALE", 0.95),
                                lot_size=int(_env_float("BT_LOT_SIZE", 20)),
                                strike_step=_env_float("BT_STRIKE_STEP", 100.0),
                                slippage_pct=_env_float("BT_SLIPPAGE_PCT", 0.01)),
        execution=ExecutionParams(product=product),
        paper_capital=_env_float("PAPER_CAPITAL", 500_000.0),
        paper_explore=(_env("PAPER_EXPLORE", "1") or "1") != "0",
        explore_min_score=_env_float("EXPLORE_MIN_SCORE", 60.0),
        explore_max_trades=int(_env_float("EXPLORE_MAX_TRADES", 4)),
        sheets_url=_env("SHEETS_WEBHOOK_URL"),
        sheets_secret=_env("SHEETS_SECRET"),
        loop_interval_s=_env_float("LOOP_INTERVAL_S", 1.0),
        chain_interval_s=_env_float("CHAIN_INTERVAL_S", 1.0),
        chain_record_every_s=_env_float("CHAIN_RECORD_EVERY_S", 10.0),
        flow_record_every_s=_env_float("FLOW_RECORD_EVERY_S", 1.0),
        flow_strikes=int(_env_float("FLOW_STRIKES", 5)),
        stream_chain=(_env("STREAM_CHAIN", "1") or "1") != "0",
        flow_retention_days=int(_env_float("FLOW_RETENTION_DAYS", 30)),
        capital_profile=(_env("CAPITAL_PROFILE", "auto") or "auto").lower(),
        log_dir=_env("LOG_DIR", "logs") or "logs",
    )
