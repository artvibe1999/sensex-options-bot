"""Typed, validated settings from config/*.yaml. Invalid configuration raises ConfigError at start-up (fail closed).

Secrets never live here: they come from the environment (.env), see config.py.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import os
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Mapping, Optional

import yaml

CONFIG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config")
KNOWN_INDICES = ("SENSEX", "NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50")
LATCH_TRIGGERS = ("trade_loss", "daily_loss", "weekly_loss", "drawdown", "broker_mismatch", "unreconciled",
                  "stale_quote", "wide_spread")


class ConfigError(ValueError):
    pass


def _time(v: Any, name: str) -> dt.time:
    try:
        h, m = str(v).split(":")
        return dt.time(int(h), int(m))
    except (ValueError, TypeError) as exc:
        raise ConfigError(f"{name}: expected HH:MM, got {v!r}") from exc


def _pos(v: Any, name: str, allow_zero: bool = False) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name}: expected a number, got {v!r}") from exc
    if x < 0 or (x == 0 and not allow_zero):
        raise ConfigError(f"{name}: must be {'>= 0' if allow_zero else '> 0'}, got {v!r}")
    return x


def _ns(d: Mapping) -> SimpleNamespace:
    """Nested dict -> attribute access (strategy parameters)."""
    return SimpleNamespace(**{k: _ns(v) if isinstance(v, Mapping) else v for k, v in d.items()})


@dataclass(frozen=True)
class Session:
    prep_time: dt.time
    market_open: dt.time
    entry_start: dt.time
    entry_end: dt.time
    square_off: dt.time
    market_close: dt.time
    report_time: dt.time


@dataclass(frozen=True)
class DataSettings:
    quote_stale_s: float
    spot_stale_s: float
    candle_fetch_delay_s: float
    warmup_days: int
    option_window_strikes: int
    record_quotes_for: tuple
    record_quotes_every_s: float
    max_gap_bars: int


@dataclass(frozen=True)
class RiskLimits:
    """Rupee limits already scaled to the account (see scaled())."""
    max_premium_per_entry: float
    emergency_loss_per_trade: float
    daily_loss_limit: float
    weekly_loss_limit: float
    max_drawdown: float
    max_entries_per_day: int
    max_open_positions: int
    max_consecutive_losses: int
    cooldown_after_exit_min: float
    emergency_feasibility_frac: float
    emergency_spread_pct: float
    emergency_spread_persist_s: float
    emergency_stale_s: float
    latch_on: frozenset
    reference_capital: float = 40000.0
    scale_with_capital: bool = True
    scale: float = 1.0

    def scaled(self, capital: float) -> "RiskLimits":
        """Rupee limits for this account size (reference values x capital / reference_capital)."""
        if not self.scale_with_capital or capital <= 0:
            return dataclasses.replace(self, scale=1.0)
        f = capital / self.reference_capital / self.scale          # self may already be scaled
        return dataclasses.replace(
            self, scale=self.scale * f,
            max_premium_per_entry=self.max_premium_per_entry * f,
            emergency_loss_per_trade=self.emergency_loss_per_trade * f,
            daily_loss_limit=self.daily_loss_limit * f, weekly_loss_limit=self.weekly_loss_limit * f,
            max_drawdown=self.max_drawdown * f)


@dataclass(frozen=True)
class PaperSim:
    entry_slippage_pct: float
    exit_slippage_pct: float
    reject_prob: float
    partial_fill_prob: float
    no_fill_prob: float
    ack_delay_s: float
    seed: Optional[int]


@dataclass(frozen=True)
class LiveExec:
    limit_buffer_pct: float
    fill_timeout_s: float
    exit_market_after_failures: int
    max_exit_attempts: int


@dataclass(frozen=True)
class ExecSettings:
    product: str
    tag_prefix: str
    paper: PaperSim
    live: LiveExec


@dataclass(frozen=True)
class Settings:
    indices: tuple
    session: Session
    data: DataSettings
    risk: RiskLimits
    execution: ExecSettings
    strategy: SimpleNamespace
    loop_interval_s: float = 1.0
    dashboard: SimpleNamespace = field(default_factory=lambda: SimpleNamespace(enabled=False, host="127.0.0.1",
                                                                                 port=8090))


def _load_yaml(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            doc = yaml.safe_load(f) or {}
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc
    if not isinstance(doc, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    return doc


def _session(d: Mapping) -> Session:
    s = Session(**{k: _time(d.get(k), f"session.{k}") for k in Session.__dataclass_fields__})
    order = [s.prep_time, s.market_open, s.entry_start, s.entry_end, s.square_off, s.market_close, s.report_time]
    if order != sorted(order):
        raise ConfigError("session times must be in order: prep < open < entry_start < entry_end < square_off < "
                          "close < report")
    return s


def _risk(d: Mapping) -> RiskLimits:
    latch = frozenset(d.get("latch_on") or ())
    bad = latch - set(LATCH_TRIGGERS)
    if bad:
        raise ConfigError(f"risk.latch_on: unknown triggers {sorted(bad)}")
    r = RiskLimits(
        max_premium_per_entry=_pos(d.get("max_premium_per_entry"), "risk.max_premium_per_entry"),
        emergency_loss_per_trade=_pos(d.get("emergency_loss_per_trade"), "risk.emergency_loss_per_trade"),
        daily_loss_limit=_pos(d.get("daily_loss_limit"), "risk.daily_loss_limit"),
        weekly_loss_limit=_pos(d.get("weekly_loss_limit"), "risk.weekly_loss_limit"),
        max_drawdown=_pos(d.get("max_drawdown"), "risk.max_drawdown"),
        max_entries_per_day=int(_pos(d.get("max_entries_per_day"), "risk.max_entries_per_day")),
        max_open_positions=int(_pos(d.get("max_open_positions"), "risk.max_open_positions")),
        max_consecutive_losses=int(_pos(d.get("max_consecutive_losses"), "risk.max_consecutive_losses")),
        cooldown_after_exit_min=_pos(d.get("cooldown_after_exit_min", 0), "risk.cooldown_after_exit_min", True),
        emergency_feasibility_frac=_pos(d.get("emergency_feasibility_frac"), "risk.emergency_feasibility_frac"),
        emergency_spread_pct=_pos(d.get("emergency_spread_pct"), "risk.emergency_spread_pct"),
        emergency_spread_persist_s=_pos(d.get("emergency_spread_persist_s", 0), "risk.emergency_spread_persist_s",
                                        True),
        emergency_stale_s=_pos(d.get("emergency_stale_s"), "risk.emergency_stale_s"),
        latch_on=latch,
        reference_capital=_pos(d.get("reference_capital", 40000), "risk.reference_capital"),
        scale_with_capital=bool(d.get("scale_with_capital", True)))
    if r.max_open_positions != 1:
        raise ConfigError("risk.max_open_positions: this release supports exactly 1 open position")
    if r.max_entries_per_day > 3:
        raise ConfigError("risk.max_entries_per_day: the brief allows at most 3")
    if not r.emergency_loss_per_trade < r.max_premium_per_entry:
        raise ConfigError("risk.emergency_loss_per_trade must be below max_premium_per_entry")
    if not r.emergency_loss_per_trade <= r.daily_loss_limit <= r.weekly_loss_limit:
        raise ConfigError("risk: need emergency_loss_per_trade <= daily_loss_limit <= weekly_loss_limit")
    if not 0 < r.emergency_feasibility_frac <= 1:
        raise ConfigError("risk.emergency_feasibility_frac must be in (0, 1]")
    return r


def _exec(d: Mapping, product: Optional[str]) -> ExecSettings:
    p, lv = d.get("paper") or {}, d.get("live") or {}
    for k in ("reject_prob", "partial_fill_prob", "no_fill_prob"):
        if not 0 <= float(p.get(k, 0)) < 1:
            raise ConfigError(f"execution.paper.{k} must be in [0, 1)")
    prod = (product or d.get("product") or "MIS").upper()
    if prod not in ("MIS", "NRML"):
        raise ConfigError("PRODUCT must be MIS or NRML")
    tag = str(d.get("tag_prefix", "SP"))
    if not tag.isalnum() or len(tag) > 4:
        raise ConfigError("execution.tag_prefix: 1-4 letters/digits")
    seed = p.get("seed")
    return ExecSettings(
        product=prod, tag_prefix=tag,
        paper=PaperSim(_pos(p.get("entry_slippage_pct", 0), "paper.entry_slippage_pct", True),
                       _pos(p.get("exit_slippage_pct", 0), "paper.exit_slippage_pct", True),
                       float(p.get("reject_prob", 0)), float(p.get("partial_fill_prob", 0)),
                       float(p.get("no_fill_prob", 0)), _pos(p.get("ack_delay_s", 0), "paper.ack_delay_s", True),
                       None if seed is None else int(seed)),
        live=LiveExec(_pos(lv.get("limit_buffer_pct", 1.0), "live.limit_buffer_pct"),
                      _pos(lv.get("fill_timeout_s", 15), "live.fill_timeout_s"),
                      int(_pos(lv.get("exit_market_after_failures", 2), "live.exit_market_after_failures")),
                      int(_pos(lv.get("max_exit_attempts", 6), "live.max_exit_attempts"))))


def _strategy(d: Mapping) -> SimpleNamespace:
    need = ("timeframes", "indicators", "regime", "orb", "vwap_continuation", "ema_pullback", "failed_breakout",
            "bollinger_squeeze", "scoring", "ensemble", "contracts", "exits")
    missing = [k for k in need if k not in d]
    if missing:
        raise ConfigError(f"strategy.yaml is missing sections: {missing}")
    sc = d["scoring"]
    if not 0 < sc["watchlist"] < sc["eligible"] <= 100:
        raise ConfigError("scoring: need 0 < watchlist < eligible <= 100")
    ex = d["exits"]
    if sum(ex["weights"].values()) != 100:
        raise ConfigError("exits.weights must add up to 100")
    if not 0 < ex["hold_below"] < ex["reassess_below"] < ex["prepare_below"] <= 100:
        raise ConfigError("exits: need 0 < hold_below < reassess_below < prepare_below <= 100")
    if not 0 < float(ex["missing_input_share"]) <= 1:
        raise ConfigError("exits.missing_input_share must be in (0, 1]: a missing input may never score as favourable")
    if d["timeframes"]["signal"] != 5 or d["timeframes"]["trend"] != 15:
        raise ConfigError("timeframes: this release supports signal 5 and trend 15 minutes")
    c = d["contracts"]
    if any(abs(int(s)) > 1 for s in c["candidate_steps"]):
        raise ConfigError("contracts.candidate_steps: never farther than one strike from ATM")
    return _ns(d)


def load_settings(config_dir: str = CONFIG_DIR, env: Optional[Mapping[str, str]] = None) -> Settings:
    env = os.environ if env is None else env
    app = _load_yaml(os.path.join(config_dir, "app.yaml"))
    idx = tuple(app.get("indices") or ())
    if not idx or any(i not in KNOWN_INDICES for i in idx):
        raise ConfigError(f"app.indices must be a non-empty subset of {KNOWN_INDICES}")
    dd = app.get("data") or {}
    data = DataSettings(
        quote_stale_s=_pos(dd.get("quote_stale_s"), "data.quote_stale_s"),
        spot_stale_s=_pos(dd.get("spot_stale_s"), "data.spot_stale_s"),
        candle_fetch_delay_s=_pos(dd.get("candle_fetch_delay_s", 4), "data.candle_fetch_delay_s"),
        warmup_days=int(_pos(dd.get("warmup_days", 6), "data.warmup_days")),
        option_window_strikes=int(_pos(dd.get("option_window_strikes", 6), "data.option_window_strikes")),
        record_quotes_for=tuple(dd.get("record_quotes_for") or ()),
        record_quotes_every_s=_pos(dd.get("record_quotes_every_s", 1), "data.record_quotes_every_s"),
        max_gap_bars=int(_pos(dd.get("max_gap_bars", 0), "data.max_gap_bars", True)))
    return Settings(
        indices=idx, session=_session(app.get("session") or {}), data=data,
        risk=_risk(_load_yaml(os.path.join(config_dir, "risk.yaml"))),
        execution=_exec(_load_yaml(os.path.join(config_dir, "execution.yaml")), env.get("PRODUCT")),
        strategy=_strategy(_load_yaml(os.path.join(config_dir, "strategy.yaml"))),
        loop_interval_s=_pos(app.get("loop_interval_s", 1.0), "loop_interval_s"),
        dashboard=_ns(app.get("dashboard") or {"enabled": False, "host": "127.0.0.1", "port": 8090}))
