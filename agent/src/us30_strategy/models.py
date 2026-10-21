"""Plain data types shared by the US30 strategy engine and backtester."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

ONE_MINUTE = timedelta(minutes=1)


@dataclass(frozen=True)
class Bar:
    """One closed 1-minute candle; ``time`` is the bar open in broker time."""

    time: datetime
    open: float
    high: float
    low: float
    close: float

    @property
    def close_time(self) -> datetime:
        return self.time + ONE_MINUTE


@dataclass(frozen=True)
class FVG:
    """Three-candle fair value gap. ``bottom``/``top`` bound the untraded zone."""

    kind: str  # "bull" or "bear"
    bottom: float
    top: float
    c1_index: int
    c3_index: int
    created: datetime  # close time of candle 3

    @property
    def size(self) -> float:
        return self.top - self.bottom


@dataclass
class Order:
    side: str  # "long" or "short"
    entry: float
    sl: float
    tp: float
    lots: float
    placed: datetime
    expires: datetime
    session: str
    gap: FVG
    placed_index: int


@dataclass
class Trade:
    side: str
    entry: float
    sl: float
    tp: float
    lots: float
    session: str
    placed: datetime
    filled: datetime
    exit_time: datetime | None = None
    exit_price: float | None = None
    exit_reason: str | None = None
    pnl: float = 0.0

    @property
    def risk_points(self) -> float:
        return abs(self.entry - self.sl)

    @property
    def r_multiple(self) -> float:
        if self.exit_price is None or self.risk_points == 0:
            return 0.0
        move = self.exit_price - self.entry if self.side == "long" else self.entry - self.exit_price
        return move / self.risk_points


@dataclass(frozen=True)
class Event:
    """Something the engine decided; ``message`` is the Dutch line shown to the user."""

    time: datetime
    kind: str
    message: str
    data: dict[str, Any] = field(default_factory=dict)
