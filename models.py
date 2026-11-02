"""Strict schemas for broker data entering the multi-index engine (Pydantic v2).

Every instrument row and candle is validated before it can reach strike selection, sizing or the trend state. A row
that fails validation is dropped and counted, never trusted: a malformed dump must not become a tradable contract.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Iterable, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

log = logging.getLogger(__name__)


class OptionRow(BaseModel):
    """One option contract from Kite's instruments dump (only the fields the bot uses)."""
    model_config = ConfigDict(extra="ignore", frozen=True)

    instrument_token: int = Field(gt=0)
    tradingsymbol: str = Field(min_length=3, max_length=64, pattern=r"^[A-Z0-9\-&]+$")
    name: str = Field(min_length=1, max_length=32)
    exchange: Literal["NFO", "BFO"]
    instrument_type: Literal["CE", "PE"]
    strike: float = Field(gt=0)
    expiry: dt.date
    lot_size: int = Field(gt=0, le=10_000)
    tick_size: float = Field(gt=0, le=10)


class IndexRow(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    instrument_token: int = Field(gt=0)
    tradingsymbol: str = Field(min_length=1, max_length=64)


class Candle(BaseModel):
    """One historical candle; the high/low must bracket open and close."""
    model_config = ConfigDict(extra="ignore", frozen=True)

    date: dt.datetime
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    close: float = Field(gt=0)

    @model_validator(mode="after")
    def _range(self) -> "Candle":
        if not (self.low <= min(self.open, self.close) and self.high >= max(self.open, self.close)):
            raise ValueError("high/low do not bracket open/close")
        return self


def parse_rows(model, rows: Iterable[dict], what: str) -> list:
    """Validated models for the rows that pass; the rest are dropped and logged once (with a count)."""
    out, bad, first = [], 0, None
    for r in rows:
        try:
            out.append(model.model_validate(r))
        except ValidationError as exc:
            bad += 1
            first = first or exc.errors()[0].get("msg")
    if bad:
        log.warning("%s: dropped %d invalid row(s) (first: %s)", what, bad, first)
    return out


def parse_one(model, row: dict) -> Optional[BaseModel]:
    try:
        return model.model_validate(row)
    except ValidationError:
        return None
