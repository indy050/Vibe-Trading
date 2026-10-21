"""US30 sweep + FVG plan: the worked examples from the trading plan as regression tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from src.us30_strategy import Bar, StrategyConfig, Us30Strategy, bias_report, load_bars_csv, run_backtest
from src.us30_strategy.engine import fmt

DAY = datetime(2026, 10, 20)


def _bars(rows):
    out = []
    for hhmm, o, h, lo, c in rows:
        hh, mm = map(int, hhmm.split(":"))
        out.append(Bar(DAY.replace(hour=hh, minute=mm), o, h, lo, c))
    return out


def _run(bars, cfg=None):
    s = Us30Strategy(cfg or StrategyConfig(), 10_000.0)
    for b in bars:
        s.on_bar(b)
    return s


def _kinds(s):
    return [e.kind for e in s.events]


# Plan example 1: London long after a sweep of the Asia low (50.900).
ASIA_1 = [("08:00", 50950, 50990, 50900, 50960)]
LONDON_1 = [
    ("09:10", 50910, 50911, 50901, 50902),
    ("09:11", 50902, 50903, 50894, 50895),  # c1 of the last bearish FVG, breaks the Asia low
    ("09:12", 50895, 50896, 50886, 50887),
    ("09:13", 50887, 50890, 50884, 50885),  # sweep low 50.884; bearish FVG 50.890-50.894
    ("09:14", 50885, 50899, 50885, 50897),  # closes through the FVG: IFVG
    ("09:15", 50901, 50908, 50900, 50906),  # bullish FVG 50.890-50.900 = entry gap
]
AFTER_1_WIN = [
    ("09:16", 50906, 50907, 50897, 50899),  # fills 50.897,5
    ("09:17", 50899, 50960, 50898, 50950),
    ("09:18", 50950, 50995, 50940, 50990),  # Asia high 50.990 = TP
]


def test_example_1_long_matches_plan_numbers():
    s = _run(_bars(ASIA_1 + LONDON_1 + AFTER_1_WIN))
    placed = next(e for e in s.events if e.kind == "order_placed")
    assert placed.data["side"] == "long"
    assert placed.data["entry"] == pytest.approx(50897.5)
    assert placed.data["sl"] == pytest.approx(50881)
    assert placed.data["tp"] == pytest.approx(50990)
    assert placed.data["lots"] == pytest.approx(1.81)  # 30 / 16.5 points, rounded down to 0.01
    [t] = s.trades
    assert t.exit_reason == "tp"
    assert t.pnl == pytest.approx(92.5 * 1.81)  # ~168 as in the plan
    assert t.r_multiple == pytest.approx(92.5 / 16.5)
    assert _kinds(s).index("sweep") < _kinds(s).index("ifvg") < _kinds(s).index("order_placed")


# Plan example 2: New York short after a sweep of the London high (51.120).
ASIA_2 = [("08:00", 51050, 51060, 51000, 51040)]
LONDON_2 = [("10:00", 51100, 51120, 51090, 51110), ("11:00", 51040, 51050, 51020, 51030)]
NY_2 = [
    ("14:40", 51110, 51116, 51108, 51115),
    ("14:41", 51115, 51125, 51114, 51123),
    ("14:42", 51123, 51128, 51120, 51126),  # sweep high 51.128; bullish FVG 51.116-51.120
    ("14:43", 51126, 51126, 51112, 51114),  # closes below it: IFVG
    ("14:44", 51109, 51110, 51100, 51102),  # bearish FVG 51.110-51.120 = entry gap
]


def test_example_2_short_matches_plan_numbers():
    tail = [("14:45", 51102, 51113, 51101, 51110), ("14:46", 51110, 51111, 51015, 51020)]
    s = _run(_bars(ASIA_2 + LONDON_2 + NY_2 + tail))
    placed = next(e for e in s.events if e.kind == "order_placed")
    assert placed.data["side"] == "short"
    assert placed.data["entry"] == pytest.approx(51112.5)
    assert placed.data["sl"] == pytest.approx(51131)
    assert placed.data["tp"] == pytest.approx(51020)  # London low
    [t] = s.trades
    assert t.exit_reason == "tp" and t.pnl > 0


def test_example_3_unfilled_limit_expires_after_60_minutes():
    start = DAY.replace(hour=14, minute=45)
    drift = [Bar(start + timedelta(minutes=k), 51100, 51105, 51095, 51100) for k in range(70)]
    s = _run(_bars(ASIA_2 + LONDON_2 + NY_2) + drift)
    cancelled = [e for e in s.events if e.kind == "order_cancelled"]
    assert len(cancelled) == 1 and "60 minuten" in cancelled[0].message
    assert cancelled[0].time == DAY.replace(hour=15, minute=45)
    assert s.trades == [] and s.order is None


def test_fvg_outside_size_band_is_ignored():
    s = _run(_bars(ASIA_1 + LONDON_1 + AFTER_1_WIN), StrategyConfig(fvg_max=3.0))
    assert "ifvg" not in _kinds(s) and s.trades == []


def test_disrespect_after_more_than_20_minutes_is_no_setup():
    fill = [(f"09:{m:02d}", 50888, 50893, 50885, 50889) for m in range(14, 38)]
    late = [("09:38", 50889, 50899, 50886, 50897)]
    s = _run(_bars(ASIA_1 + LONDON_1[:4] + fill + late))
    assert "ifvg_too_late" in _kinds(s)
    assert "order_placed" not in _kinds(s)


def test_news_blackout_blocks_new_setups():
    cfg = StrategyConfig(news_events=(DAY.replace(hour=9, minute=30),))
    s = _run(_bars(ASIA_1 + LONDON_1 + AFTER_1_WIN), cfg)
    skipped = [e for e in s.events if e.kind == "setup_skipped"]
    assert skipped and skipped[0].data["reason"] == "nieuwsvenster"
    assert s.trades == []


def test_pending_limit_removed_at_window_end():
    shift = lambda rows, h, m: [(f"{h}:{m + k:02d}", *r[1:]) for k, r in enumerate(rows)]  # noqa: E731
    ny_late = shift(NY_2, 20, 20)  # setup forms 20:20-20:24, order placed at 20:25
    hold = [(f"20:{m}", 51100, 51105, 51095, 51100) for m in range(25, 35)]
    s = _run(_bars(ASIA_2 + LONDON_2 + ny_late + hold))
    cancelled = [e for e in s.events if e.kind == "order_cancelled"]
    assert cancelled and cancelled[0].data["reason"] == "handelsvenster dicht"
    assert cancelled[0].time == DAY.replace(hour=20, minute=30)


def test_stop_loss_then_ten_minute_pause():
    loss = [("09:16", 50906, 50907, 50897, 50899), ("09:17", 50899, 50900, 50879, 50882)]
    s = _run(_bars(ASIA_1 + LONDON_1 + loss))
    [t] = s.trades
    assert t.exit_reason == "sl" and t.pnl == pytest.approx(-16.5 * 1.81)
    assert s.cooldown_until == DAY.replace(hour=9, minute=28)
    assert s.blocked_reason(DAY.replace(hour=9, minute=20), "long") == "pauze na verlies"


def test_stop_inside_fill_candle_counts_as_loss():
    whipsaw = [("09:16", 50906, 50907, 50880, 50899)]
    s = _run(_bars(ASIA_1 + LONDON_1 + whipsaw))
    [t] = s.trades
    assert t.exit_reason == "sl"


def test_limits_one_trade_and_entries_per_session():
    s = _run(_bars(ASIA_1 + LONDON_1 + AFTER_1_WIN[:1]))
    t = DAY.replace(hour=9, minute=20)
    assert s.blocked_reason(t, "short") == "er loopt al een trade"
    s.position = None
    s.entries_session[(t.date(), "london")] = 3
    assert "3 entries deze sessie" in s.blocked_reason(t, "short")
    s.entries_session.clear()
    s.entries_day[t.date()] = 6
    assert "6 entries vandaag" in s.blocked_reason(t, "short")
    assert s.blocked_reason(DAY.replace(hour=20, minute=30), "long") == "buiten het handelsvenster"


def test_tp_modes():
    bars = _bars(ASIA_1 + LONDON_1)
    tps = {m: next(e for e in _run(bars, StrategyConfig(tp_mode=m)).events if e.kind == "order_placed").data["tp"]
           for m in ("previous", "current", "nearest", "furthest")}
    assert tps == {"previous": 50990, "current": 50911, "nearest": 50911, "furthest": 50990}


def test_backtest_summary_and_mt5_csv(tmp_path):
    rows = ASIA_1 + LONDON_1 + AFTER_1_WIN
    lines = ["<DATE>\t<TIME>\t<OPEN>\t<HIGH>\t<LOW>\t<CLOSE>\t<TICKVOL>\t<VOL>\t<SPREAD>"]
    lines += [f"2026.10.20\t{t}:00\t{o}\t{h}\t{lo}\t{c}\t10\t0\t150" for t, o, h, lo, c in rows]
    path = tmp_path / "US30_M1.csv"
    path.write_text("\n".join(lines), encoding="utf-8")
    bars = load_bars_csv(path)
    assert len(bars) == len(rows) and bars[0].time == DAY.replace(hour=8)
    summary = run_backtest(bars).summary()
    assert summary["trades"] == 1 and summary["wins"] == 1 and summary["setups"] == 1
    assert summary["net_pnl"] == pytest.approx(92.5 * 1.81)


def test_spread_widens_long_fill():
    s = _run(_bars(ASIA_1 + LONDON_1 + AFTER_1_WIN), replace(StrategyConfig(), spread=1.0))
    assert s.trades == []  # 50.897 low + 1 spread never reaches the 50.897,5 buy limit


def test_structure_and_format():
    t0 = datetime(2026, 10, 1)
    zig = [0, 4, 8, 12, 8, 4, 6, 10, 16, 20, 16, 12, 14, 18, 24, 28, 24, 20]  # HH + HL waves
    bars = []
    for k, level in enumerate(zig):
        for m in range(60):
            p = 46000 + level * 10
            bars.append(Bar(t0 + timedelta(hours=k, minutes=m), p, p + 2, p - 2, p))
    assert bias_report(bars)["H1"] == "bullish"
    assert fmt(50897.5) == "50.897,5" and fmt(51131) == "51.131"


def test_market_entry_when_price_already_past_the_limit():
    from src.us30_strategy.models import FVG

    s = _run(_bars(ASIA_1 + LONDON_1[:5]))  # IFVG confirmed, sweep low 50.884
    gap = FVG("bull", 50890, 50900, len(s.bars) - 2, len(s.bars), DAY.replace(hour=9, minute=16))
    bar = Bar(DAY.replace(hour=9, minute=15), 50897, 50899, 50893, 50895)
    s._place(bar, len(s.bars), "long", gap)
    assert s.order is None
    [t] = s.trades
    assert t.entry == 50895 and t.sl == 50881
    assert t.lots == pytest.approx(2.14)  # 30 / 14 points
    assert _kinds(s)[-1] == "market_entry"
    assert s.entries_session[(DAY.date(), "london")] == 1

    s2 = _run(_bars(ASIA_1 + LONDON_1[:5]))
    s2._place(Bar(bar.time, 50882, 50883, 50879, 50880), len(s2.bars), "long", gap)
    assert s2.trades == [] and "stop" in s2.events[-1].message


def test_limit_fills_at_better_open():
    gap_down = [("09:16", 50895, 50899, 50893, 50898), ("09:17", 50898, 50995, 50897, 50990)]
    s = _run(_bars(ASIA_1 + LONDON_1 + gap_down))
    [t] = s.trades
    assert t.entry == 50895 and t.exit_reason == "tp"


def test_news_window_removes_open_limit():
    cfg = StrategyConfig(news_events=(DAY.replace(hour=15, minute=30),))  # blackout from 14:50
    hold = [(f"14:{m}", 51100, 51105, 51095, 51100) for m in range(45, 55)]
    s = _run(_bars(ASIA_2 + LONDON_2 + NY_2 + hold), cfg)
    cancelled = [e for e in s.events if e.kind == "order_cancelled"]
    assert cancelled and cancelled[0].data["reason"] == "nieuwsvenster"
    assert cancelled[0].time == DAY.replace(hour=14, minute=50)
    assert s.trades == [] and s.entries_session[(DAY.date(), "ny")] == 0
