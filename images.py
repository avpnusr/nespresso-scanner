#!/usr/bin/env python3
"""Capsule banner images, in a fixed banner style, one 1819x865 image per
capsule, living in `images/<sku>.png`.

`catalogue.json` is machine-readable, but the dashboards want a picture per
capsule. This script draws those banners with the OpenAI image API, writes the
prompt batches for making them by hand in ChatGPT, and installs the results.
Python 3 standard library only, except the `generate` command which needs
Pillow to scale the API's output to the banner size.

    python3 images.py generate --limit 2      # draw banners for new capsules, needs OPENAI_API_KEY
    python3 images.py generate --dry-run      # show what would be drawn, no API call
    python3 images.py prompts                 # prompts/needs-images.md for capsules with no image
    python3 images.py prompts --all           # ignore the dashboard catalogue, list every capsule
    python3 images.py ingest --from ~/Downloads/nes-pics
    python3 images.py --selftest

Drawing a banner attaches two reference images: the line's style anchor
(`prompts/style-vertuo.png` / `prompts/style-original.png`) and the capsule's own
store photo. The store photo is what keeps the capsule's shape, colour and
pattern right; the anchor keeps the scene, lighting and typography of the series.

Images are keyed by the store **sku**, so a capsule keeps its picture when the
store renames it. `scan.py` then points the catalogue's `image` at the generated
banner and keeps the store photo as `source_image`.
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import shutil
import tempfile
import unicodedata
import urllib.error
import urllib.request
import uuid
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
    "vertuo": "style-vertuo.png",
    "original": "style-original.png",
}

# The image API rejects the banner's own 1819x865 (width and height must both be
# divisible by 16 and no divisible size has that exact ratio), so it draws the
# nearest landscape size and `banner()` scales it down: 0.3%, invisible.
API_SIZE = "1824x864"
BANNER_SIZE = (1819, 865)
QUALITY = "high"
MODEL = "gpt-image-2.5-sunburst"
EDITS_URL = "https://api.openai.com/v1/images/edits"
USER_AGENT = "nespresso-scanner/1.0 (+https://git.khmls.net/klein/nespresso-scanner)"

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


# The image model invents a colour from the capsule's name when nothing pins it (the
# Festive banners came out champagne/violet/brown instead of the real green/blue/pink),
# so the prompt states the colour sampled from the store photo, by word and by hex.
NAMED_COLOURS = [
    ((0x3a, 0x3a, 0x3a), "charcoal grey"),
    ((0x4a, 0x2c, 0x1a), "dark chocolate brown"),
    ((0x6b, 0x42, 0x26), "deep warm brown"),
    ((0xa1, 0x67, 0x30), "caramel bronze"),
    ((0xd9, 0xb9, 0x81), "champagne gold"),
    ((0xef, 0xe0, 0xc8), "cream"),
    ((0xb8, 0xb8, 0xb8), "silver"),
    ((0x6e, 0x24, 0x36), "burgundy"),
    ((0xa3, 0x40, 0x2e), "brick red"),
    ((0xc9, 0x6f, 0x45), "terracotta"),
    ((0x75, 0x98, 0x08), "olive green"),
    ((0x3d, 0x52, 0x26), "dark forest green"),
    ((0x89, 0xb8, 0x83), "soft sage green"),
    ((0x2e, 0x8b, 0x57), "emerald green"),
    ((0x2a, 0x7f, 0x76), "teal"),
    ((0x5b, 0x7d, 0x99), "steel blue"),
    ((0x80, 0x9b, 0xb3), "dusty sky blue"),
    ((0x22, 0x34, 0x4f), "navy blue"),
    ((0x5a, 0x4a, 0x8a), "indigo violet"),
    ((0x7a, 0x4a, 0x8f), "purple"),
    ((0xb2, 0x79, 0x90), "dusty rose pink"),
    ((0xe0, 0x7a, 0x9a), "bright pink"),
]


def colour_name(rgb: tuple[int, int, int]) -> str:
    """Nearest named colour — a prompt-friendly word for a sampled hex."""
    r, g, b = rgb
    return min(NAMED_COLOURS,
               key=lambda c: (r - c[0][0]) ** 2 + (g - c[0][1]) ** 2 + (b - c[0][2]) ** 2)[1]


def dominant_colour(png: bytes) -> tuple[int, int, int] | None:
    """The capsule's own colour, averaged from the store packshot, skipping the white
    background, shadows and black barcode flange. Pillow is a generator-only need, so a
    missing install just means no colour pin in the prompt."""
    try:
        from PIL import Image
    except ImportError:
        return None
    with Image.open(io.BytesIO(png)) as img:
        img = img.convert("RGB")
        img.thumbnail((64, 64))
        r = g = b = n = 0
        for px in img.getdata():
            if min(px) > 210 or max(px) < 35:
                continue
            r += px[0]
            g += px[1]
            b += px[2]
            n += 1
    if not n:
        return None
    return r // n, g // n, b // n


def prompt_for(entry: dict, colour: tuple[int, int, int] | None = None) -> str:
    """The banner prompt: the template names the capsule and the series; the taste text
    from the product page steers the scene, and a sampled colour stops the model from
    inventing one from the name."""
    line = entry.get("line") or "vertuo"
    template = ORIGINAL if line == "original" else VERTUO
    prompt = template.format(name=(entry.get("name") or "").upper())
    profile = (entry.get("description") or "").strip()
    if profile:
        prompt += f" Flavour and origin from the Nespresso product page: {profile}"
        ingredients = (entry.get("ingredients") or "").strip()
        if ingredients:
            prompt += f" (ingredients: {ingredients})"
        prompt += "."
    if colour:
        r, g, b = colour
        prompt += (f" The capsule's colour is {colour_name(colour)}"
                   f" (#{r:02x}{g:02x}{b:02x}), sampled from the attached store photo —"
                   " the photo is the capsule itself, so recreate the scene around it and"
                   " never invent or shift its colour from the capsule's name.")
    return prompt


def fetch(url: str) -> bytes:
    """Download a store photo (the CDN wants a user agent)."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=180) as response:
        return response.read()


