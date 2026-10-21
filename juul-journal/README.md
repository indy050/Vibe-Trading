# Juuls Journal (Netlify)

Handelsjournal met P&L-kalender voor de US30-strategie. De pagina staat op
Netlify; trades staan server-side in Netlify Blobs (store `journal`), niet in de
browser.

- Site: https://juul-trade-journal.netlify.app
- `public/index.html`: de journal-pagina (herfstthema).
- `netlify/functions/api.mts`: de API op `/api/*`.

## Toegang

Elke API-call heeft `Authorization: Bearer <toegangscode>` nodig. De code is
eenmalig ingesteld via `POST /api/setup`; alleen de SHA-256-hash staat in Blobs.
Is de omgevingsvariabele `JOURNAL_TOKEN` gezet, dan geldt die. De code staat
nooit in deze repository.

| Route | Doel |
|---|---|
| `GET /api/state` | alle trades, stortingen en instellingen |
| `PUT /api/trades/:id`, `DELETE /api/trades/:id` | trade opslaan of verwijderen |
| `PUT /api/deposits/:id`, `DELETE /api/deposits/:id` | storting |
| `PUT /api/settings` | startkapitaal |
| `POST /api/import` | Trade Portal-back-up samenvoegen (bestaande trades worden overgeslagen) |
| `GET /api/export` | alles in het Trade Portal-back-upformaat |

## Juul vanuit Python

```bash
export JUUL_JOURNAL_TOKEN=...   # je toegangscode
python -m src.us30_strategy.journal --trades trades.csv --push https://juul-trade-journal.netlify.app
```

## Opnieuw deployen

Vraag de Netlify-connector om een deploy van site `juul-trade-journal` en voer
het gegeven `npx @netlify/mcp ...`-commando uit in deze map.
