"""Parameters of the US30 liquidity-sweep + FVG reversal plan.

Every number from the trading plan lives here so the engine itself contains no
magic values. All clock times are broker server time (IC Markets MT5), the same
clock the plan's session boxes use.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time


@dataclass(frozen=True)
class SessionTimes:
    asia_start: time = time(2, 0)
    london_start: time = time(9, 0)
    ny_start: time = time(14, 30)
    ny_end: time = time(23, 0)


@dataclass(frozen=True)
class StrategyConfig:
    sessions: SessionTimes = field(default_factory=SessionTimes)

    # Trading window: no new setups outside it; unfilled limits are removed at its end.
    window_start: time = time(9, 0)
    window_end: time = time(20, 30)

    # Fair value gap size on the 1-minute chart, in index points ($1 = 1 point).
    fvg_min: float = 1.0
    fvg_max: float = 20.0
    # Ignore "gaps" across missing minutes (weekend, data holes).
    require_contiguous_bars: bool = True

    # Disrespect (IFVG) must close within this many minutes of the FVG forming.
    ifvg_max_minutes: int = 20
    # After the IFVG, how long to wait for the entry gap to form.
    entry_gap_max_minutes: int = 20
    # Limit sits this fraction into the entry gap, measured from the side price enters.
    entry_fraction: float = 0.25
    # Pending limit lifetime.
    order_valid_minutes: int = 60
    # Price already beyond the limit level when the entry gap forms: enter at market.
    market_entry_if_past: bool = True

    # Stop beyond the sweep extreme.
    sl_buffer: float = 3.0
    # Take profit: "previous" = previous session's opposite extreme (plan example 1),
    # "current" = current session extreme so far, "nearest"/"furthest" of the two.
    tp_mode: str = "previous"

    # Risk and position sizing.
    risk_fraction: float = 0.003
    value_per_point_per_lot: float = 1.0  # account currency per 1.0 point per 1 lot
    lot_step: float = 0.01
    min_lot: float = 0.01

    # Trade limits.
    max_entries_per_session: int = 3
    max_entries_per_day: int = 6
    # Pause after a losing trade (minutes). 0 = no pause, the user's choice.
    cooldown_after_loss_minutes: int = 0

    # High-impact US news blackout (event times in broker time).
    news_before_minutes: int = 40
    news_after_minutes: int = 15
    news_events: tuple[datetime, ...] = ()
    cancel_pending_on_news: bool = True

    # Optional higher-timeframe bias filter. None = informational only (plan default).
    bias_filter_timeframe: str | None = None

    # Execution model for backtests: fixed spread in points, applied to the ask side.
    spread: float = 0.0