def multipart(prompt: str, files: list[tuple[str, bytes]], size: str, quality: str,
              model: str) -> tuple[bytes, str]:
    """Build the `POST /v1/images/edits` body: one prompt, N reference images."""
    boundary = uuid.uuid4().hex
    chunks: list[bytes] = []

    def field(name: str, value: str) -> None:
        chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"'
                      f'\r\n\r\n{value}\r\n'.encode())

    def image(name: str, filename: str, data: bytes) -> None:
        chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
                      f'filename="{filename}"\r\nContent-Type: image/png\r\n\r\n'.encode())
        chunks.append(data + b"\r\n")

    field("model", model)
    field("prompt", prompt)
    field("size", size)
    field("quality", quality)
    field("n", "1")
    for filename, data in files:
        image("image[]", filename, data)
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def edit(prompt: str, files: list[tuple[str, bytes]], key: str, size: str = API_SIZE,
         quality: str = QUALITY, model: str = MODEL) -> bytes:
    """Draw one banner and return the PNG bytes the API hands back."""
    body, content_type = multipart(prompt, files, size, quality, model)
    request = urllib.request.Request(EDITS_URL, data=body, headers={
        "Authorization": f"Bearer {key}", "Content-Type": content_type})
    try:
        with urllib.request.urlopen(request, timeout=900) as response:
            doc = json.load(response)
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"image API refused the request ({exc.code}): {exc.read().decode()[:400]}")
    print(f"  {doc.get('usage', {}).get('total_tokens', '?')} tokens")
    return base64.b64decode(doc["data"][0]["b64_json"])


def banner(png: bytes) -> bytes:
    """Scale the API's 1824x864 output to the 1819x865 the rest of the series uses."""
    from PIL import Image  # only the generator needs it; the rest of the repo stays stdlib
    with Image.open(io.BytesIO(png)) as image:
        out = io.BytesIO()
        image.convert("RGB").resize(BANNER_SIZE, Image.LANCZOS).save(out, "PNG", optimize=True)
        return out.getvalue()


def references(entry: dict) -> list[tuple[str, bytes]]:
    """The line's style anchor (when installed) plus the capsule's own store photo.

    The store photo comes last and carries the capsule's anatomy, colour and
    pattern; the anchor only carries the series' scene, light and typography.
    """
    out: list[tuple[str, bytes]] = []
    anchor = PROMPTS / REFERENCES.get(entry.get("line") or "vertuo", "")
    if anchor.is_file():
        out.append((anchor.name, anchor.read_bytes()))
    photo = store_photo(entry)
    if photo:
        out.append(("store.png", fetch(photo)))
    return out


def per_line(entries: list[dict], limit: int) -> list[dict]:
    """Cap how many banners a run draws per line.

    The queue is sorted by line, so a long Original backlog would otherwise be
    drawn to the end before a single Vertuo capsule is reached.
    """
    picked: list[dict] = []
    counts: dict[str, int] = {}
    for entry in entries:
        line = entry.get("line") or "vertuo"
        if counts.get(line, 0) < limit:
            counts[line] = counts.get(line, 0) + 1
            picked.append(entry)
    return picked


