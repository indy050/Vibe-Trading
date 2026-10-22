"""The council: before any order goes out, the agents give their view on the setup.

Sterre proposes (the setup already passed every rule of the plan). Vera, Ada and
Ruud react. Agreement sends the order; a veto or enough objections turn it down.
The decision itself is deterministic so the backtest and the live run agree; the
transcript is what the office feed and Telegram show.

Stances:
* ``eens``    - agrees.
* ``bezwaar`` - objection: counts toward turning the trade down.
* ``veto``    - hard stop (risk or account state): the trade is turned down outright.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .engine import fmt
from .news import Headline


@dataclass(frozen=True)
class Opinion:
    agent: str
    stance: str  # eens / bezwaar / veto
    reason: str


@dataclass
class Verdict:
    approved: bool
    opinions: list[Opinion]
    transcript: list[str] = field(default_factory=list)

    @property
    def objections(self) -> list[Opinion]:
        return [o for o in self.opinions if o.stance != "eens"]

    @property
    def summary(self) -> str:
        if self.approved and not self.objections:
            return "Iedereen eens: order gaat door."
        if self.approved:
            return f"Na overleg door, ondanks {len(self.objections)} bezwaar."
        return "Afgeslagen na overleg."


@dataclass(frozen=True)
class CouncilConfig:
    # Vera: minimum number of the four broker charts that must show the same setup.
    min_broker_match: int = 3
    # Turn the trade down from this many objections (a veto always does).
    reject_at_objections: int = 2
    # Ruud objects to a trade against the structure on these timeframes.
    bias_timeframes: tuple[str, ...] = ("H4", "H1")
    # Ada objects when a high-impact headline landed this recently.
    breaking_news_minutes: int = 15


class Council:
    def __init__(self, config: CouncilConfig | None = None,
                 vera: Callable[[datetime], int | None] | None = None,
                 bias: Callable[[str], str] | None = None,
                 headlines: Callable[[], Sequence[Headline]] | None = None,
                 risk: Callable[[dict], str | None] | None = None) -> None:
        self.cfg = config or CouncilConfig()
        self.vera, self.bias, self.headlines, self.risk = vera, bias, headlines, risk

    def opinions(self, p: dict) -> list[Opinion]:
        side_nl = "long" if p["side"] == "long" else "short"
        ops = [Opinion("Sterre", "eens",
                       f"{side_nl} volgens plan: sweep, IFVG en entry gap. Entry {fmt(p['entry'])}, SL {fmt(p['sl'])}, "
                       f"TP {fmt(p['tp'])}, {p['rr']:.1f}:1.")]

        score = self.vera(p["time"]) if self.vera else None
        if score is None:
            ops.append(Opinion("Vera", "eens", "Geen brokerdata op dit moment, ik houd me afzijdig."))
        elif score < self.cfg.min_broker_match:
            ops.append(Opinion("Vera", "bezwaar", f"Maar {score}/4 charts laten deze setup zien. Te weinig bevestiging."))
        else:
            ops.append(Opinion("Vera", "eens", f"{score}/4 charts zien hetzelfde."))

        recent = []
        if self.headlines:
            since = p["time"] - timedelta(minutes=self.cfg.breaking_news_minutes)
            recent = [h for h in self.headlines() if h.high and h.time >= since]
        if recent:
            ops.append(Opinion("Ada", "bezwaar", f"Zwaar nieuws {recent[-1].time:%H:%M}: {recent[-1].title}"))
        else:
            ops.append(Opinion("Ada", "eens", "Geen zwaar nieuws in de laatste minuten."))

        veto = self.risk(p) if self.risk else None
        against = []
        if self.bias:
            bad = "bearish" if p["side"] == "long" else "bullish"
            against = [tf for tf in self.cfg.bias_timeframes if self.bias(tf) == bad]
        if veto:
            ops.append(Opinion("Ruud", "veto", veto))
        elif against:
            ops.append(Opinion("Ruud", "bezwaar", f"{' en '.join(against)} {'is' if len(against) == 1 else 'zijn'} "
                                                  f"{'bearish' if p['side'] == 'long' else 'bullish'}: dit is een {side_nl} tegen de trend."))
        else:
            ops.append(Opinion("Ruud", "eens", f"Risico {p.get('risk_pct', 0.3):.1f}% en structuur in orde."))
        return ops

    def review(self, proposal: dict) -> Verdict:
        ops = self.opinions(proposal)
        objections = [o for o in ops if o.stance == "bezwaar"]
        vetoes = [o for o in ops if o.stance == "veto"]
        lines = [f"{o.agent}: {o.reason}" for o in ops]
        if vetoes:
            lines.append("Ruud legt een veto in. Trade afgeslagen.")
            return Verdict(False, ops, lines)
        if not objections:
            return Verdict(True, ops, lines + ["Allemaal eens. Max plaatst de order."])
        # Discussion round: Sterre answers each objection, then the count decides.
        for o in objections:
            lines.append(f"Sterre → {o.agent}: het plan zelf is compleet; is dit genoeg reden om over te slaan?")
        if len(objections) >= self.cfg.reject_at_objections:
            names = " en ".join(o.agent for o in objections)
            lines.append(f"{names} blijven bij hun bezwaar. {len(objections)} bezwaren: trade afgeslagen.")
            return Verdict(False, ops, lines)
        lines.append(f"{objections[0].agent}: bezwaar genoteerd, ik houd het niet tegen. Order gaat door onder voorbehoud.")
        return Verdict(True, ops, lines)
