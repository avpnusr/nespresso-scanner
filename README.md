# nespresso-scanner

Scan the product catalogue from Nespresso and maintain it machine-readable in
this repository. It exists so [nespresso-stats](../nespresso-stats) can poll
which capsules Nespresso currently sells and hint at new ones.

## What it does

`scan.py` reads the public German Nespresso store (`nespresso.com/de/de`):

1. the sitemap for every capsule product URL (`/order/capsules/...`), then
2. each product page's embedded `window.ui.push(...)` product JSON,

and writes the result to `catalogue.json`. Python 3 standard library only, like
the dashboard. `catalogue.json` is only rewritten when the capsule list actually
changes, so the committed `generated` timestamp marks the last change.

```bash
python3 scan.py             # refresh catalogue.json
python3 scan.py --selftest  # parser check, no network
```

The scan refuses to write a partial catalogue: fewer than 20 capsules or more
than 10% of product pages failing aborts the run and leaves the old file alone.

## `catalogue.json`

```json
{
  "generated": "2026-10-15T11:54:27+00:00",
  "source": "https://www.nespresso.com/de/de/order/capsules",
  "market": "de",
  "language": "de",
  "count": 80,
  "capsules": [
    {
      "sku": "7048.20",
      "name": "Altissio",
      "line": "vertuo",            // "vertuo" | "original"
      "family": "Espresso",         // the dashboard's cup size, derived from the page
      "intensity": 9,
      "special": false,             // limited edition
      "available": true,            // orderable right now
      "price": 0.52,
      "url": "https://www.nespresso.com/de/de/order/capsules/vertuo/...",
      "image": "https://www.nespresso.com/ecom/medias/..."
    }
  ]
}
```

`sku` is Nespresso's stable product id and the reliable key for matching. `name`
is the store's display name, which Nespresso changes from time to time — the
dashboard's existing seed uses older names (e.g. `Ispirazione Firenze Arpeggio`
where the store now says `Arpeggio`), so match on `sku` or allow aliases.

## Refreshing

`.gitea/workflows/catalogue.yml` runs `scan.py` twice a day (05:00 and 17:00 UTC)
and commits `catalogue.json` when it changed. It can also be started by hand
from the Actions tab. The commit step pushes with the built-in
`${{ secrets.GITEA_TOKEN }}`; the repository needs `contents: write` (set in the
workflow) and Actions enabled.
