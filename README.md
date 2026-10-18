# nespresso-scanner

> **Disclaimer**
>
> This repository is maintained with the help of AI agents. All images in it are
> AI generated and are **not** sourced from Nespresso. Nespresso and all related
> product names are trademarks of their respective owners. This project is
> unofficial and not affiliated with or endorsed by Nespresso.

Scan the product catalogue from Nespresso and maintain it machine-readable in
this repository. It exists so [nespresso-stats](https://github.com/avpnusr/nespresso-stats) can poll
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
      "image": "https://raw.githubusercontent.com/avpnusr/nespresso-scanner/main/images/7048.20.webp",
      "source_image": "https://www.nespresso.com/ecom/medias/..."
    }
  ]
}
```

`sku` is Nespresso's stable product id and the reliable key for matching. `name`
is the store's display name, which Nespresso changes from time to time — the
dashboard's existing seed uses older names (e.g. `Ispirazione Firenze Arpeggio`
where the store now says `Arpeggio`), so match on `sku` or allow aliases.
`image` is the picture a client shows and is always an AI-generated banner
(`images/<sku>.webp`). `source_image` is the Nespresso store photo, kept only as a
reference for the generator. A capsule without a generated banner yet has no
`image` at all, so a client never falls back to a Nespresso picture.

## Images

Capsule banners are AI-generated in a fixed style, one per capsule, under
`images/<sku>.webp`. `images.py` draws them with the OpenAI image API, writes the
ChatGPT prompts for capsules that still need one, and installs the results — see
[prompts/README.md](prompts/README.md).

```bash
OPENAI_API_KEY=sk-... python3 images.py generate --limit 2   # draw banners for capsules without one
python3 images.py generate --line original --limit 5          # catch up one line only
python3 images.py generate --dry-run                          # show what it would draw
```

Every capsule in `catalogue.json` that has no `images/<sku>.webp` yet is a
candidate, regardless of what the dashboard knows.

`--limit` counts per line, so a long Original backlog cannot keep Vertuo capsules
without a banner for weeks: the queue is sorted by line, and a few Originals a run
would be weeks of nothing else.

Each drawing passes two reference images to the API: the line's style anchor
(`prompts/style-vertuo.png` / `prompts/style-original.png`, carrying the series'
scene, light and typography) and the capsule's own store photo, which carries its
shape, colour and pattern. The API cannot return the series' 1819×865 exactly —
width and height must both be divisible by 16, and no such size has that ratio —
so it draws 1824×864 and the script scales that to 1819×865. WebP is written at
quality 90, about a ninth of the same banner as PNG (~2.2 MB → ~250 KB), which
matters when a dashboard loads one per capsule. That scaling and encoding is why
`generate` and `ingest` need Pillow; `scan.py` and everything else stays standard
library.

## Refreshing

`.github/workflows/catalogue.yml` runs `scan.py` twice a day (05:00 and 17:00 UTC),
draws a banner for up to five capsules per line that have none, and commits `catalogue.json`,
`images/` and `prompts/needs-images.md` when they changed. It can also be started
by hand from the Actions tab. The commit step pushes with the built-in
`${{ secrets.GITHUB_TOKEN }}`; the repository needs `contents: write` (set in the
workflow) and Actions enabled.

Banner drawing needs an `OPENAI_API_KEY` repository secret; without it the
workflow skips that step. It costs real money per image, so the workflow caps the
run at five banners per line — raise `--limit` there to work through the backlog
faster.
