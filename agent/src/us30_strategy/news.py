"""Ada's news desk: the ForexFactory calendar and FinancialJuice headlines.

* ForexFactory (weekly calendar JSON) gives the scheduled US high-impact events.
  Their broker-time blackout windows (40 min before, 15 min after) go straight into
  ``StrategyConfig.news_events``.
* FinancialJuice (RSS) gives real-time headlines. Ada tags the ones that matter for
  US30 and reads released figures (actual vs forecast). Headlines are information:
  they do not block trades unless the user adds such a rule.

IC Markets server time runs New York time + 7 hours (GMT+2 in winter, GMT+3 in
summer), so converting via America/New_York stays correct across both DST switches.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

FF_THIS_WEEK = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
FF_NEXT_WEEK = "https://nfs.faireconomy.media/ff_calendar_nextweek.json"
FJ_RSS = "https://www.financialjuice.com/feed.ashx?xy=rss"
NEW_YORK = ZoneInfo("America/New_York")
BROKER_OFFSET_FROM_NY = timedelta(hours=7)
USER_AGENT = "Mozilla/5.0 (compatible; us30-news-desk/1.0)"


def to_broker_time(dt: datetime) -> datetime:
    """Aware datetime -> naive IC Markets server time."""
    return (dt.astimezone(NEW_YORK) + BROKER_OFFSET_FROM_NY).replace(tzinfo=None)


def _fetch(url: str, timeout: float = 15.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed https URLs
        return resp.read()


# --------------------------------------------------------------------------- ForexFactory
@dataclass(frozen=True)
class CalendarEvent:
    title: str
    country: str
    impact: str  # High / Medium / Low / Holiday
    time: datetime  # broker time
    forecast: str = ""
    previous: str = ""


def parse_forexfactory(raw: bytes | str) -> list[CalendarEvent]:
    out = []
    for e in json.loads(raw):
        try:
            when = datetime.fromisoformat(e["date"])
        except (KeyError, ValueError):
            continue
        out.append(CalendarEvent(e.get("title", "").strip(), e.get("country", ""), e.get("impact", ""),
                                 to_broker_time(when), e.get("forecast", ""), e.get("previous", "")))
    return sorted(out, key=lambda ev: ev.time)


def us_high_impact(events: Iterable[CalendarEvent], impacts: Sequence[str] = ("High",)) -> list[CalendarEvent]:
    """The events the plan calls 'belangrijk Amerikaans nieuws'."""
    return [e for e in events if e.country == "USD" and e.impact in impacts]


def blackout_window(ev: CalendarEvent, before_min: int = 40, after_min: int = 15) -> tuple[datetime, datetime]:
    return ev.time - timedelta(minutes=before_min), ev.time + timedelta(minutes=after_min)


# --------------------------------------------------------------------------- FinancialJuice
_CATEGORIES: dict[str, tuple[str, ...]] = {
    "fed": ("fed", "fomc", "powell", "rate cut", "rate hike", "waller", "williams", "jefferson", "bowman",
            "goolsbee", "kashkari", "daly", "logan", "musalem", "hammack", "barr", "cook"),
    "politiek": ("trump", "white house", "tariff", "sanction", "congress", "senate", "shutdown", "treasury",
                 "bessent", "lutnick", "greer", "vance"),
    "geopolitiek": ("iran", "israel", "russia", "ukraine", "china", "north korea", "taiwan", "troops",
                    "missile", "strike", "attack", "war", "ceasefire", "houthi"),
    "markt": ("dow", "djia", "us30", "s&p", "spx", "nasdaq", "us futures", "stock futures", "wall street",
              "yields", "10-year", "vix", "earnings"),
}
# Dow 30 members by name, so company headlines that move the index are caught.
_DOW = ("apple", "amgen", "amazon", "american express", "boeing", "caterpillar", "salesforce", "cisco",
        "chevron", "disney", "goldman", "home depot", "honeywell", "ibm", "johnson & johnson", "coca-cola",
        "jpmorgan", "mcdonald", "3m", "merck", "microsoft", "nike", "nvidia", "procter", "sherwin",
        "travelers", "unitedhealth", "visa", "verizon", "walmart")
_HIGH = ("fomc", "powell", "rate decision", "nonfarm", "non-farm", "cpi", "pce", "gdp", "tariff",
         "emergency", "attack", "strike", "invasion", "default")
_FIGURES = re.compile(r"Actual\s+(?P<actual>[-+]?[\d.,]+[%KMBT]?)"
                      r"(?:.*?Forecast\s+(?P<forecast>[-+]?[\d.,]+[%KMBT]?))?"
                      r"(?:.*?Previous\s+(?P<previous>[-+]?[\d.,]+[%KMBT]?))?", re.I)


def _has(text: str, word: str) -> bool:
    return re.search(r"(?<![a-z])" + re.escape(word) + r"(?:s|es)?(?![a-z])", text) is not None


@dataclass(frozen=True)
class Headline:
    id: str
    title: str
    time: datetime  # broker time
    link: str = ""
    categories: tuple[str, ...] = ()
    high: bool = False
    figures: dict[str, str] = field(default_factory=dict)

    @property
    def relevant(self) -> bool:
        return bool(self.categories)


def classify(title: str) -> tuple[tuple[str, ...], bool, dict[str, str]]:
    t = title.lower()
    cats = [c for c, words in _CATEGORIES.items() if any(_has(t, w) for w in words)]
    if any(_has(t, w) for w in _DOW) and "markt" not in cats:
        cats.append("markt")
    m = _FIGURES.search(title)
    figures = {k: v for k, v in (m.groupdict() if m else {}).items() if v}
    is_us = t.startswith(("us ", "u.s.")) or " us " in t[:12]
    if figures and is_us:
        cats.insert(0, "data")
    high = bool(cats) and (any(_has(t, w) for w in _HIGH) or "data" in cats)
    return tuple(cats), high, figures


def parse_financialjuice(raw: bytes | str) -> list[Headline]:
    root = ET.fromstring(raw)
    out = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        title = re.sub(r"^FinancialJuice:\s*", "", title)
        try:
            when = parsedate_to_datetime(item.findtext("pubDate") or "")
        except (TypeError, ValueError):
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        cats, high, figures = classify(title)
        out.append(Headline((item.findtext("guid") or title).strip(), title, to_broker_time(when),
                            (item.findtext("link") or "").strip(), cats, high, figures))
    return sorted(out, key=lambda h: h.time)


# --------------------------------------------------------------------------- the desk
class NewsDesk:
    """Polls both sources, caches ForexFactory (they ask for gentle polling), dedupes headlines."""

    def __init__(self, cache_dir: str | Path | None = None, ff_refresh: timedelta = timedelta(hours=1),
                 fetch=_fetch) -> None:
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.ff_refresh = ff_refresh
        self.fetch = fetch
        self.calendar: list[CalendarEvent] = []
        self._ff_at: datetime | None = None
        self._seen: set[str] = set()

    def refresh_calendar(self, now: datetime | None = None) -> list[CalendarEvent]:
        now = now or datetime.now()
        if self._ff_at and now - self._ff_at < self.ff_refresh:
            return self.calendar
        events: list[CalendarEvent] = []
        for name, url in (("ff_thisweek.json", FF_THIS_WEEK), ("ff_nextweek.json", FF_NEXT_WEEK)):
            try:
                raw = self.fetch(url)
                if self.cache_dir:
                    self.cache_dir.mkdir(parents=True, exist_ok=True)
                    (self.cache_dir / name).write_bytes(raw)
            except Exception:
                cached = self.cache_dir / name if self.cache_dir else None
                if not (cached and cached.exists()):
                    continue
                raw = cached.read_bytes()
            try:
                events += parse_forexfactory(raw)
            except ValueError:
                continue
        if events:
            self.calendar = sorted({(e.time, e.title, e.country): e for e in events}.values(), key=lambda e: e.time)
            self._ff_at = now
        return self.calendar

    def todays_events(self, day: date, impacts: Sequence[str] = ("High",)) -> list[CalendarEvent]:
        return [e for e in us_high_impact(self.calendar, impacts) if e.time.date() == day]

    def news_event_times(self, impacts: Sequence[str] = ("High",)) -> tuple[datetime, ...]:
        """Ready for ``StrategyConfig.news_events`` / ``Us30Strategy.set_news_events``."""
        return tuple(e.time for e in us_high_impact(self.calendar, impacts))

    def poll_headlines(self, only_relevant: bool = True) -> list[Headline]:
        """New headlines since the last poll (call every 30-60 s)."""
        items = parse_financialjuice(self.fetch(FJ_RSS))
        fresh = [h for h in items if h.id not in self._seen]
        self._seen.update(h.id for h in items)
        return [h for h in fresh if h.relevant] if only_relevant else fresh


# --------------------------------------------------------------------------- Ada's lines
def _hm(t: datetime) -> str:
    return t.strftime("%H:%M")


def morning_brief(events: Sequence[CalendarEvent], before_min: int = 40, after_min: int = 15) -> str:
    if not events:
        return "ForexFactory: vandaag geen belangrijk US-nieuws. Het hele handelsvenster is open."
    parts = []
    for e in events:
        start, end = blackout_window(e, before_min, after_min)
        parts.append(f"{_hm(e.time)} {e.title} (geen entries {_hm(start)}-{_hm(end)})")
    return "ForexFactory vandaag: " + "; ".join(parts) + "."


def headline_line(h: Headline) -> str:
    if h.figures:
        f = h.figures
        extra = f" Verwacht {f['forecast']}" if "forecast" in f else ""
        extra += f", vorige {f['previous']}" if "previous" in f else ""
        return f"FinancialJuice: {h.title}.{extra}" if extra and "Forecast" not in h.title else f"FinancialJuice: {h.title}"
    return f"FinancialJuice [{', '.join(h.categories)}]: {h.title}"


def save_events_csv(events: Iterable[CalendarEvent], path: str | Path) -> int:
    """Append US events to a history file the backtest can read with ``--news``."""
    path = Path(path)
    known = set()
    if path.exists():
        known = {r[0] for r in csv.reader(path.read_text(encoding="utf-8").splitlines()) if r}
    new = [e for e in events if e.time.strftime("%Y-%m-%d %H:%M") not in known]
    with path.open("a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if not known:
            w.writerow(["time", "title", "impact"])
        for e in new:
            w.writerow([e.time.strftime("%Y-%m-%d %H:%M"), e.title, e.impact])
    return len(new)


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Ada's nieuwsdesk: ForexFactory-kalender en FinancialJuice-headlines.")
    ap.add_argument("--date", help="dag in brokertijd (JJJJ-MM-DD), standaard vandaag")
    ap.add_argument("--medium", action="store_true", help="ook US-nieuws met 'Medium' impact")
    ap.add_argument("--headlines", type=int, default=15, help="aantal relevante FinancialJuice-headlines")
    ap.add_argument("--save-news", help="voeg US-kalenderitems toe aan dit CSV-bestand (voor backtests)")
    args = ap.parse_args(argv)

    impacts = ("High", "Medium") if args.medium else ("High",)
    desk = NewsDesk()
    desk.refresh_calendar()
    day = date.fromisoformat(args.date) if args.date else to_broker_time(datetime.now(timezone.utc)).date()
    print(morning_brief(desk.todays_events(day, impacts)))
    week = us_high_impact(desk.calendar, impacts)
    if week:
        print("\nDeze en volgende week (brokertijd):")
        for e in week:
            print(f"  {e.time:%a %d-%m %H:%M}  {e.impact:<6} {e.title}  (verwacht {e.forecast or '-'}, vorige {e.previous or '-'})")
    if args.save_news:
        print(f"\n{save_events_csv(week, args.save_news)} nieuwe items opgeslagen in {args.save_news}")
    if args.headlines:
        print("\nFinancialJuice, relevant voor US30:")
        for h in desk.poll_headlines()[-args.headlines:]:
            print(f"  {h.time:%H:%M} {'!' if h.high else ' '} {headline_line(h)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
