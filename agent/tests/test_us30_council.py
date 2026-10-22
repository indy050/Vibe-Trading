"""The council: agents discuss a setup before the order goes out; Telegram messages."""

from __future__ import annotations

from src.us30_strategy import StrategyConfig, Us30Strategy
from src.us30_strategy.council import Council, CouncilConfig
from src.us30_strategy.news import Headline
from src.us30_strategy.notify import TelegramNotifier, format_event
from tests.test_us30_strategy import AFTER_1_WIN, ASIA_1, DAY, LONDON_1, _bars

BREAKING = Headline("1", "US troops evacuating Middle East posts - NBC", DAY.replace(hour=9, minute=10),
                    categories=("geopolitiek",), high=True)


def _run_with(council):
    s = Us30Strategy(StrategyConfig(), 10_000.0, review=council.review)
    for b in _bars(ASIA_1 + LONDON_1 + AFTER_1_WIN):
        s.on_bar(b)
    return s


def _council_event(s):
    return next(e for e in s.events if e.kind == "council")


def test_everyone_agrees_order_goes_out():
    s = _run_with(Council(vera=lambda t: 4, bias=lambda tf: "bullish", headlines=lambda: []))
    ev = _council_event(s)
    assert ev.data["approved"] and ev.data["objections"] == 0
    assert any(line.startswith("Vera: 4/4") for line in ev.data["transcript"])
    assert len(s.trades) == 1


def test_two_objections_turn_the_trade_down_after_discussion():
    s = _run_with(Council(vera=lambda t: 2, headlines=lambda: [BREAKING]))
    ev = _council_event(s)
    assert not ev.data["approved"] and ev.data["objections"] == 2
    t = ev.data["transcript"]
    assert any(line.startswith("Sterre → Vera") for line in t) and any(line.startswith("Sterre → Ada") for line in t)
    assert "afgeslagen" in t[-1]
    assert s.trades == [] and s.order is None
    assert any(e.kind == "setup_skipped" and e.data["reason"] == "afgeslagen na overleg" for e in s.events)


def test_single_objection_goes_ahead_under_reservation():
    s = _run_with(Council(vera=lambda t: 4, bias=lambda tf: "bearish" if tf == "H4" else "range"))
    ev = _council_event(s)
    assert ev.data["approved"] and ev.data["objections"] == 1
    assert "H4 is bearish" in " ".join(ev.data["transcript"]) and "onder voorbehoud" in ev.data["transcript"][-1]
    assert len(s.trades) == 1


def test_ruud_veto_and_stricter_threshold():
    s = _run_with(Council(vera=lambda t: 4, risk=lambda p: "Account-sync klopt niet met MT5."))
    assert not _council_event(s).data["approved"] and s.trades == []
    strict = _run_with(Council(CouncilConfig(reject_at_objections=1), vera=lambda t: 2))
    assert not _council_event(strict).data["approved"]


def test_telegram_messages():
    s = _run_with(Council(vera=lambda t: 2, headlines=lambda: [BREAKING]))
    text = format_event(_council_event(s))
    assert text.startswith("🗣️ Overleg US30 09:15") and "afgeslagen" in text
    ok = _run_with(Council(vera=lambda t: 4))
    assert format_event(_council_event(ok)) is None  # unanimous: the order message is enough
    msgs = [format_event(e) for e in ok.events if format_event(e)]
    assert msgs[0].startswith("📝 US30 09:15 · Buy limit 50.897,5") and msgs[-1].startswith("🏁")

    sent = []

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"{}"

    def opener(req, timeout):
        sent.append((req.full_url, req.data))
        return Resp()

    n = TelegramNotifier("123:abc", "42", opener=opener)
    n.on_event(_council_event(s))
    assert sent[0][0] == "https://api.telegram.org/bot123:abc/sendMessage" and b'"chat_id": "42"' in sent[0][1]

    def broken(req, timeout):
        raise OSError("offline")

    TelegramNotifier("123:abc", "42", opener=broken).on_event(_council_event(s))  # never raises
