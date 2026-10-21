"""Juul, the journal agent: Trade Portal backup format, merging and the P&L calendar."""

from __future__ import annotations

import json
from datetime import date, datetime

import pytest

from src.us30_strategy.backtest import main as backtest_main
from src.us30_strategy.journal import (
    daily_pnl, journal_line, load_backup, main, merge_backup, month_calendar_text, month_summary,
    read_trades_csv, trade_to_entry,
)
from src.us30_strategy.models import Trade

WIN = Trade("long", 50897.5, 50881, 50990, 1.81, "london", datetime(2026, 10, 20, 9, 16), datetime(2026, 10, 20, 9, 16),
            datetime(2026, 10, 20, 9, 19), 50990, "tp", 167.43)
MANUAL = {"id": 1760000000000, "date": "2026-10-20", "pair": "XAU/USD", "dir": "Short", "entry": "", "sl": "",
          "tp1": "", "lots": "", "exit": "", "result": "-30.00", "notes": "handmatig", "shot": None}


def test_entry_matches_trade_portal_layout():
    e = trade_to_entry(WIN)
    assert set(e) == {"id", "date", "pair", "dir", "entry", "sl", "tp1", "lots", "exit", "result", "notes", "shot"}
    assert (e["date"], e["pair"], e["dir"], e["entry"], e["result"]) == ("2026-10-20", "US30", "Long", "50897.5", "167.43")
    assert "Londen" in e["notes"] and "+5.6R" in e["notes"]
    assert trade_to_entry(WIN, rate=0.5)["result"] == "83.72"
    with pytest.raises(ValueError):
        trade_to_entry(Trade("long", 1, 0, 2, 1, "ny", WIN.placed, WIN.filled))


def test_merge_keeps_manual_trades_and_deposits_and_never_duplicates():
    backup = {"version": 1, "trades": [MANUAL], "deposits": [{"id": 1, "date": "2026-10-01", "amount": 500}]}
    merged, added = merge_backup(backup, [trade_to_entry(WIN)])
    assert added == 1 and len(merged["trades"]) == 2 and merged["deposits"] == backup["deposits"]
    again, added2 = merge_backup(merged, [trade_to_entry(WIN)])
    assert added2 == 0 and len(again["trades"]) == 2


def test_daily_pnl_and_calendar():
    entries = [MANUAL, trade_to_entry(WIN), {"date": "2026-10-21", "result": "0"}]
    daily = daily_pnl(entries)
    assert daily[date(2026, 10, 20)] == (137.43, 2)
    assert daily[date(2026, 10, 21)] == (0.0, 0)
    s = month_summary(daily, 2026, 10)
    assert s["green_days"] == 1 and s["trades"] == 2
    text = month_calendar_text(daily, 2026, 10)
    assert "oktober 2026" in text and "20    +137" in text
    assert journal_line(WIN).startswith("Gejournald: US30 long Londen, +167,4")


def test_cli_journals_backtest_trades_into_backup(tmp_path):
    from tests.test_us30_strategy import AFTER_1_WIN, ASIA_1, LONDON_1

    bars = tmp_path / "bars.csv"
    rows = ["time,open,high,low,close"] + [f"2026-10-20 {t}:00,{o},{h},{lo},{c}" for t, o, h, lo, c in ASIA_1 + LONDON_1 + AFTER_1_WIN]
    bars.write_text("\n".join(rows), encoding="utf-8")
    trades_csv = tmp_path / "trades.csv"
    backtest_main([str(bars), "--trades-csv", str(trades_csv)])
    assert len(read_trades_csv(trades_csv)) == 1
    backup = tmp_path / "backup.json"
    backup.write_text(json.dumps({"version": 1, "trades": [MANUAL], "deposits": []}), encoding="utf-8")
    out = tmp_path / "merged.json"
    main(["--backup", str(backup), "--trades", str(trades_csv), "--out", str(out), "--month", "2026-10"])
    merged = load_backup(out)
    assert [t["pair"] for t in merged["trades"]] == ["XAU/USD", "US30"]