def generate(entries: list[dict], limit: int, key: str, *, dry_run: bool = False,
             size: str = API_SIZE, quality: str = QUALITY, model: str = MODEL) -> int:
    """Draw a banner for each entry (up to `limit` per line) and install it."""
    if not dry_run:
        try:
            import PIL  # noqa: F401  only the generator needs it
        except ImportError:
            raise SystemExit("drawing banners needs Pillow: python3 -m pip install pillow")
    installed = 0
    for entry in per_line(entries, limit):
        try:
            files = references(entry)
        except (urllib.error.URLError, OSError) as exc:  # dead store photo: skip this capsule, keep going
            print(f"{entry.get('sku')}  {entry.get('name')}: store photo unavailable ({exc}); skipped")
            continue
        print(f"{entry.get('sku')}  {entry.get('name')}  ({len(files)} reference(s))")
        store = next((data for name, data in files if name == "store.png"), None)
        colour = dominant_colour(store) if store else None
        if colour:
            print(f"  colour pin: #{colour[0]:02x}{colour[1]:02x}{colour[2]:02x} {colour_name(colour)}")
        if dry_run:
            continue
        if not files:
            print("  no reference images; skipped")
            continue
        # ingest() keys a banner by its file name, so stage it as <sku>.png
        staged = Path(tempfile.mkdtemp()) / f"{entry['sku']}.png"
        staged.write_bytes(banner(edit(prompt_for(entry, colour), files, key, size, quality, model)))
        installed += ingest([staged])
    return installed


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
            f"     attach: prompts/{REFERENCES.get(entry.get('line', 'vertuo'), REFERENCES['vertuo'])}",
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
    by_key: dict[str, dict | None] = {}
    for entry in doc["capsules"]:
        if entry.get("sku"):
            by_key[entry["sku"]] = entry
        # A name is only a usable key while it is unique: Colombia, Kahawa Ya Congo and
        # Hazelnut Brownie are each sold as an Original *and* a Vertuo capsule. Matching
        # one of those by name put the Vertuo banner on the Original sku, so a shared
        # name is marked as ambiguous instead of resolved to whichever came first.
        name = slug(entry.get("name", ""))
        by_key[name] = entry if name not in by_key else None
    installed = 0
    for src in paths:
        key = src.stem if src.stem in by_key else slug(src.stem)
        entry = by_key.get(key)
        if entry is None:
            print(f"skip {src.name}: no single capsule matches \u201c{src.stem}\u201d "
                  f"(a name both lines share matches none) \u2014 name the file after the sku")
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
    # the taste and ingredient text from the product page steers the scene…
    entry = {"name": "Crystal Spark", "line": "vertuo",
             "description": "geb\u00e4ckartigen und fruchtigen Noten",
             "ingredients": "Gemahlener R\u00f6stkaffee"}
    p = prompt_for(entry)
    assert "geb\u00e4ckartigen und fruchtigen Noten" in p and "Gemahlener R\u00f6stkaffee" in p
    assert "Flavour" not in prompt_for({"name": "X", "line": "vertuo"})
    # …and a sampled colour pins the capsule, so the model cannot invent one from the name
    assert colour_name((0x89, 0xb8, 0x83)) == "soft sage green"
    assert colour_name((0x6b, 0x42, 0x26)) == "deep warm brown"
    assert "colour is soft sage green (#89b883)" in prompt_for({"name": "X", "line": "vertuo"},
                                                                 (0x89, 0xb8, 0x83))
    assert "#89b883" not in prompt_for({"name": "X", "line": "vertuo"})
    # the per-run cap is per line, so a long Original backlog cannot starve Vertuo
    backlog = ([{"sku": f"o{i}", "line": "original", "name": f"o{i}"} for i in range(5)]
               + [{"sku": f"v{i}", "line": "vertuo", "name": f"v{i}"} for i in range(3)])
    assert [e["sku"] for e in per_line(backlog, 2)] == ["o0", "o1", "v0", "v1"]
    # the image API body carries one prompt and every reference image
    body, content_type = multipart("hello", [("style-vertuo.png", b"a"), ("store.png", b"b")],
                                   API_SIZE, QUALITY, MODEL)
    assert content_type.startswith("multipart/form-data; boundary=")
    assert b'name="prompt"\r\n\r\nhello' in body, body[:200]
    assert body.count(b'name="image[]"') == 2 and API_SIZE.encode() in body
    assert body.endswith(b"--\r\n")
    # the API size is not the banner size, so its output has to be scaled to it
    assert BANNER_SIZE == (1819, 865) and API_SIZE != "1819x865"
    try:
        from PIL import Image
    except ImportError:
        pass  # only the generator needs it
    else:
        drawn = io.BytesIO()
        Image.new("RGB", (1824, 864)).save(drawn, "PNG")
        with Image.open(io.BytesIO(banner(drawn.getvalue()))) as scaled:
            assert scaled.size == BANNER_SIZE, scaled.size
        # the packshot's white background must not wash out the sampled capsule colour
        pack = io.BytesIO()
        Image.new("RGB", (64, 64), (255, 255, 255)).save(pack, "PNG")
        assert dominant_colour(pack.getvalue()) is None, "a blank packshot pins no colour"
        pack = io.BytesIO()
        img = Image.new("RGB", (64, 64), (255, 255, 255))
        for x in range(16, 48):
            for y in range(8, 56):
                img.putpixel((x, y), (137, 184, 131))
        img.save(pack, "PNG")
        assert dominant_colour(pack.getvalue()) == (137, 184, 131)
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
            png = tmp / "7005.20.png"
            png.write_bytes(b"\x89PNG\r\n\x1a\n")
            assert ingest([png]) == 1
            back = json.loads(CATALOGUE.read_text())["capsules"][0]
            assert back["source_image"] == "https://x/1.png", back
            assert back["image"].endswith("/images/7005.20.png"), back
            assert (IMAGES / "7005.20.png").exists()
            # a name shared by an Original and a Vertuo capsule must not be guessed at
            CATALOGUE.write_text(scan.dumps({
                "generated": "x", "source": "s", "market": "de", "language": "de", "count": 2,
                "capsules": [
                    {"sku": "7934.20", "name": "Kahawa Ya Congo", "line": "original",
                     "image": "https://x/ol.png"},
                    {"sku": "7061.20", "name": "Kahawa Ya Congo", "line": "vertuo",
                     "image": "https://x/vl.png"}],
            }))
            ambiguous = tmp / "kahawa-ya-congo.png"
            ambiguous.write_bytes(b"\x89PNG\r\n\x1a\n")
            assert ingest([ambiguous]) == 0, "an ambiguous name must not be guessed at"
            exact = tmp / "7061.20.png"
            exact.write_bytes(b"\x89PNG\r\n\x1a\n")
            assert ingest([exact]) == 1
            got = {c["sku"]: c for c in json.loads(CATALOGUE.read_text())["capsules"]}
            assert "source_image" not in got["7934.20"], got["7934.20"]
            assert got["7061.20"]["image"].endswith("/images/7061.20.png"), got["7061.20"]
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
    p_generate = sub.add_parser("generate", help="draw banners with the OpenAI image API")
    p_generate.add_argument("--limit", type=int, default=2, help="how many banners to draw per line (default 2)")
    p_generate.add_argument("--line", choices=("vertuo", "original"),
                            help="only this line (default: both)")
    p_generate.add_argument("--sku", action="append", default=[], metavar="SKU",
                            help="draw only these sku(s), overwriting their banners"
                                 " (ignores the cost filter and the per-line cap)")
    p_generate.add_argument("--all", action="store_true",
                            help="draw for every capsule without a banner, not just ones the dashboard lacks")
    p_generate.add_argument("--known", type=Path, default=STATS_CATALOGUE,
                            help="dashboard catalogue to skip (default: ../nespresso-stats/capsules.json)")
    p_generate.add_argument("--quality", default=QUALITY)
    p_generate.add_argument("--model", default=MODEL)
    p_generate.add_argument("--size", default=API_SIZE,
                            help=f"size to request from the API, scaled to 1819x865 (default {API_SIZE})")
    p_generate.add_argument("--dry-run", action="store_true",
                            help="list what would be drawn, without calling the API")
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
    elif args.command == "generate":
        key = os.environ.get("OPENAI_API_KEY", "")
        if not key and not args.dry_run:
            raise SystemExit("set OPENAI_API_KEY to draw banners")
        if args.sku:
            wanted = set(args.sku)
            entries = [e for e in json.loads(CATALOGUE.read_text())["capsules"]
                       if e.get("sku") in wanted]
            limit = len(entries)  # --sku names exactly what to (re)draw, no per-line cap
        else:
            # the dashboard catalogue is only a cost filter, so a missing one means more work, not an error
            known, every = (load_known(args.known), False) if args.known.exists() and not args.all else (set(), True)
            entries = [e for e in needs_image(known, every)
                       if not args.line or (e.get("line") or "vertuo") == args.line]
            limit = args.limit
        count = generate(entries, limit, key, dry_run=args.dry_run, size=args.size,
                         quality=args.quality, model=args.model)
        if args.dry_run:
            print(f"{len(per_line(entries, limit))} of {len(entries)} capsule(s) would be drawn")
        else:
            print(f"installed {count} banner(s) into {IMAGES.relative_to(ROOT)}")
    elif args.command == "ingest":
        count = ingest(source_files(args.from_dir, args.files))
        print(f"installed {count} banner(s) into {IMAGES.relative_to(ROOT)}")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
