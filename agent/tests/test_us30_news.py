"""Ada's news desk: ForexFactory calendar and FinancialJuice headlines (offline fixtures)."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

from src.us30_strategy import StrategyConfig, Us30Strategy, load_news_csv
from src.us30_strategy.news import (
    FF_NEXT_WEEK, FF_THIS_WEEK, FJ_RSS, NewsDesk, classify, morning_brief, parse_financialjuice,
    parse_forexfactory, save_events_csv, to_broker_time, us_high_impact,
)

FF = json.dumps([
    {"title": "CPI m/m", "country": "USD", "date": "2026-07-15T08:30:00-04:00", "impact": "High", "forecast": "0.3%", "previous": "0.2%"},
    {"title": "Non-Farm Employment Change", "country": "USD", "date": "2026-12-04T08:30:00-05:00", "impact": "High", "forecast": "150K", "previous": "120K"},
    {"title": "Unemployment Claims", "country": "USD", "date": "2026-07-15T08:30:00-04:00", "impact": "Medium", "forecast": "200K", "previous": "199K"},
    {"title": "German ZEW", "country": "EUR", "date": "2026-07-15T05:00:00-04:00", "impact": "High", "forecast": "", "previous": ""},
])
RSS = """<rss version="2.0"><channel><title>FinancialJuice</title>
<item><title>FinancialJuice: US CPI (MoM) Actual 0.4% (Forecast 0.3%, Previous 0.2%)</title><pubDate>Wed, 15 Jul 2026 12:30:02 GMT</pubDate><guid>1</guid></item>
<item><title>FinancialJuice: Fed's Waller: Not in a hurry to cut rates</title><pubDate>Wed, 15 Jul 2026 13:05:00 GMT</pubDate><guid>2</guid></item>
<item><title>FinancialJuice: UnitedHealth shares fall 6% premarket after guidance cut</title><pubDate>Wed, 15 Jul 2026 11:00:00 GMT</pubDate><guid>3</guid></item>
<item><title>FinancialJuice: UK Rightmove house prices rise</title><pubDate>Wed, 15 Jul 2026 07:00:00 GMT</pubDate><guid>4</guid></item>
</channel></rss>"""


def test_new_york_time_maps_to_ic_markets_server_time_in_summer_and_winter():
    assert to_broker_time(datetime.fromisoformat("2026-07-15T08:30:00-04:00")) == datetime(2026, 7, 15, 15, 30)
    assert to_broker_time(datetime.fromisoformat("2026-12-04T08:30:00-05:00")) == datetime(2026, 12, 4, 15, 30)


def test_forexfactory_us_high_impact_and_brief():
    events = parse_forexfactory(FF)
    high = us_high_impact(events)
    assert [e.title for e in high] == ["CPI m/m", "Non-Farm Employment Change"]
    assert len(us_high_impact(events, ("High", "Medium"))) == 3
    brief = morning_brief([high[0]])
    assert "15:30 CPI m/m" in brief and "14:50-15:45" in brief
    assert "geen belangrijk US-nieuws" in morning_brief([])


def test_financialjuice_tagging():
    hs = {h.id: h for h in parse_financialjuice(RSS)}
    cpi = hs["1"]
    assert cpi.categories[0] == "data" and cpi.high
    assert cpi.figures == {"actual": "0.4%", "forecast": "0.3%", "previous": "0.2%"}
    assert cpi.time == datetime(2026, 7, 15, 15, 30, 2)
    assert "fed" in hs["2"].categories
    assert "markt" in hs["3"].categories  # Dow member
    assert not hs["4"].relevant
    assert classify("Trump: new tariffs on EU from Monday")[1] is True


def test_desk_dedupes_headlines_and_falls_back_to_cache(tmp_path):
    calls = {"fail": False}

    def fetch(url):
        if url == FJ_RSS:
            return RSS.encode()
        if calls["fail"]:
            raise OSError("offline")
        return FF.encode() if url == FF_THIS_WEEK else b"[]"

    desk = NewsDesk(cache_dir=tmp_path, fetch=fetch)
    assert len(desk.refresh_calendar(datetime(2026, 7, 13))) == 4
    assert [h.id for h in desk.poll_headlines()] == ["3", "1", "2"]
    assert desk.poll_headlines() == []
    calls["fail"] = True
    fresh = NewsDesk(cache_dir=tmp_path, fetch=fetch)
    assert len(fresh.refresh_calendar()) == 4
    assert [e.title for e in fresh.todays_events(date(2026, 7, 15))] == ["CPI m/m"]
    assert FF_NEXT_WEEK  # both weeks are requested


def test_calendar_feeds_engine_blackout_and_history_csv(tmp_path):
    desk = NewsDesk(fetch=lambda url: FF.encode() if url == FF_THIS_WEEK else b"[]")
    desk.refresh_calendar()
    strat = Us30Strategy(StrategyConfig())
    strat.set_news_events(desk.news_event_times())
    cpi = datetime(2026, 7, 15, 15, 30)
    assert strat.blocked_reason(cpi - timedelta(minutes=30), "long") == "nieuwsvenster"
    assert strat.blocked_reason(cpi + timedelta(minutes=20), "long") is None
    path = tmp_path / "news.csv"
    assert save_events_csv(us_high_impact(desk.calendar), path) == 2
    assert save_events_csv(us_high_impact(desk.calendar), path) == 0
    assert load_news_csv(path) == (cpi, datetime(2026, 12, 4, 15, 30))
