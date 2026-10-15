# Capsule banner images

Every catalogue capsule can have one AI-generated banner at `images/<sku>.png`,
made by hand in ChatGPT and installed by `images.py`. The dashboard polls
`catalogue.json`, so a generated banner becomes the capsule's picture in
Nespresso Stats; until one exists the store's own photo is used.

## Make a batch

```bash
python3 images.py prompts      # writes prompts/needs-images.md
```

The file lists every capsule that still has no banner. By default it only lists
capsules the dashboard does not already have its own picture for (it reads
`../nespresso-stats/capsules.json`); `--all` ignores that. Each capsule has:

- `store` / `photo` — the product page and the store's own capsule photo,
- `attach` — the line's style anchor to attach as a reference,
- `save as` — where to save the result,
- one ready-to-paste prompt.

## In ChatGPT

1. Attach **two** images: the `attach` style anchor and the `photo` store photo.
2. Paste the prompt paragraph. Use the built-in image generation at the highest
   quality the account offers.
3. Download the result and save it as the path under `save as` (a full
   1819×865 PNG; no resizing needed).

Keep the two style anchors — `active-coffee-plus.png` (Vertuo) and
`ispirazione-ristretto-italiano.png` (Original) — so every banner matches the
existing series.

## Install

```bash
python3 images.py ingest --from ~/Downloads    # looks for files named <sku>.png
```

It copies each file to `images/<sku>.png` and points `catalogue.json`'s `image`
at the generated banner, keeping the store photo as `source_image`. Commit
`images/` and `catalogue.json` together.

`scan.py` re-derives `image`/`source_image` from `images/` on every scan, so the
banner survives the twice-daily refresh, and a capsule keeps its banner when the
store renames it because the file is keyed by sku.
