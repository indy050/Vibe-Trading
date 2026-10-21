"""Higher-timeframe market structure (HH/HL vs LH/LL) built from 1-minute bars.

The plan reads bias from month, week, day, 4H and 1H. Buckets use broker time,
so 4H and daily candles line up with the broker's own chart.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime

from .models import Bar

TIMEFRAMES = ("MN1", "W1", "D1", "H4", "H1")


def _bucket(t: datetime, tf: str) -> tuple:
    if tf == "MN1":
        return (t.year, t.month)
    if tf == "W1":
        iso = t.isocalendar()
        return (iso.year, iso.week)
    if tf == "D1":
        return (t.date(),)
    if tf == "H4":
        return (t.date(), t.hour // 4)
    if tf == "H1":
        return (t.date(), t.hour)
    raise ValueError(f"unknown timeframe {tf!r}")


def resample(bars: Iterable[Bar], tf: str) -> list[Bar]:
    out: list[Bar] = []
    key = None
    for b in bars:
        k = _bucket(b.time, tf)
        if k != key:
            out.append(b)
            key = k
        else:
            last = out[-1]
            out[-1] = Bar(last.time, last.open, max(last.high, b.high), min(last.low, b.low), b.close)
    return out


def swings(bars: Sequence[Bar], width: int = 2) -> tuple[list[float], list[float]]:
    """Fractal swing highs and lows: a bar beating ``width`` bars on each side."""
    highs: list[float] = []
    lows: list[float] = []
    for i in range(width, len(bars) - width):
        window = bars[i - width:i + width + 1]
        if bars[i].high == max(b.high for b in window) and all(
            b.high < bars[i].high for j, b in enumerate(window) if j != width
        ):
            highs.append(bars[i].high)
        if bars[i].low == min(b.low for b in window) and all(
            b.low > bars[i].low for j, b in enumerate(window) if j != width
        ):
            lows.append(bars[i].low)
    return highs, lows


def structure(bars: Sequence[Bar], width: int = 2) -> str:
    """``bullish`` (HH + HL), ``bearish`` (LH + LL), ``range`` or ``unknown``."""
    highs, lows = swings(bars, width)
    if len(highs) < 2 or len(lows) < 2:
        return "unknown"
    hh, hl = highs[-1] > highs[-2], lows[-1] > lows[-2]
    if hh and hl:
        return "bullish"
    if not hh and not hl:
        return "bearish"
    return "range"


def bias_report(bars_1m: Sequence[Bar]) -> dict[str, str]:
    return {tf: structure(resample(bars_1m, tf)) for tf in TIMEFRAMES}
