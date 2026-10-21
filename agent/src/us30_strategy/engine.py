"""Bar-by-bar engine for the US30 liquidity-sweep + FVG reversal plan.

Feed closed 1-minute bars (broker time) to :meth:`Us30Strategy.on_bar`. The engine
only looks at bars it has already received, so the same code drives backtests and
a live loop. It decides; it never talks to a broker. Execution belongs to the MT5
connector, which keeps its own order gate and kill switch.

Rules (trading plan, page 1):
1. Sweep: London sweeps the Asia high/low, New York sweeps the London high/low.
2. The last FVG of the trend leg into the sweep (bearish for a long, bullish for a short), $1-$20.
3. A candle closes through that FVG within 20 minutes of it forming (IFVG).
4. Entry gap: the lowest bullish FVG of the reversal (buy) / highest bearish FVG (sell).
5. Limit on the first 25% of the entry gap, valid 60 minutes.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .config import StrategyConfig
from .models import FVG, ONE_MINUTE, Bar, Event, Order, Trade
from .structure import resample, structure

SESSION_NL = {"asia": "Azië", "london": "Londen", "ny": "New York"}
PREVIOUS_SESSION = {"london": "asia", "ny": "london"}


def fmt(v: float) -> str:
    """Dutch number format used in event messages: 50.897,5"""
    s = f"{v:,.1f}".replace(",", "_").replace(".", ",").replace("_", ".")
    return s[:-2] if s.endswith(",0") else s


@dataclass
class _Waiting:
    """An IFVG has confirmed; waiting for the entry gap."""

    sweep_index: int
    ifvg_time: datetime
    deadline: datetime
    fvg: FVG


@dataclass
class _Scan:
    """Setup search state for one London or New York session."""

    session: str
    day: date
    ref_high: float
    ref_low: float
    sweep_low: float | None = None
    sweep_low_index: int | None = None
    sweep_high: float | None = None
    sweep_high_index: int | None = None
    last_bear: FVG | None = None
    last_bull: FVG | None = None
    fvgs: list[FVG] = field(default_factory=list)
    waiting: dict[str, _Waiting] = field(default_factory=dict)


class Us30Strategy:
    def __init__(self, config: StrategyConfig | None = None, equity: float = 10_000.0,
                 on_event: Callable[[Event], None] | None = None) -> None:
        self.cfg = config or StrategyConfig()
        self.equity = equity
        self.on_event = on_event
        self.bars: list[Bar] = []
        self.levels: dict[tuple[date, str], list[float]] = {}
        self.scan: _Scan | None = None
        self.order: Order | None = None
        self.position: Trade | None = None
        self.position_index: int | None = None
        self.trades: list[Trade] = []
        self.events: list[Event] = []
        self.entries_session: Counter = Counter()
        self.entries_day: Counter = Counter()
        self.cooldown_until: datetime | None = None

    # ------------------------------------------------------------------ helpers
    def session_of(self, t: datetime) -> str | None:
        s, tt = self.cfg.sessions, t.time()
        if s.asia_start <= tt < s.london_start:
            return "asia"
        if s.london_start <= tt < s.ny_start:
            return "london"
        if s.ny_start <= tt < s.ny_end:
            return "ny"
        return None

    def in_window(self, t: datetime) -> bool:
        return self.cfg.window_start <= t.time() < self.cfg.window_end

    def in_news_blackout(self, t: datetime) -> bool:
        before = timedelta(minutes=self.cfg.news_before_minutes)
        after = timedelta(minutes=self.cfg.news_after_minutes)
        return any(ev - before <= t <= ev + after for ev in self.cfg.news_events)

    def _emit(self, t: datetime, kind: str, message: str, **data) -> Event:
        ev = Event(t, kind, message, data)
        self.events.append(ev)
        if self.on_event:
            self.on_event(ev)
        return ev

    def lots_for(self, risk_points: float) -> float:
        if risk_points <= 0:
            return 0.0
        raw = self.equity * self.cfg.risk_fraction / (risk_points * self.cfg.value_per_point_per_lot)
        lots = math.floor(raw / self.cfg.lot_step + 1e-9) * self.cfg.lot_step
        return round(lots, 8) if lots >= self.cfg.min_lot else 0.0

    def bias(self, timeframe: str) -> str:
        return structure(resample(self.bars, timeframe))

    def blocked_reason(self, t: datetime, side: str) -> str | None:
        """Why no new setup may be taken right now, or None."""
        cfg = self.cfg
        if not self.in_window(t):
            return "buiten het handelsvenster"
        if self.position is not None:
            return "er loopt al een trade"
        if self.order is not None:
            return "er staat al een order"
        sess = self.session_of(t)
        if self.entries_session[(t.date(), sess)] >= cfg.max_entries_per_session:
            return f"maximum van {cfg.max_entries_per_session} entries deze sessie bereikt"
        if self.entries_day[t.date()] >= cfg.max_entries_per_day:
            return f"maximum van {cfg.max_entries_per_day} entries vandaag bereikt"
        if self.cooldown_until and t < self.cooldown_until:
            return "pauze na verlies"
        if self.in_news_blackout(t):
            return "nieuwsvenster"
        if cfg.bias_filter_timeframe:
            b = self.bias(cfg.bias_filter_timeframe)
            if (side == "long" and b == "bearish") or (side == "short" and b == "bullish"):
                return f"{cfg.bias_filter_timeframe}-structuur is {b}"
        return None

    def cancel_pending(self, t: datetime, reason: str = "handmatig geannuleerd") -> None:
        """Cancel a pending order and any setup waiting for its entry gap."""
        if self.order is not None:
            o = self.order
            self.order = None
            self._emit(t, "order_cancelled", f"Limit {o.side} @ {fmt(o.entry)} verwijderd: {reason}.", reason=reason)
        if self.scan:
            self.scan.waiting.clear()

    # ------------------------------------------------------------------ main loop
    def on_bar(self, bar: Bar) -> list[Event]:
        start = len(self.events)
        i = len(self.bars)
        self.bars.append(bar)
        t = bar.time
        sess = self.session_of(t)

        if sess:
            lv = self.levels.setdefault((t.date(), sess), [bar.high, bar.low])
            lv[0], lv[1] = max(lv[0], bar.high), min(lv[1], bar.low)

        self._manage_position(bar, i)
        self._manage_order(bar, i, sess)
        self._update_scan(bar, i, sess)
        if self.scan:
            self._update_sweeps(bar, i)
            self._check_disrespect(bar)
            self._detect_fvg(i)
            self._try_entry(bar, i)
        return self.events[start:]

    # ------------------------------------------------------------------ position / order
    def _close_position(self, t: datetime, price: float, reason: str) -> None:
        p = self.position
        sign = 1 if p.side == "long" else -1
        p.exit_time, p.exit_price, p.exit_reason = t, price, reason
        p.pnl = (price - p.entry) * sign * p.lots * self.cfg.value_per_point_per_lot
        self.equity += p.pnl
        self.position = None
        self.position_index = None
        if p.pnl < 0 and self.cfg.cooldown_after_loss_minutes:
            self.cooldown_until = t + timedelta(minutes=self.cfg.cooldown_after_loss_minutes)
        label = {"tp": "take profit", "sl": "stop loss"}[reason]
        self._emit(t, "exit", f"{p.side.capitalize()} gesloten op {label} @ {fmt(price)}, {p.r_multiple:+.1f}R.",
                   reason=reason, pnl=round(p.pnl, 2), r=round(p.r_multiple, 2))

    def _exit_hits(self, bar: Bar, p: Trade) -> tuple[bool, bool]:
        sp = self.cfg.spread
        if p.side == "long":  # long exits on bid
            return bar.low <= p.sl, bar.high >= p.tp
        return bar.high + sp >= p.sl, bar.low + sp <= p.tp  # short exits on ask

    def _manage_position(self, bar: Bar, i: int) -> None:
        p = self.position
        if p is None or self.position_index is None or self.position_index >= i:
            return
        sl_hit, tp_hit = self._exit_hits(bar, p)
        # When one candle touches both, assume the stop came first (conservative).
        if sl_hit:
            self._close_position(bar.close_time, p.sl, "sl")
        elif tp_hit:
            self._close_position(bar.close_time, p.tp, "tp")

    def _open(self, trade: Trade, i: int) -> None:
        self.position = trade
        self.position_index = i
        self.trades.append(trade)
        self.entries_session[(trade.filled.date(), trade.session)] += 1
        self.entries_day[trade.filled.date()] += 1

    def _manage_order(self, bar: Bar, i: int, sess: str | None) -> None:
        o = self.order
        if o is None or o.placed_index >= i:
            return
        if bar.time >= o.expires:
            self.cancel_pending(bar.time, f"{self.cfg.order_valid_minutes} minuten verstreken")
            return
        if sess is None or not self.in_window(bar.time):
            self.cancel_pending(bar.time, "handelsvenster dicht")
            return
        if self.cfg.cancel_pending_on_news and self.in_news_blackout(bar.time):
            self.cancel_pending(bar.time, "nieuwsvenster")
            return
        sp = self.cfg.spread
        filled = bar.low + sp <= o.entry if o.side == "long" else bar.high >= o.entry
        if not filled:
            return
        self.order = None
        # A candle that opens beyond the limit fills at its open, the better price.
        price = min(o.entry, bar.open + sp) if o.side == "long" else max(o.entry, bar.open)
        self._open(Trade(o.side, price, o.sl, o.tp, o.lots, o.session, o.placed, bar.time), i)
        self._emit(bar.time, "filled", f"Limit {o.side} gevuld @ {fmt(price)} ({o.lots:.2f} lot).",
                   side=o.side, entry=price, lots=o.lots)
        sl_hit, _ = self._exit_hits(bar, self.position)
        if sl_hit:  # stop touched inside the fill candle: count it as stopped out
            self._close_position(bar.close_time, o.sl, "sl")

    # ------------------------------------------------------------------ setup search
    def _update_scan(self, bar: Bar, i: int, sess: str | None) -> None:
        if sess not in PREVIOUS_SESSION:
            self.scan = None
            return
        if self.scan and self.scan.session == sess and self.scan.day == bar.time.date():
            return
        ref = self.levels.get((bar.time.date(), PREVIOUS_SESSION[sess]))
        if not ref:
            self.scan = None
            self._emit(bar.time, "no_reference", f"Geen {SESSION_NL[PREVIOUS_SESSION[sess]]}-data: geen sweep-niveaus voor {SESSION_NL[sess]}.")
            return
        self.scan = _Scan(sess, bar.time.date(), ref_high=ref[0], ref_low=ref[1])
        self._emit(bar.time, "session_start",
                   f"{SESSION_NL[sess]} open. Sweep-niveaus: hoog {fmt(ref[0])}, laag {fmt(ref[1])}.",
                   session=sess, ref_high=ref[0], ref_low=ref[1])

    def _update_sweeps(self, bar: Bar, i: int) -> None:
        s = self.scan
        prev = SESSION_NL[PREVIOUS_SESSION[s.session]]
        if bar.low < s.ref_low and (s.sweep_low is None or bar.low < s.sweep_low):
            if s.sweep_low is None:
                self._emit(bar.time, "sweep", f"Sweep van het {prev}-laag ({fmt(s.ref_low)}).", side="long", level=s.ref_low)
            s.sweep_low, s.sweep_low_index = bar.low, i
        if bar.high > s.ref_high and (s.sweep_high is None or bar.high > s.sweep_high):
            if s.sweep_high is None:
                self._emit(bar.time, "sweep", f"Sweep van het {prev}-hoog ({fmt(s.ref_high)}).", side="short", level=s.ref_high)
            s.sweep_high, s.sweep_high_index = bar.high, i

    def _check_disrespect(self, bar: Bar) -> None:
        s, limit = self.scan, timedelta(minutes=self.cfg.ifvg_max_minutes)
        checks = (
            ("long", s.last_bear, lambda f: bar.close > f.top, s.sweep_low_index),
            ("short", s.last_bull, lambda f: bar.close < f.bottom, s.sweep_high_index),
        )
        for side, fvg, broken, sweep_index in checks:
            if fvg is None or not broken(fvg):
                continue
            if side == "long":
                s.last_bear = None
            else:
                s.last_bull = None
            # The sweep must belong to this leg: its extreme comes at or after the FVG.
            if sweep_index is None or sweep_index < fvg.c1_index:
                continue
            age = bar.close_time - fvg.created
            minutes = int(age.total_seconds() // 60)
            if age > limit:
                self._emit(bar.time, "ifvg_too_late",
                           f"FVG {fmt(fvg.bottom)}-{fmt(fvg.top)} pas na {minutes} min doorbroken (max {self.cfg.ifvg_max_minutes}): geen setup.",
                           side=side, minutes=minutes)
                continue
            reason = self.blocked_reason(bar.time, side)
            if reason:
                self._emit(bar.time, "setup_skipped", f"IFVG {side} genegeerd: {reason}.", side=side, reason=reason)
                continue
            s.waiting[side] = _Waiting(sweep_index, bar.close_time,
                                       bar.close_time + timedelta(minutes=self.cfg.entry_gap_max_minutes), fvg)
            self._emit(bar.time, "ifvg",
                       f"IFVG {side}: slot {fmt(bar.close)} door FVG {fmt(fvg.bottom)}-{fmt(fvg.top)} na {minutes} min.",
                       side=side, fvg=(fvg.bottom, fvg.top), minutes=minutes)

    def _detect_fvg(self, i: int) -> None:
        if i < 2:
            return
        c1, c3 = self.bars[i - 2], self.bars[i]
        if self.cfg.require_contiguous_bars and c3.time - c1.time != 2 * ONE_MINUTE:
            return
        if c3.low > c1.high:
            fvg = FVG("bull", c1.high, c3.low, i - 2, i, c3.close_time)
        elif c3.high < c1.low:
            fvg = FVG("bear", c3.high, c1.low, i - 2, i, c3.close_time)
        else:
            return
        if not (self.cfg.fvg_min <= fvg.size <= self.cfg.fvg_max):
            return
        self.scan.fvgs.append(fvg)
        if fvg.kind == "bear":
            self.scan.last_bear = fvg
        else:
            self.scan.last_bull = fvg

    def _target(self, side: str, entry: float) -> float | None:
        s = self.scan
        cur = self.levels[(s.day, s.session)]
        if side == "long":
            prev, now = s.ref_high, cur[0]
            ok = [v for v in (prev, now) if v > entry]
            pick_near = min
        else:
            prev, now = s.ref_low, cur[1]
            ok = [v for v in (prev, now) if v < entry]
            pick_near = max
        if not ok:
            return None
        mode = self.cfg.tp_mode
        if mode == "previous":
            return prev if prev in ok else now
        if mode == "current":
            return now if now in ok else prev
        if mode == "nearest":
            return pick_near(ok)
        if mode == "furthest":
            return (max if side == "long" else min)(ok)
        raise ValueError(f"unknown tp_mode {mode!r}")

    def _try_entry(self, bar: Bar, i: int) -> None:
        s = self.scan
        for side in list(s.waiting):
            w = s.waiting[side]
            if side == "long":
                gaps = [g for g in s.fvgs if g.kind == "bull" and g.c1_index >= w.sweep_index]
                gap = min(gaps, key=lambda g: g.bottom) if gaps else None
            else:
                gaps = [g for g in s.fvgs if g.kind == "bear" and g.c1_index >= w.sweep_index]
                gap = max(gaps, key=lambda g: g.top) if gaps else None
            if gap is None:
                if bar.close_time >= w.deadline:
                    del s.waiting[side]
                    self._emit(bar.time, "setup_expired", f"Geen entry gap binnen {self.cfg.entry_gap_max_minutes} min na de IFVG: setup vervalt.", side=side)
                continue
            del s.waiting[side]
            self._place(bar, i, side, gap)
            if self.order:
                s.waiting.clear()
                return

    def _place(self, bar: Bar, i: int, side: str, gap: FVG) -> None:
        s, cfg = self.scan, self.cfg
        reason = self.blocked_reason(bar.time, side)
        if reason:
            self._emit(bar.time, "setup_skipped", f"Entry gap {side} gevonden maar geen order: {reason}.", side=side, reason=reason)
            return
        if side == "long":
            entry = gap.top - cfg.entry_fraction * gap.size
            sl = s.sweep_low - cfg.sl_buffer
            market = bar.close <= entry
            price = bar.close + cfg.spread  # a market buy pays the ask
        else:
            entry = gap.bottom + cfg.entry_fraction * gap.size
            sl = s.sweep_high + cfg.sl_buffer
            market = bar.close >= entry
            price = bar.close
        if market:
            # Price reached the entry level before the limit could be placed: enter at market.
            if not cfg.market_entry_if_past:
                self._emit(bar.time, "setup_skipped", f"Koers ({fmt(bar.close)}) al voorbij de entry {fmt(entry)}: geen order.", side=side)
                return
            if (side == "long" and price <= sl) or (side == "short" and price >= sl):
                self._emit(bar.time, "setup_skipped", f"Koers ({fmt(bar.close)}) al voorbij de stop {fmt(sl)}: geen trade.", side=side)
                return
            entry = price
        tp = self._target(side, entry)
        if tp is None:
            self._emit(bar.time, "setup_skipped", "Geen sessie-hoog/laag als doel voorbij de entry: geen order.", side=side)
            return
        lots = self.lots_for(abs(entry - sl))
        if lots <= 0:
            self._emit(bar.time, "setup_skipped", "Stop te ver voor de minimale lotgrootte bij 0,3% risico: geen order.", side=side)
            return
        rr = abs(tp - entry) / abs(entry - sl)
        if market:
            self._open(Trade(side, entry, sl, tp, lots, s.session, bar.close_time, bar.close_time), i)
            self._emit(bar.time, "market_entry",
                       f"Koers al in de entry gap: direct {'gekocht' if side == 'long' else 'verkocht'} @ {fmt(entry)} · SL {fmt(sl)} · TP {fmt(tp)} · {lots:.2f} lot · {rr:.1f}:1.",
                       side=side, entry=entry, sl=sl, tp=tp, lots=lots, gap=(gap.bottom, gap.top))
            return
        self.order = Order(side, entry, sl, tp, lots, bar.close_time,
                           bar.close_time + timedelta(minutes=cfg.order_valid_minutes), s.session, gap, i)
        self._emit(bar.time, "order_placed",
                   f"{'Buy' if side == 'long' else 'Sell'} limit {fmt(entry)} · SL {fmt(sl)} · TP {fmt(tp)} · {lots:.2f} lot · {rr:.1f}:1.",
                   side=side, entry=entry, sl=sl, tp=tp, lots=lots, gap=(gap.bottom, gap.top))
