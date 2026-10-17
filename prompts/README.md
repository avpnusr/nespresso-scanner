# Capsule banner images

Every catalogue capsule can have one AI-generated banner at `images/<sku>.png`,
made by `images.py generate` or by hand in ChatGPT. The dashboard polls
`catalogue.json`, so a generated banner becomes the capsule's picture in
Nespresso Stats; until one exists the store's own photo is used.

## Make a batch automatically

```bash
OPENAI_API_KEY=sk-... python3 images.py generate --limit 2
```

This is what the [workflow](../.github/workflows/catalogue.yml) runs twice a day.
It draws a banner for the capsules that still have none and installs it, and skips
capsules the dashboard already has its own picture for (`--all` ignores that, like
`prompts --all`). `--limit` counts per line and `--line vertuo|original` narrows
the run to one line. Each request carries two reference images: the line's style
anchor from `prompts/` and the capsule's own store photo. The anchors are the
series' look — replace them only with a banner you are happy to see the whole
line follow. Without an anchor for a line, its capsules are drawn from the store
photo alone.

## Make a batch by hand

```bash
python3 images.py prompts      # writes prompts/needs-images.md
```

The file lists every capsule that still has no banner. By default it only lists
capsules the dashboard does not already have its own picture for (it reads
`../nespresso-stats/capsules.json`); `--all` ignores that. Each capsule has:

- `store` / `photo` — the product page and the store's own capsule photo,
- `attach` — the line's style anchor under `prompts/` to attach as a reference,
- `save as` — where to save the result,
- one ready-to-paste prompt.

## In ChatGPT

1. Attach **two** images: the `attach` style anchor and the `photo` store photo.
2. Paste the prompt paragraph. Use the built-in image generation at the highest
   quality the account offers.
3. Download the result and save it as the path under `save as` (a full
   1819×865 PNG; no resizing needed).

Keep the two style anchors — `style-vertuo.png` and `style-original.png` — so
every banner matches the existing series. The store photo is what keeps the
capsule's shape right: an Original capsule is a small truncated cone with a foil
flange, a Vertuo one is a ribbed dome, and a reference of the wrong line will drag
the drawing to the wrong shape.

## Install

```bash
python3 images.py ingest --from ~/Downloads    # looks for files named <sku>.png
```

It copies each file to `images/<sku>.png` and points `catalogue.json`'s `image`
at the generated banner, keeping the store photo as `source_image`. Commit
`images/` and `catalogue.json` together.

Name the file after the **sku**, not the capsule name. Colombia, Kahawa Ya Congo
and Hazelnut Brownie are each sold as an Original *and* a Vertuo capsule, and a
file named after one of those is refused rather than matched to whichever capsule
came first — that is how three Vertuo banners ended up on the Original skus.

`scan.py` re-derives `image`/`source_image` from `images/` on every scan, so the
banner survives the twice-daily refresh, and a capsule keeps its banner when the
store renames it because the file is keyed by sku.
