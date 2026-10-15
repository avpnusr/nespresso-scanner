#!/usr/bin/env python3
"""Capsule banner images, made by hand in ChatGPT and installed here.

`catalogue.json` is machine-readable, but the dashboards want a picture per
capsule. Those are generated with ChatGPT's built-in image tool in a fixed
banner style, one 1819x865 image per capsule, and live in `images/<sku>.png`.
This script writes the prompts and installs the results. Python 3 standard
library only, like the rest.

    python3 images.py prompts                  # prompts/needs-images.md for capsules with no image
    python3 images.py prompts --all            # ignore the dashboard catalogue, list every capsule
    python3 images.py ingest --from ~/Downloads/nes-pics
    python3 images.py --selftest

Images are keyed by the store **sku**, so a capsule keeps its picture when the
store renames it. `scan.py` then points the catalogue's `image` at the generated
banner and keeps the store photo as `source_image`.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import unicodedata
from datetime import date
from pathlib import Path

import scan  # reuse the compact catalogue writer so the file format stays identical

ROOT = Path(__file__).resolve().parent
CATALOGUE = ROOT / "catalogue.json"
IMAGES = ROOT / "images"
PROMPTS = ROOT / "prompts"
STATS_CATALOGUE = ROOT.parent / "nespresso-stats" / "capsules.json"
RAW_BASE = "https://git.khmls.net/klein/nespresso-scanner/raw/branch/main"

# One style anchor per line; attach it as a reference (composition/lighting only).
REFERENCES = {
    "vertuo": "active-coffee-plus.png",
    "original": "ispirazione-ristretto-italiano.png",
}

VERTUO = (
    "Use case: ads-marketing. Generate ONE standalone high-quality premium photorealistic "
    "Nespresso Vertuo {name} banner, 1819x865 pixels, wide 2.103:1. The attached style "
    "reference is for STYLE AND COMPOSITION ONLY: match its cinematic warm sunlight, exquisite "
    "realistic materials, elegant readable cream serif heading at the left, the capsule large "
    "in the hero foreground center-right, and a clear glass mug of hot black coffee with thick "
    "golden crema behind it on the right. Subject: one metallic Vertuo dome capsule with subtle "
    "radial ribs, embossed Nespresso N and a silver barcode flange \u2014 match its exact colour, "
    "finish and decorative pattern to the attached store photo. Scene: a distinct, elegant scene "
    "that fits this capsule's origin or flavour, consistent with the reference series. Exact "
    "heading at the left: \"{name}\", split naturally into two or three lines with generous "
    "margins, then small spaced \"VERTUO\" between delicate rules below. All text legible with no "
    "overlap; capsule and glass fully within frame. No other text, no ACTIVE or COFFEE+ labels, "
    "no slogans, no health claims, no watermark; single image, no collage."
)
ORIGINAL = (
    "Use case: ads-marketing. Generate ONE standalone high-quality premium photorealistic "
    "Nespresso ORIGINAL coffee banner, 1819x865 pixels, wide 2.103:1. The attached style "
    "reference is for STYLE ONLY: match its cinematic warm sunlight, exquisite realistic "
    "materials, elegant readable cream serif heading at the left, the capsule in the hero "
    "foreground center-right, and a clear glass of hot black coffee with rich golden crema "
    "behind it on the right. Crucial capsule anatomy: ORIGINAL small tapered truncated-cone "
    "aluminum capsule, narrow flat closed end on top, broad silver circular foil flange at the "
    "base, subtly fluted sloping walls, embossed N logo \u2014 no Vertuo dome, no barcode rim; "
    "match its exact colour and pattern to the attached store photo. Scene: a distinct, elegant "
    "scene that fits this capsule's origin or flavour, consistent with the reference series. "
    "Exact heading at the left: \"{name}\", split naturally into balanced lines with generous "
    "margins, then small spaced \"ORIGINAL\" between delicate rules below. Roasted coffee beans "
    "on warm stone, shallow depth of field. All text legible with no overlap; capsule and glass "
    "fully within frame. No other text, no ice, no watermark; single image, no collage."
)


def slug(name: str) -> str:
    """Il Caffè -> il-caffe; Double Espresso Chiaro Decaffeinato -> double-espresso-chiaro-decaffeinato."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")


def has_image(entry: dict) -> bool:
    sku = entry.get("sku")
    return bool(sku) and (IMAGES / f"{sku}.png").exists()


def store_photo(entry: dict) -> str:
    return entry.get("source_image") or entry.get("image") or ""


def prompt_for(entry: dict) -> str:
    line = entry.get("line") or "vertuo"
    template = ORIGINAL if line == "original" else VERTUO
    return template.format(name=(entry.get("name") or "").upper())


def load_known(path: Path) -> set[str]:
    data = json.loads(path.read_text())
    return {(c.get("sku") or "").strip() for c in data if c.get("sku")}


def needs_image(known: set[str], every: bool) -> list[dict]:
    doc = json.loads(CATALOGUE.read_text())
    out = []
    for entry in doc["capsules"]:
        if not entry.get("available", True) or has_image(entry):
            continue
        if not every and (entry.get("sku") or "") in known:
            continue  # the dashboard already has its own picture for this capsule
        out.append(entry)
    return sorted(out, key=lambda e: (e.get("line", ""), e.get("name", "")))


