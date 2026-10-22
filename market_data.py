"""Market snapshot assembly: spot, VIX, regime, trend and option chain.

Fails closed: if daily history is unavailable (e.g. the Kite historical-data
add-on is missing and there is no cache), no option chain is produced and
therefore no trade is taken.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Optional

from chain_features import ChainFeatures, FeatureTracker
from config import IST, StrategyParams
from strategy_engine import Chain, RegimeDetector, RegimeState, StrategyEngine, trend_direction
from zerodha_client import ZerodhaClient

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Snapshot:
    ts: dt.datetime
    spot: float
    vix: float
    rs: RegimeState
    chain: Optional[Chain]
    trend_dir: Optional[int]
    fresh: bool          # True when the chain was rebuilt for this snapshot
    features: Optional[ChainFeatures] = None   # OI / gamma / skew features of the latest chain


class MarketData:
    def __init__(self, client: ZerodhaClient, engine: StrategyEngine, detector: RegimeDetector,
                 params: StrategyParams, cache_path: str, chain_interval_s: float) -> None:
        self.client, self.engine, self.detector = client, engine, detector
        self.p, self.cache_path, self.chain_interval_s = params, cache_path, chain_interval_s
        self._daily: list[dict] = []
        self._daily_day: Optional[dt.date] = None
        self._daily_retry_at = 0.0
        self._hist5: list[float] = []
        self._hist5_at = -1e9
        self._bars: "OrderedDict[int, float]" = OrderedDict()
        self._chain: Optional[Chain] = None
        self._chain_at = -1e9
        self.tracker = FeatureTracker()
        self._feat: Optional[ChainFeatures] = None

    def reset_day(self) -> None:
        self._chain, self._chain_at = None, -1e9
        self._feat = None
        self.tracker.reset()
        self._bars.clear()
        self._daily_day = None

    # ------------------------------------------------------------ daily bars
    def _save_cache(self, candles: list[dict]) -> None:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.cache_path)), exist_ok=True)
            with open(self.cache_path, "w", encoding="utf-8") as f:
                json.dump([{**c, "date": c["date"].isoformat()} for c in candles], f)
        except OSError:
            log.warning("Could not write daily cache", exc_info=True)

    def _load_cache(self) -> list[dict]:
        try:
            with open(self.cache_path, encoding="utf-8") as f:
                return [{**c, "date": dt.datetime.fromisoformat(c["date"])} for c in json.load(f)]
        except (OSError, ValueError, KeyError):
            return []

    def daily_candles(self, now: dt.datetime) -> list[dict]:
        if self._daily_day == now.date() and (self._daily or time.monotonic() < self._daily_retry_at):
            return self._daily
        try:
            c = self.client.historical(self.client.spot_token, now - dt.timedelta(days=90), now, "day")
            c = [x for x in c if x["date"].date() < now.date()]      # completed days only
            if c:
                self._daily, self._daily_day = c, now.date()
                self._save_cache(c)
                return c
        except Exception as exc:  # noqa: BLE001
            log.warning("Daily history fetch failed (%s); using cache", exc)
        self._daily, self._daily_day = self._load_cache(), now.date()
        self._daily_retry_at = time.monotonic() + 600
        if not self._daily:
            log.error("No daily history available: trading disabled until it is")
        return self._daily

    # --------------------------------------------------------------- 5m closes
    def _note_spot(self, now: dt.datetime, spot: float) -> None:
        bucket = int(now.timestamp() // 300)
        self._bars[bucket] = spot
        while len(self._bars) > 80:
            self._bars.popitem(last=False)

    def closes_5m(self, now: dt.datetime) -> list[float]:
        if time.monotonic() - self._hist5_at > 300:
            self._hist5_at = time.monotonic()
            try:
                c = self.client.historical(self.client.spot_token, now - dt.timedelta(days=5), now, "5minute")
                self._hist5 = [x["close"] for x in c]
            except Exception as exc:  # noqa: BLE001
                log.warning("5-minute history unavailable (%s); using own bars", exc)
                self._hist5 = []
        return self._hist5 if self._hist5 else list(self._bars.values())

    # ---------------------------------------------------------------- snapshot
    def snapshot(self, now: dt.datetime) -> Snapshot:
        spot = self.client.get_ltp(self.client.spot_token)
        vix = self.client.get_ltp(self.client.vix_token)
        self._note_spot(now, spot)
        daily = self.daily_candles(now)
        rs = self.detector.update(vix, daily)
        if not daily:
            return Snapshot(now, spot, vix, rs, None, None, False)
        expiry = self.engine.pick_expiry(now.date())
        fresh = False
        if expiry is None:
            self._chain = None
        elif (self._chain is None or self._chain.expiry != expiry
              or time.monotonic() - self._chain_at >= self.chain_interval_s - 0.05):
            chain = self.engine.build_chain(expiry, spot, now)
            if chain is not None:
                self._chain, self._chain_at, fresh = chain, time.monotonic(), True
                try:
                    self._feat = self.tracker.update(time.monotonic(), chain)
                except Exception:  # noqa: BLE001 - features are optional; never block price data
                    log.exception("Feature computation failed")
                    self._feat = None
        trend = trend_direction(self.closes_5m(now), self.p)
        return Snapshot(now, spot, vix, rs, self._chain, trend, fresh,
                        self._feat if self._chain is not None else None)
