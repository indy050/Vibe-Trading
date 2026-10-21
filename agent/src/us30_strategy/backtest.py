"""Backtest the US30 plan on 1-minute bars exported from MT5.

Usage (from ``agent/``)::

    python -m src.us30_strategy US30_M1.csv --equity 10000 --news news.csv

Fill model: a limit fills when a later candle trades through it; a candle that
touches both stop and target counts as a stop; a stop touched inside the fill
candle counts as a loss. That is deliberately pessimistic.
"""

from __future__ import annotations

import argparse
import csv
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from .config import StrategyConfig
from .engine import Us30Strategy
from .models import Bar, Event, Trade

_TIME_FORMATS = ("%Y.%m.%d %H:%M:%S", "%Y.%m.%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S")


def _parse_time(text: str) -> datetime:
    for f in _TIME_FORMATS:
        try:
            return datetime.strptime(text.strip(), f)
        except ValueError:
            pass
    raise ValueError(f"unrecognised timestamp {text!r}")


def load_bars_csv(path: str | Path) -> list[Bar]:
    """Read MT5 'Export bars' files (<DATE> <TIME> <OPEN> ...) or time,open,high,low,close CSV."""
    text = Path(path).read_text(encoding="utf-8-sig")
    dialect = csv.Sniffer().sniff(text.splitlines()[0], delimiters=",;\t")
    rows = list(csv.reader(text.splitlines(), dialect))
    header = [h.strip("<> ").lower() for h in rows[0]]
    col = {name: idx for idx, name in enumerate(header)}
    bars = []
    for r in rows[1:]:
        if not r:
            continue
        if "date" in col and "time" in col:
            t = _parse_time(f"{r[col['date']]} {r[col['time']]}")
        else:
            t = _parse_time(r[col.get("time", col.get("datetime", 0))])
        bars.append(Bar(t, float(r[col["open"]]), float(r[col["high"]]), float(r[col["low"]]), float(r[col["close"]])))
    bars.sort(key=lambda b: b.time)
    return bars


def load_news_csv(path: str | Path) -> tuple[datetime, ...]:
    """One high-impact US event per line, first column a broker-time timestamp."""
    out = []
    for r in csv.reader(Path(path).read_text(encoding="utf-8-sig").splitlines()):
        if r and r[0].strip() and not r[0].lower().startswith(("time", "date")):
            out.append(_parse_time(r[0]))
    return tuple(sorted(out))


@dataclass
class BacktestResult:
    trades: list[Trade]
    events: list[Event]
    start_equity: float
    end_equity: float

    def summary(self) -> dict:
        closed = [t for t in self.trades if t.exit_price is not None]
        wins = [t for t in closed if t.pnl > 0]
        losses = [t for t in closed if t.pnl <= 0]
        gross_win = sum(t.pnl for t in wins)
        gross_loss = -sum(t.pnl for t in losses)
        peak, eq, max_dd = self.start_equity, self.start_equity, 0.0
        for t in closed:
            eq += t.pnl
            peak = max(peak, eq)
            max_dd = max(max_dd, (peak - eq) / peak)
        kinds = [e.kind for e in self.events]
        return {
            "trades": len(closed),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": len(wins) / len(closed) if closed else 0.0,
            "net_pnl": self.end_equity - self.start_equity,
            "return": self.end_equity / self.start_equity - 1,
            "avg_r": sum(t.r_multiple for t in closed) / len(closed) if closed else 0.0,
            "profit_factor": gross_win / gross_loss if gross_loss else float("inf") if gross_win else 0.0,
            "max_drawdown": max_dd,
            "setups": kinds.count("ifvg"),
            "orders": kinds.count("order_placed"),
            "orders_expired": sum(1 for e in self.events if e.kind == "order_cancelled" and "verstreken" in e.message),
        }


def run_backtest(bars: Iterable[Bar], config: StrategyConfig | None = None, equity: float = 10_000.0) -> BacktestResult:
    strat = Us30Strategy(config, equity)
    for b in bars:
        strat.on_bar(b)
    return BacktestResult(strat.trades, strat.events, equity, strat.equity)


def _print_summary(res: BacktestResult) -> None:
    s = res.summary()
    print(f"Trades        {s['trades']}  (winst {s['wins']}, verlies {s['losses']}, winrate {s['win_rate']:.0%})")
    print(f"Resultaat     {s['net_pnl']:+.2f}  ({s['return']:+.2%})")
    print(f"Gemiddeld     {s['avg_r']:+.2f}R per trade, profit factor {s['profit_factor']:.2f}")
    print(f"Max drawdown  {s['max_drawdown']:.2%}")
    print(f"Setups        {s['setups']} IFVG's, {s['orders']} orders, {s['orders_expired']} verlopen")


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Backtest van het US30 sweep + FVG handelsplan op 1-minuut data.")
    ap.add_argument("bars", help="CSV met 1-minuut candles (MT5 export) in brokertijd")
    ap.add_argument("--equity", type=float, default=10_000.0)
    ap.add_argument("--news", help="CSV met tijden van belangrijk US-nieuws (brokertijd)")
    ap.add_argument("--spread", type=float, default=0.0, help="vaste spread in punten")
    ap.add_argument("--value-per-point", type=float, default=1.0, help="accountvaluta per punt per lot")
    ap.add_argument("--tp-mode", default="previous", choices=["previous", "current", "nearest", "furthest"])
    ap.add_argument("--trades-csv", help="schrijf alle trades naar dit bestand")
    ap.add_argument("--log", action="store_true", help="toon elke beslissing van de engine")
    args = ap.parse_args(argv)

    cfg = StrategyConfig(spread=args.spread, value_per_point_per_lot=args.value_per_point, tp_mode=args.tp_mode)
    if args.news:
        cfg = replace(cfg, news_events=load_news_csv(args.news))
    res = run_backtest(load_bars_csv(args.bars), cfg, args.equity)
    if args.log:
        for e in res.events:
            print(f"{e.time:%Y-%m-%d %H:%M}  {e.message}")
        print()
    _print_summary(res)
    if args.trades_csv:
        with open(args.trades_csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["session", "side", "placed", "filled", "entry", "sl", "tp", "lots", "exit_time", "exit", "reason", "pnl", "r"])
            for t in res.trades:
                w.writerow([t.session, t.side, t.placed, t.filled, t.entry, t.sl, t.tp, t.lots,
                            t.exit_time, t.exit_price, t.exit_reason, round(t.pnl, 2), round(t.r_multiple, 2)])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