def write_prompts(entries: list[dict], out: Path) -> None:
    lines = [
        "# Image generation prompts",
        "",
        f"{len(entries)} capsule(s) with no banner yet \u2014 generated {date.today().isoformat()} "
        "by `images.py prompts`.",
        "",
        "Mode: ChatGPT built-in image generation, highest quality. Final size 1819 \u00d7 865 px (wide 2.103:1).",
        "For each capsule, attach two reference images: the line's style anchor (under `attach`) and",
        "the capsule's own store photo (under `photo`). Paste the paragraph, download, and save it",
        "as the path under `save as` \u2014 then run `images.py ingest`.",
        "",
    ]
    for entry in entries:
        sku = entry.get("sku") or slug(entry.get("name", ""))
        lines += [
            f"## {entry.get('name')} \u00b7 {entry.get('line', '')} \u00b7 {entry.get('family', '')}",
            "",
            f"<!-- store: {entry.get('url', '')}",
            f"     photo: {store_photo(entry)}",
            f"     attach: {REFERENCES.get(entry.get('line', 'vertuo'), REFERENCES['vertuo'])}",
            f"     save as: images/{sku}.png -->",
            "",
            prompt_for(entry),
            "",
        ]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines))


def source_files(from_dir: Path | None, files: list[Path]) -> list[Path]:
    found = list(files)
    if from_dir:
        found += sorted(p for p in from_dir.glob("*.png") if p.is_file())
    return found


def ingest(paths: list[Path]) -> int:
    doc = json.loads(CATALOGUE.read_text())
    by_key: dict[str, dict] = {}
    for entry in doc["capsules"]:
        if entry.get("sku"):
            by_key[entry["sku"]] = entry
        by_key.setdefault(slug(entry.get("name", "")), entry)
    installed = 0
    for src in paths:
        entry = by_key.get(src.stem) or by_key.get(slug(src.stem))
        if entry is None:
            print(f"skip {src.name}: no capsule matches \u201c{src.stem}\u201d")
            continue
        sku = entry.get("sku") or slug(entry.get("name", ""))
        IMAGES.mkdir(exist_ok=True)
        shutil.copyfile(src, IMAGES / f"{sku}.png")
        entry["source_image"] = store_photo(entry)
        entry["image"] = f"{RAW_BASE}/images/{sku}.png"
        installed += 1
    if installed:
        CATALOGUE.write_text(scan.dumps(doc))
    return installed


def selftest() -> None:
    assert slug("Il Caff\u00e8") == "il-caffe"
    assert slug("Double Espresso Chiaro Decaffeinato") == "double-espresso-chiaro-decaffeinato"
    vertuo = {"sku": "7005.20", "name": "Melozio", "line": "vertuo", "family": "Mug", "image": "https://x/1.png"}
    original = {"sku": "7928.20", "name": "Arpeggio", "line": "original", "family": "Espresso"}
    assert "MELOZIO" in prompt_for(vertuo) and "VERTUO" in prompt_for(vertuo)
    assert "ORIGINAL" in prompt_for(original) and "truncated-cone" in prompt_for(original)
    # ingest keys a downloaded banner by sku and rewrites the catalogue fields
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        global CATALOGUE, IMAGES
        catalogue, images = CATALOGUE, IMAGES
        try:
            CATALOGUE, IMAGES = tmp / "catalogue.json", tmp / "images"
            CATALOGUE.write_text(scan.dumps({
                "generated": "x", "source": "s", "market": "de", "language": "de", "count": 1,
                "capsules": [dict(vertuo)],
            }))
            banner = tmp / "7005.20.png"
            banner.write_bytes(b"\x89PNG\r\n\x1a\n")
            assert ingest([banner]) == 1
            back = json.loads(CATALOGUE.read_text())["capsules"][0]
            assert back["source_image"] == "https://x/1.png", back
            assert back["image"].endswith("/images/7005.20.png"), back
            assert (IMAGES / "7005.20.png").exists()
        finally:
            CATALOGUE, IMAGES = catalogue, images
    print("selftest ok")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--selftest", action="store_true", help="check prompt/ingest logic, no files touched")
    sub = parser.add_subparsers(dest="command")
    p_prompts = sub.add_parser("prompts", help="write the ChatGPT prompt batch")
    p_prompts.add_argument("--all", action="store_true",
                           help="list every capsule without an image, not just ones the dashboard lacks")
    p_prompts.add_argument("--known", type=Path, default=STATS_CATALOGUE,
                           help="dashboard catalogue to skip (default: ../nespresso-stats/capsules.json)")
    p_prompts.add_argument("--out", type=Path, default=PROMPTS / "needs-images.md")
    p_ingest = sub.add_parser("ingest", help="install downloaded banners")
    p_ingest.add_argument("--from", dest="from_dir", type=Path, default=Path.home() / "Downloads" / "nes-pics")
    p_ingest.add_argument("--file", dest="files", type=Path, action="append", default=[])
    args = parser.parse_args()

    if args.selftest:
        selftest()
    elif args.command == "prompts":
        known = set()
        if not args.all:
            if not args.known.exists():
                raise SystemExit(f"no dashboard catalogue at {args.known} (use --all to ignore it)")
            known = load_known(args.known)
        entries = needs_image(known, args.all)
        write_prompts(entries, args.out)
        print(f"wrote {len(entries)} prompt(s) to {args.out}")
    elif args.command == "ingest":
        count = ingest(source_files(args.from_dir, args.files))
        print(f"installed {count} banner(s) into {IMAGES.relative_to(ROOT)}")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
