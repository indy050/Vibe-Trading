"""Juul, the journal agent: logs every closed trade and keeps the daily P&L calendar.

The user's Trade Portal journal (trade-portal-journal.netlify.app) keeps its data
in the browser and exchanges it through a backup file:
``{"version": 1, "exported": ..., "trades": [...], "deposits": [...]}``.
Restoring a backup *replaces* everything in the browser, so Juul always merges
new trades into the latest backup instead of writing a file with only new trades.
"""

from __future__ import annotations

import argparse
import calendar
import csv
import json
from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import date, datetime
from pathlib import Path

from .engine import SESSION_NL, fmt
from .models import Trade

PAIR = "US30"


def journal_id(trade: Trade) -> int:
    """Stable id (ms timestamp of the fill), so re-running never duplicates a trade."""
    return int(trade.filled.timestamp() * 1000)


def trade_to_entry(trade: Trade, rate: float = 1.0, source: str = "engine") -> dict:
    """One closed trade in the journal's own field layout."""
    if trade.exit_price is None:
        raise ValueError("only closed trades are journalled")
    reason = {"tp": "TP", "sl": "SL"}.get(trade.exit_reason or "", trade.exit_reason or "")
    notes = (f"{SESSION_NL.get(trade.session, trade.session)} · {reason} · {trade.r_multiple:+.1f}R · "
             f"regels gevolgd ({source}) · gevuld {trade.filled:%H:%M}, gesloten {trade.exit_time:%H:%M}")
    return {
        "id": journal_id(trade),
        "date": (trade.exit_time or trade.filled).strftime("%Y-%m-%d"),
        "pair": PAIR,
        "dir": "Long" if trade.side == "long" else "Short",
        "entry": f"{trade.entry:.1f}",
        "sl": f"{trade.sl:.1f}",
        "tp1": f"{trade.tp:.1f}",
        "lots": f"{trade.lots:.2f}",
        "exit": f"{trade.exit_price:.1f}",
        "result": f"{trade.pnl * rate:.2f}",
        "notes": notes,
        "shot": None,
    }


def load_backup(path: str | Path | None) -> dict:
    if not path or not Path(path).exists():
        return {"version": 1, "trades": [], "deposits": []}
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data.get("trades"), list):
        raise ValueError(f"{path} is geen Trade Portal back-up")
    data.setdefault("deposits", [])
    return data


def merge_backup(backup: dict, entries: Iterable[dict]) -> tuple[dict, int]:
    """Add entries whose id is not in the backup yet; keeps manual trades and deposits."""
    known = {str(t.get("id")) for t in backup["trades"]}
    new = [e for e in entries if str(e["id"]) not in known]
    merged = {
        "version": backup.get("version", 1),
        "exported": datetime.now().isoformat(timespec="milliseconds") + "Z",
        "trades": backup["trades"] + new,
        "deposits": backup.get("deposits", []),
    }
    return merged, len(new)


def daily_pnl(entries: Iterable[dict]) -> dict[date, tuple[float, int]]:
    """Per day: summed result and number of closed trades (journal entries, any pair)."""
    days: dict[date, list[float]] = defaultdict(lambda: [0.0, 0])
    for t in entries:
        try:
            d = date.fromisoformat(t["date"])
            r = float(t.get("result") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        days[d][0] += r
        days[d][1] += 1 if r != 0 else 0
    return {d: (round(v[0], 2), int(v[1])) for d, v in days.items()}


def month_summary(daily: dict[date, tuple[float, int]], year: int, month: int) -> dict:
    vals = [(d, p, n) for d, (p, n) in daily.items() if d.year == year and d.month == month]
    traded = [v for v in vals if v[2]]
    return {
        "pnl": round(sum(p for _, p, _ in vals), 2),
        "trades": sum(n for *_, n in vals),
        "green_days": sum(1 for _, p, n in traded if p > 0),
        "red_days": sum(1 for _, p, n in traded if p < 0),
        "best": max(traded, key=lambda v: v[1], default=None),
        "worst": min(traded, key=lambda v: v[1], default=None),
    }


def month_calendar_text(daily: dict[date, tuple[float, int]], year: int, month: int) -> str:
    """Monday-first month grid with the day result in each cell and week totals."""
    names = ["", "januari", "februari", "maart", "april", "mei", "juni", "juli", "augustus",
             "september", "oktober", "november", "december"]
    lines = [f"{names[month]} {year}".center(84), "   ma        di        wo        do        vr        za        zo    | week"]
    for week in calendar.Calendar(firstweekday=0).monthdatescalendar(year, month):
        cells, total = [], 0.0
        for d in week:
            p, n = daily.get(d, (0.0, 0))
            if d.month != month:
                cells.append(" " * 10)
            elif n:
                cells.append(f"{d.day:>2} {p:+7.0f}")
                total += p
            else:
                cells.append(f"{d.day:>2}   ·    ")
        lines.append("".join(cells) + f"| {total:+7.0f}")
    s = month_summary(daily, year, month)
    lines.append(f"Maand {s['pnl']:+.2f} · {s['trades']} trades · {s['green_days']} groene en {s['red_days']} rode dagen")
    return "\n".join(lines)


def read_trades_csv(path: str | Path) -> list[Trade]:
    """Trades written by the backtest/live runner (``--trades-csv``)."""
    out = []
    for r in csv.DictReader(Path(path).read_text(encoding="utf-8").splitlines()):
        if not r.get("exit"):
            continue
        t = Trade(r["side"], float(r["entry"]), float(r["sl"]), float(r["tp"]), float(r["lots"]), r["session"],
                  datetime.fromisoformat(r["placed"]), datetime.fromisoformat(r["filled"]),
                  datetime.fromisoformat(r["exit_time"]), float(r["exit"]), r["reason"], float(r["pnl"]))
        out.append(t)
    return out


def journal_line(trade: Trade) -> str:
    """What Juul says in the office feed."""
    sign = "+" if trade.pnl >= 0 else "−"
    return (f"Gejournald: US30 {'long' if trade.side == 'long' else 'short'} {SESSION_NL.get(trade.session, '')}, "
            f"{sign}{fmt(abs(trade.pnl))} ({trade.r_multiple:+.1f}R), regels gevolgd.")


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Juul: trades journallen in de Trade Portal back-up en de P&L-kalender tonen.")
    ap.add_argument("--backup", help="laatste back-up uit de Trade Portal (Back-up downloaden)")
    ap.add_argument("--trades", help="CSV met gesloten trades (van de backtest of live runner)")
    ap.add_argument("--out", help="schrijf de samengevoegde back-up hierheen (om terug te zetten in de journal)")
    ap.add_argument("--rate", type=float, default=1.0, help="omrekenkoers naar je journal-valuta")
    ap.add_argument("--source", default="engine", help="label in de notities, bijv. 'demo' of 'backtest'")
    ap.add_argument("--month", help="toon de P&L-kalender van deze maand (JJJJ-MM), standaard deze maand")
    args = ap.parse_args(argv)

    backup = load_backup(args.backup)
    if args.trades:
        entries = [trade_to_entry(t, args.rate, args.source) for t in read_trades_csv(args.trades)]
        backup, added = merge_backup(backup, entries)
        print(f"{added} nieuwe trades gejournald ({len(entries) - added} stonden er al in).")
        if args.out:
            Path(args.out).write_text(json.dumps(backup, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"Back-up geschreven: {args.out}. Zet hem terug via 'Back-up terugzetten' in de journal.")
    y, m = (map(int, args.month.split("-")) if args.month else (date.today().year, date.today().month))
    print()
    print(month_calendar_text(daily_pnl(backup["trades"]), y, m))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
