# US30 sweep + FVG strategie

Het handelsplan *US30 / DJ30 – Liquidity sweep + Fair Value Gap reversal* als code.
De engine leest gesloten 1-minuut candles (brokertijd IC Markets MT5) en beslist
per candle wat het plan zegt: sweep, laatste FVG, IFVG, entry gap, limit, SL en TP.
Zelf plaatst hij geen orders. Dat doet later de MT5-koppeling van Vibe-Trading,
eerst op een demo-account.

## Backtest draaien

Exporteer in MT5 de 1-minuut bars van US30 (rechtsklik op het symbool → *Bars* →
*Export*). Draai daarna vanuit de map `agent/`:

```bash
python -m src.us30_strategy US30_M1.csv --equity 10000 --spread 1.5 --log
python -m src.us30_strategy US30_M1.csv --news nieuws.csv --trades-csv trades.csv
```

`nieuws.csv` bevat één tijdstip per regel (brokertijd) van belangrijk US-nieuws.
Met `--value-per-point` geef je op wat 1 punt per lot oplevert in je
accountvaluta. In MT5 staat dat bij de specificatie van het symbool.

## Regels zoals ze in de code staan

| Plan | Code (`config.py`) |
|---|---|
| Sessies Azië 02:00-09:00, Londen 09:00-14:30, New York 14:30-23:00 | `SessionTimes` |
| Handelsvenster 09:00-20:30 | `window_start`, `window_end` |
| FVG $1-$20 | `fvg_min`, `fvg_max` |
| IFVG binnen 20 min | `ifvg_max_minutes` |
| Limit op de eerste 25%, 60 min geldig | `entry_fraction`, `order_valid_minutes` |
| SL sweep ± $3 | `sl_buffer` |
| Risico 0,3% | `risk_fraction` |
| 1 trade tegelijk, max 3 per sessie en 6 per dag | `max_entries_per_session`, `max_entries_per_day` |
| Geen entries 40 min voor tot 15 min na nieuws | `news_before_minutes`, `news_after_minutes` |
| Na verlies 10 minuten pauze | `cooldown_after_loss_minutes` |

## Keuzes waar het plan ruimte laat

1. **Take profit.** "Hoog/laag van de vorige of huidige sessie": standaard pakt de
   code het niveau van de **vorige** sessie, zoals in voorbeeld 1. Als dat niet
   voorbij de entry ligt, neemt hij het niveau van de huidige sessie. Andere
   standen: `current`, `nearest`, `furthest`.
2. **Sweep bij de leg.** De sweep telt alleen als het extreem (laagste of
   hoogste punt) ná de eerste kaars van de laatste FVG ligt. Zo hoort de sweep
   bij dezelfde beweging.
3. **Entry gap.** De engine kiest de laagste bullish FVG (bij een buy) die na de
   sweep is ontstaan. Hij wacht daar maximaal 20 minuten na de IFVG op
   (`entry_gap_max_minutes`).
4. **Koers al voorbij de entry.** Staat de koers bij het ontstaan van de entry
   gap al onder de buy-entry (of boven de sell-entry), dan stapt de engine
   **direct in tegen de marktprijs**. SL en TP blijven volgens de regels; de
   lotgrootte wordt berekend op de echte afstand tot de SL. Staat de koers al
   voorbij de SL, dan komt er geen trade (`market_entry_if_past`).
   Opent een candle na het plaatsen van de limit al voorbij de entry, dan telt
   de backtest de openingsprijs als fill, net als een markt-entry.
5. **"Entries" tellen bij een fill.** Een limit die verloopt, telt niet mee voor
   het maximum van 3 per sessie en 6 per dag.
6. **Nieuws en open order.** Begint het nieuwsvenster terwijl er een limit
   klaarstaat, dan wordt die verwijderd. Een lopende trade loopt door.
7. **Hogere timeframes.** `bias_report()` geeft de structuur op maand, week, dag,
   4H en 1H (HH+HL = bullish, LH+LL = bearish). Dat is informatie, net als in je
   plan. Met `bias_filter_timeframe="H1"` wordt het een harde filter.
8. **Backtest is streng.** Raakt één candle zowel SL als TP, dan telt het als
   verlies. Raakt de SL in de fill-candle, dan ook.

## Ada's nieuwsdesk (`news.py`)

Ada volgt twee bronnen:

- **ForexFactory**, de weekkalender (deze en volgende week). Belangrijk US-nieuws
  (USD, impact *High*) wordt omgerekend naar servertijd van IC Markets
  (New York + 7 uur, klopt in zomer- en wintertijd) en gaat als nieuwsvenster
  naar de engine via `Us30Strategy.set_news_events(...)`. De kalender wordt
  hooguit eens per uur opgehaald en lokaal bewaard voor als de site even niet
  bereikbaar is.
- **FinancialJuice**, de realtime headlines (RSS). Ada labelt wat ertoe doet voor
  US30: `data` (US-cijfers met actual/forecast/previous), `fed`, `politiek`,
  `geopolitiek` en `markt` (o.a. de 30 Dow-aandelen). Belangrijke headlines krijgen
  een `!`. Headlines zijn informatie: ze blokkeren geen trades.

```bash
python -m src.us30_strategy.news                    # ochtendbriefing + headlines
python -m src.us30_strategy.news --medium           # ook US-nieuws met Medium impact
python -m src.us30_strategy.news --save-news nieuws.csv   # bouw nieuwshistorie op voor backtests
```

ForexFactory geeft via deze feed alleen de huidige en volgende week. Draai
`--save-news` dus regelmatig (bijvoorbeeld dagelijks), dan groeit `nieuws.csv`
mee en kan de backtest er later mee werken (`--news nieuws.csv`).

## Juul, de journal-agent (`journal.py`)

Juul zet elke gesloten trade in je **Trade Portal**-journal en houdt de
P&L-kalender per dag bij. De journal bewaart alles in je browser en wisselt data
uit via de back-up. Daarom werkt Juul met dat bestand:

1. In de journal: *Back-up downloaden*.
2. Juul voegt de nieuwe trades toe (handmatige trades en stortingen blijven staan,
   dubbele trades worden overgeslagen):

   ```bash
   python -m src.us30_strategy.journal --backup trade-journal-backup.json \
       --trades trades.csv --out trade-journal-bijgewerkt.json --source demo
   ```

3. In de journal: *Back-up terugzetten* met het nieuwe bestand.
   Let op: terugzetten vervangt alles in de browser. Gebruik daarom altijd de
   nieuwste back-up als basis.

Elke trade krijgt in de notities de sessie, TP of SL, de R-waarde en
"regels gevolgd". Met `--month 2026-10` toont Juul de P&L-kalender van die maand,
inclusief je handmatige trades.
