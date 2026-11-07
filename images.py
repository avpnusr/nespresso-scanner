#!/usr/bin/env python3
"""Capsule banner images, in a fixed banner style, one 1819x865 image per
capsule, living in `images/<sku>.webp`.

`catalogue.json` is machine-readable, but the dashboards want a picture per
capsule. This script draws those banners with the OpenAI image API, writes the
prompt batches for making them by hand in ChatGPT, and installs the results.
Python 3 standard library only, except `generate` and `ingest`, which need
Pillow to scale the API's output and to encode WebP — the same banners as PNG
were ~9x the size, which a dashboard loads once per capsule.

    python3 images.py generate --limit 2      # draw banners for new capsules, needs OPENAI_API_KEY
    python3 images.py generate --dry-run      # show what would be drawn, no API call
    python3 images.py prompts                 # prompts/needs-images.md for capsules with no image
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
import http.client
import io
import json
import os
import re
import sys
import tempfile
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
import warnings
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import scan  # reuse the compact catalogue writer so the file format stays identical

ROOT = Path(__file__).resolve().parent
CATALOGUE = ROOT / "catalogue.json"
IMAGES = ROOT / "images"
PROMPTS = ROOT / "prompts"
RAW_BASE = scan.RAW_BASE  # one source of truth: ingest() and the scanner must agree

# One style anchor per line; attach it as a reference (composition/lighting only).
REFERENCES = {
    "vertuo": "style-vertuo.png",
    "original": "style-original.png",
}

# The image API rejects the banner's own 1819x865: it only accepts a fixed set of
# sizes (width and height divisible by 16), none of which has that exact ratio, so
# it draws the nearest landscape size and `banner()` scales it down: 0.3%, invisible.
API_SIZE = "1824x864"
BANNER_SIZE = (1819, 865)
# WebP at this quality is ~9% of the PNG size, with the heading text still crisp.
WEBP_QUALITY = 90
SUFFIX = ".webp"
QUALITY = "high"
MODEL = "gpt-image-2.5-sunburst"
# A redraw names capsules explicitly, but every name is a paid drawing: cap the batch here so
# no caller (dispatch, shell glob, script) can start an unbounded run.
MAX_REDRAW = 10
EDITS_URL = "https://api.openai.com/v1/images/edits"
USER_AGENT = "nespresso-scanner/1.0 (+https://github.com/avpnusr/nespresso-scanner)"

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
    return bool(sku) and (IMAGES / f"{sku}{SUFFIX}").exists()


def store_photo(entry: dict) -> str:
    """The store's own packshot. Never the banner: that would feed the generator its own
    output as the reference, and a banner is not a capsule photo."""
    return entry.get("source_image") or ""


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
        # RGBA whenever there is alpha: converting straight to RGB keeps the invisible pixels'
        # stored RGB, so a grey transparent background pins "silver" for every capsule
        if "A" in img.getbands() or img.info.get("transparency") is not None:
            img = img.convert("RGBA")
        else:
            img = img.convert("RGB")
        img.thumbnail((64, 64))
        r = g = b = n = 0
        # getdata() is deprecated in Pillow 12.1, which is also where its replacement arrived
        pixels = getattr(img, "get_flattened_data", img.getdata)()
        for px in pixels:
            if len(px) == 4 and px[3] < 128:  # invisible
                continue
            if min(px[:3]) > 210 or max(px[:3]) < 35:
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
    profile = (entry.get("description") or "").strip().rstrip(".")
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


def _on_store(url: str) -> bool:
    """Same origin as the store (scheme, host without a trailing dot, effective port), for a
    redirect target or a catalogue value."""
    if not isinstance(url, str):
        return False
    try:
        wanted = urllib.parse.urlsplit(url)
        base = urllib.parse.urlsplit(scan.BASE)
        return bool(wanted.hostname) and _origin(wanted) == _origin(base)
    except ValueError:  # a malformed port or host is never the store
        return False


def _origin(parts: urllib.parse.SplitResult) -> tuple[str, str, int]:
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").rstrip(".").lower()
    port = parts.port if parts.port is not None else (443 if scheme == "https" else 80)
    return scheme, host, port


class StoreOnlyRedirects(urllib.request.HTTPRedirectHandler):
    """The store photo is fetched in CI and its bytes go to the image API, so a redirect must
    not carry that request off the store: store_url() only checked the first hop."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _on_store(newurl):
            raise urllib.error.HTTPError(newurl, code, f"redirect off the store: {newurl}",
                                          headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


STORE_OPENER = urllib.request.build_opener(StoreOnlyRedirects)


def fetch(url: str) -> bytes:
    """Download a store photo (the CDN wants a user agent)."""
    if not _on_store(url):
        # catalogue values are untrusted input too, not only what scan.py just wrote
        raise ValueError(f"not a store url: {url}")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with STORE_OPENER.open(request, timeout=180) as response:
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
        raise RuntimeError(f"image API refused the request ({exc.code}): {exc.read().decode()[:400]}")
    try:
        image = base64.b64decode(doc["data"][0]["b64_json"])
        usage = (doc.get("usage") or {}).get("total_tokens", "?")
    except (AttributeError, IndexError, KeyError, TypeError) as exc:
        # a 200 without an image is this capsule's failure, not a reason to stop the queue
        raise RuntimeError(f"image API returned no image ({type(exc).__name__})")
    print(f"  {usage} tokens")
    return image


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
    installed = failed = no_photo = would = 0
    for entry in per_line(entries, limit):
        sku, name = entry.get("sku"), entry.get("name")
        if not store_photo(entry):
            # the anchor carries the series' style, not the capsule: drawn from it alone, the
            # banner invents shape and colour and then stays published as the capsule's picture
            print(f"{sku}  {name}: no store photo; skipped")
            continue
        try:
            files = references(entry)
        except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as exc:
            # a dead store photo costs no API call: one is that capsule's bad luck, but three with
            # nothing drawn means the CDN or the allowlist broke, so count them apart
            print(f"{sku}  {name}: store photo unavailable ({exc}); skipped")
            failed += 1
            no_photo += 1
            continue
        try:
            store = next((data for filename, data in files if filename == "store.png"), None)
            colour = dominant_colour(store) if store else None
            print(f"{sku}  {name}  ({len(files)} reference(s))")
            if colour:
                print(f"  colour pin: #{colour[0]:02x}{colour[1]:02x}{colour[2]:02x} {colour_name(colour)}")
            if dry_run:
                would += 1
                continue
            # ingest() keys a banner by its file name, so stage it as <sku>.png; the temp dir is
            # per capsule and removed with it, or every drawing leaves a full-size PNG behind
            with tempfile.TemporaryDirectory() as tmp:
                staged = Path(tmp) / f"{sku}.png"
                staged.write_bytes(banner(edit(prompt_for(entry, colour), files, key, size, quality, model)))
                if ingest([staged]):
                    installed += 1
                else:  # drawn and paid for, but nothing installed it: a broken catalogue or images/
                    failed += 1
        except (urllib.error.URLError, OSError, ValueError, KeyError, RuntimeError,
                http.client.HTTPException) as exc:
            # a dead photo or a refused drawing is this capsule's problem, not the run's: the
            # queue is sorted by line, so aborting here would starve every capsule after it
            print(f"{sku}  {name}: {exc}; skipped")
            failed += 1
    # a refused drawing or an install that did nothing means the pipeline is broken, not one
    # capsule; photos are different, so it takes several before blaming the store
    if not installed and not dry_run and (failed > no_photo or no_photo >= 3):
        raise SystemExit(f"no banner was drawn ({failed} failed, {no_photo} without a photo)"
                         " — the pipeline, not one capsule")
    return would if dry_run else installed


def own_descriptions(entries: list[dict], doc: dict) -> list[dict]:
    """Drop a description that several capsules share: that is a range blurb, not this
    capsule's taste text, and in a prompt it steers every banner in the range the same way."""
    counts = Counter((c.get("description") or "").strip() for c in doc["capsules"])
    shared = {d for d, n in counts.items() if d and n > 1}
    return [dict(e, description="") if (e.get("description") or "").strip() in shared else e
            for e in entries]


def needs_image() -> list[dict]:
    doc = json.loads(CATALOGUE.read_text())
    queue = [e for e in doc["capsules"]
             # a capsule with no store photo cannot be drawn (the anchor alone invents it), so it
             # must not hold a queue slot or sit in the backlog file forever
             if e.get("available", True) and not has_image(e) and store_photo(e)]
    return sorted(own_descriptions(queue, doc),
                  key=lambda e: (e.get("line", ""), e.get("name", "")))


def markdown_safe(text: str) -> str:
    """Store text lands in a committed file that Gitea renders: a literal --> in it would close
    the comment early, and [![](...)](...) would fetch a remote picture when the file is viewed."""
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace("[", "&#91;").replace("]", "&#93;").replace("!", "&#33;")
            .replace("|", "&#124;").replace("`", "&#96;"))


def comment_safe(text: str) -> str:
    """Inside the HTML comment only a literal > can do harm; escaping more would wreck a URL
    someone copies out of it (?a=1&amp;b=2)."""
    return text.replace(">", "&gt;")


def write_prompts(entries: list[dict], out: Path) -> None:
    lines = [
        "# Image generation prompts",
        "",
        f"{len(entries)} capsule(s) with no banner yet. Written by `images.py prompts`.",
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
            f"## {markdown_safe(entry.get('name') or '')} \u00b7 {markdown_safe(entry.get('line') or '')}"
            f" \u00b7 {markdown_safe(entry.get('family') or '')}",
            "",
            f"<!-- store: {comment_safe(entry.get('url') or '')}",
            f"     photo: {comment_safe(store_photo(entry))}",
            f"     attach: prompts/{REFERENCES.get(entry.get('line') or 'vertuo', REFERENCES['vertuo'])}",
            f"     save as: ~/Downloads/nes-pics/{comment_safe(sku)}.png -->",
            "",
            "````text",
            # a closing fence has to start a line, so store text cannot end the block
            prompt_for(entry).replace("\r", " ").replace("\n", " "),
            "````",
            "",
        ]
    out.parent.mkdir(parents=True, exist_ok=True)
    # atomic: a failure mid-write must not leave a truncated prompt list for the commit
    staged = out.with_name(f".{out.name}.{os.getpid()}.tmp")
    staged.write_text("\n".join(lines))
    os.replace(staged, out)


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
        dest = IMAGES / f"{sku}{SUFFIX}"
        try:
            from PIL import Image
        except ImportError:
            raise SystemExit("installing a banner needs Pillow to write WebP: "
                             "python3 -m pip install pillow")
        # a hand-saved banner can be a few pixels off the series size (the API path
        # is scaled in banner()); normalise the manual path the same way
        # src can be the target itself (re-normalising an installed banner), so
        # stage the re-encode before replacing it
        staged = dest.with_name(f".{sku}.tmp")
        try:
            with Image.open(src) as image:
                if image.size != BANNER_SIZE:
                    print(f"{src.name}: {image.width}x{image.height} scaled to "
                          f"{BANNER_SIZE[0]}x{BANNER_SIZE[1]}")
                image.convert("RGB").resize(BANNER_SIZE, Image.LANCZOS).save(
                    staged, "WEBP", quality=WEBP_QUALITY, method=6)
            staged.replace(dest)
        except (OSError, Image.DecompressionBombError) as exc:  # unreadable, or a decompression bomb
            # one broken file must not abort the batch after earlier banners were replaced and
            # before catalogue.json is saved, which would leave images/ ahead of the catalogue
            print(f"skip {src.name}: {exc}")
            staged.unlink(missing_ok=True)
            continue
        entry["image"] = f"{RAW_BASE}/images/{sku}{SUFFIX}"
        installed += 1
    if installed:
        # this edit is a catalogue change, so the published timestamp has to follow it
        doc["generated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        scan.save(doc, CATALOGUE)
    return installed


def selftest() -> None:
    assert slug("Il Caff\u00e8") == "il-caffe"
    assert slug("Double Espresso Chiaro Decaffeinato") == "double-espresso-chiaro-decaffeinato"
    vertuo = {"sku": "7005.20", "name": "Melozio", "line": "vertuo", "family": "Mug",
              "image": "https://x/1.png", "source_image": "https://x/store.png"}
    original = {"sku": "7928.20", "name": "Arpeggio", "line": "original", "family": "Espresso"}
    assert "MELOZIO" in prompt_for(vertuo) and "VERTUO" in prompt_for(vertuo)
    assert "ORIGINAL" in prompt_for(original) and "truncated-cone" in prompt_for(original)
    # a banner must never become its own store photo reference
    assert store_photo({"image": "https://x/banner.webp"}) == ""
    assert store_photo({"source_image": "https://x/s.png", "image": "https://x/b.png"}) \
        == "https://x/s.png"
    # a redirect off the store must be refused; one on the store must still be followed
    try:
        fetch("https://evil.example/x")
    except ValueError:
        pass
    else:
        raise AssertionError("fetch must refuse an off-store url before any request")
    try:
        StoreOnlyRedirects().redirect_request(None, None, 302, "", {}, "https://evil.example/x")
    except urllib.error.HTTPError:
        pass
    else:
        raise AssertionError("an off-store redirect must be refused")
    on_store = StoreOnlyRedirects().redirect_request(
        urllib.request.Request(f"{scan.BASE}/a"), None, 302, "", {},
        f"{scan.BASE}/de/de/ecom/medias/x.png")
    assert on_store is not None and on_store.full_url.endswith("x.png"), \
        "a redirect on the store must still be followed"
    assert _on_store(f"{scan.BASE}/x"), "a store path is on the store"
    assert not _on_store("http" + scan.BASE[len("https"):] + "/x"), "a scheme downgrade is not"
    # the same origin written differently is still the store
    assert _on_store("https://WWW.NESPRESSO.COM/x"), "host case does not matter"
    assert _on_store("https://www.nespresso.com:443/x"), "the default port does not matter"
    assert _on_store("https://www.nespresso.com./x"), "a trailing dot does not matter"
    assert not _on_store("https://www.nespresso.com:8443/x"), "another port is not the store"
    assert not _on_store("https://www.nespresso.com:0/x"), "port 0 is not the default port"
    assert not _on_store({"url": f"{scan.BASE}/x"}), "a non-string value is never the store"

    # fetch() must go through that opener, not plain urlopen
    class _StubOpener:
        def __init__(self):
            self.calls: list[str] = []

        def open(self, request, timeout=None):
            self.calls.append(request.full_url)
            raise urllib.error.URLError("stub")

    global STORE_OPENER
    real_opener, stub = STORE_OPENER, _StubOpener()
    STORE_OPENER = stub
    try:
        try:
            fetch(f"{scan.BASE}/x.png")
        except urllib.error.URLError:
            pass
    finally:
        STORE_OPENER = real_opener
    assert stub.calls == [f"{scan.BASE}/x.png"], stub.calls
    # a description several capsules share is a range blurb: it must not steer their prompts
    doc = {"capsules": [
        {"sku": "1", "description": "Kakao- und Gew\u00fcrznoten."},
        {"sku": "2", "description": "Kakao- und Gew\u00fcrznoten."},
        {"sku": "3", "description": "Zitrusnote mit Limette."},
        {"sku": "4", "description": ""},
    ]}
    mine = own_descriptions(doc["capsules"], doc)
    assert [e["description"] for e in mine] == ["", "", "Zitrusnote mit Limette.", ""], mine
    # the taste and ingredient text from the product page steers the scene…
    entry = {"name": "Crystal Spark", "line": "vertuo",
             "description": "geb\u00e4ckartigen und fruchtigen Noten",
             "ingredients": "Gemahlener R\u00f6stkaffee"}
    p = prompt_for(entry)
    assert "geb\u00e4ckartigen und fruchtigen Noten" in p and "Gemahlener R\u00f6stkaffee" in p
    assert "Flavour" not in prompt_for({"name": "X", "line": "vertuo"})
    # a description that already ends in a full stop must not get a second one
    assert prompt_for({"name": "X", "line": "vertuo",
                       "description": "Unverkennbar."}).endswith("Unverkennbar.")
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
        # a palette packshot with bytes transparency must not warn on that convert
        pal = Image.new("P", (64, 64))
        pal.putpalette([255, 255, 255, 137, 184, 131] + [0, 0, 0] * 254)
        pal.paste(1, (16, 8, 48, 56))
        pal.info["transparency"] = bytes([0, 255, 0])
        pack = io.BytesIO()
        pal.save(pack, "PNG")
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            assert dominant_colour(pack.getvalue()) == (137, 184, 131)
        # an RGBA packshot must not average in its transparent background; grey is chosen so the
        # white/black filters alone would let it through
        rgba = Image.new("RGBA", (64, 64), (200, 200, 200, 0))
        for x in range(16, 48):
            for y in range(8, 56):
                rgba.putpixel((x, y), (137, 184, 131, 255))
        pack = io.BytesIO()
        rgba.save(pack, "PNG")
        assert dominant_colour(pack.getvalue()) == (137, 184, 131), dominant_colour(pack.getvalue())
    # ingest keys a downloaded banner by sku, scales it to the series size and
    # writes WebP, so the rest of this test needs Pillow
    try:
        from PIL import Image as pil_image
    except ImportError:
        print("selftest ok (Pillow missing: ingest checks skipped)")
        return
    buf = io.BytesIO()
    pil_image.new("RGB", (1824, 864), (137, 184, 131)).save(buf, "PNG")
    hand_saved = buf.getvalue()
    # ingest keys a downloaded banner by sku and rewrites the catalogue fields
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        global CATALOGUE, IMAGES, edit, references
        catalogue, images = CATALOGUE, IMAGES
        try:
            CATALOGUE, IMAGES = tmp / "catalogue.json", tmp / "images"
            CATALOGUE.write_text(scan.dumps({
                "generated": "x", "source": "s", "market": "de", "language": "de", "count": 1,
                "capsules": [dict(vertuo)],
            }))
            png = tmp / "7005.20.png"
            png.write_bytes(hand_saved)
            assert ingest([png]) == 1
            back = json.loads(CATALOGUE.read_text())["capsules"][0]
            assert back["source_image"] == "https://x/store.png", back
            assert back["image"].endswith("/images/7005.20.webp"), back
            installed = IMAGES / "7005.20.webp"
            with pil_image.open(installed) as got:
                assert (got.format, got.size) == ("WEBP", BANNER_SIZE), (got.format, got.size)
            # re-ingesting an installed banner must not truncate it
            assert ingest([installed]) == 1
            with pil_image.open(installed) as got:
                assert got.size == BANNER_SIZE, got.size
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
            ambiguous.write_bytes(hand_saved)
            assert ingest([ambiguous]) == 0, "an ambiguous name must not be guessed at"
            exact = tmp / "7061.20.png"
            exact.write_bytes(hand_saved)
            assert ingest([exact]) == 1
            got = {c["sku"]: c for c in json.loads(CATALOGUE.read_text())["capsules"]}
            assert "source_image" not in got["7934.20"], got["7934.20"]
            assert got["7061.20"]["image"].endswith("/images/7061.20.webp"), got["7061.20"]
            # the banner must never become its own "store photo" reference
            assert "source_image" not in got["7061.20"], got["7061.20"]
            # one refused drawing must not stop the queue: the capsule after it still installs
            real_edit, real_references = edit, references

            def fake_edit(prompt, files, key, size, quality, model):
                if "REFUSE" in prompt:
                    raise RuntimeError("image API refused the request (400)")
                return hand_saved

            edit = fake_edit
            references = lambda entry: [("store.png", hand_saved)]
            try:
                assert generate([
                    {"sku": "7934.20", "name": "REFUSE", "line": "original",
                     "source_image": "https://x/s.png"},
                    {"sku": "7061.20", "name": "OK", "line": "vertuo",
                     "source_image": "https://x/s.png"}], 2, "k") == 1
                # a pipeline that draws nothing must fail loudly, not report "installed 0"
                try:
                    generate([{"sku": f"793{i}.20", "name": "REFUSE", "line": "original",
                               "source_image": "https://x/s.png"} for i in range(3)], 3, "k")
                except SystemExit:
                    pass
                else:
                    raise AssertionError("an all-failed run must not exit 0")
                # drawn but not installable (a sku the catalogue does not know) is a failure too,
                # not a silent no-op that pays for a banner and drops it
                try:
                    generate([{"sku": "7999.20", "name": "OK", "line": "vertuo",
                               "source_image": "https://x/s.png"}], 1, "k")
                except SystemExit:
                    pass
                else:
                    raise AssertionError("a drawn-but-not-installed banner must not exit 0")
            finally:
                edit, references = real_edit, real_references
            # a capsule with no store photo cannot be drawn, so it never holds a queue slot
            CATALOGUE.write_text(scan.dumps({
                "generated": "x", "source": "s", "market": "de", "language": "de", "count": 2,
                "capsules": [
                    {"sku": "7101.20", "name": "A", "line": "vertuo",
                     "source_image": "https://x/s.png"},
                    {"sku": "7102.20", "name": "B", "line": "vertuo"},
                    {"sku": "7103.20", "name": "C", "line": "vertuo", "available": False,
                     "source_image": "https://x/s.png"}],
            }))
            assert [e["sku"] for e in needs_image()] == ["7101.20"], needs_image()
            # a shared description must be blanked where the queue is actually built too
            CATALOGUE.write_text(scan.dumps({
                "generated": "x", "source": "s", "market": "de", "language": "de", "count": 2,
                "capsules": [
                    {"sku": "7101.20", "name": "A", "line": "vertuo", "available": True,
                     "source_image": "https://x/s.png", "description": "Kakao- und Gew\u00fcrznoten."},
                    {"sku": "7102.20", "name": "B", "line": "vertuo", "available": True,
                     "source_image": "https://x/s.png", "description": "Kakao- und Gew\u00fcrznoten."}],
            }))
            assert [e["description"] for e in needs_image()] == ["", ""], needs_image()
            # a dry run counts what it would really draw, not the queue it was handed
            real_references = references
            try:
                references = lambda entry: [("store.png", hand_saved)]
                assert generate([{"sku": "7101.20", "line": "vertuo",
                                  "source_image": "https://x/s.png"}], 1, "k", dry_run=True) == 1

                def dead(entry):
                    raise urllib.error.URLError("404")

                references = dead
                assert generate([{"sku": "7101.20", "line": "vertuo",
                                  "source_image": "https://x/s.png"}], 1, "k", dry_run=True) == 0
                # one dead photo is that capsule's problem, not a broken pipeline
                assert generate([{"sku": "7101.20", "line": "vertuo",
                                  "source_image": "https://x/s.png"}], 1, "k") == 0
                # three with nothing drawn is the store or the API
                try:
                    generate([{"sku": f"71{i}1.20", "line": "vertuo",
                               "source_image": "https://x/s.png"} for i in range(3)], 3, "k")
                except SystemExit:
                    pass
                else:
                    raise AssertionError("three dead photos and nothing drawn must not exit 0")
            finally:
                references = real_references
            # store text cannot close the HTML comment or render a remote image in Gitea
            md = tmp / "needs.md"
            write_prompts([{"sku": "7151.20--><b>", "name": "A ![x](http://evil/p.gif)",
                            "line": "vertuo", "family": "Mug",
                            "url": "https://www.nespresso.com/x--><img src=//evil/p.gif>",
                            "source_image": "https://x/s.png",
                            "description": "Fruity\n````\n![](http://evil/p.gif)\n````text"}], md)
            text = md.read_text()
            header = text.split("````text")[0]
            assert header.count("-->") == 1, header  # only our own comment may close
            assert "![x](" not in header and "![](http" not in header, header
            assert "<b>" not in header, header
            body = text.split("````text", 1)[1]
            # only a line that *starts* with the fence closes it, so nothing a store page writes
            # may occupy a line start except our own closer
            closed = [line for line in body.splitlines() if line.startswith("````")]
            assert closed == ["````"], closed
            # one unreadable file must not abort an ingest batch after earlier banners were
            # replaced and before catalogue.json is saved
            broken = tmp / "7101.20.png"
            broken.write_bytes(b"not a png")
            good = tmp / "7102.20.png"
            good.write_bytes(hand_saved)
            assert ingest([broken, good]) == 1, "an unreadable file must be skipped, not fatal"
            assert not list(IMAGES.glob(".*.tmp")), "a skipped file must leave no staged tmp"
            # a redraw is one paid drawing per name: unknown or too many names must fail first
            real_argv, real_key = sys.argv, os.environ.get("OPENAI_API_KEY")
            os.environ["OPENAI_API_KEY"] = "sk-test"
            too_many = ["images.py", "generate", "--dry-run"] + [
                f"--sku=7{i}0.20" for i in range(MAX_REDRAW + 1)]
            try:
                for argv, expected in ((["images.py", "generate", "--sku", "nope", "--dry-run"],
                                        "unknown sku"), (too_many, "at most")):
                    sys.argv = argv
                    try:
                        main()
                    except SystemExit as exc:
                        assert expected in str(exc), (expected, exc)
                    else:
                        raise AssertionError(f"{argv[1:]} must fail before any paid call")
            finally:
                sys.argv = real_argv
                if real_key is None:
                    os.environ.pop("OPENAI_API_KEY", None)
                else:
                    os.environ["OPENAI_API_KEY"] = real_key
            # an empty queue, or one whose only capsule has no photo, is not a broken pipeline
            assert generate([], 0, "k") == 0
            assert generate([{"sku": "7102.20", "line": "vertuo"}], 1, "k") == 0
            # and the real edit() must turn a bad payload or a refusal into RuntimeError, so the
            # queue survives it: the fake above never exercises that conversion
            real_urlopen = urllib.request.urlopen
            try:
                for payload in (b'{"data": []}', b'{"error": "x"}', b'"nope"'):
                    urllib.request.urlopen = lambda *a, _p=payload, **k: io.BytesIO(_p)
                    try:
                        edit("p", [("store.png", hand_saved)], "k")
                    except RuntimeError:
                        pass
                    else:
                        raise AssertionError(f"{payload!r} must raise RuntimeError")

                def refuse(*args, **kwargs):
                    raise urllib.error.HTTPError("u", 400, "no", {}, io.BytesIO(b"bad prompt"))

                urllib.request.urlopen = refuse
                try:
                    edit("p", [("store.png", hand_saved)], "k")
                except RuntimeError:
                    pass
                else:
                    raise AssertionError("an HTTP refusal must raise RuntimeError")
            finally:
                urllib.request.urlopen = real_urlopen
        finally:
            CATALOGUE, IMAGES = catalogue, images
    print("selftest ok")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--selftest", action="store_true", help="check prompt/ingest logic, no files touched")
    sub = parser.add_subparsers(dest="command")
    p_prompts = sub.add_parser("prompts", help="write the ChatGPT prompt batch")
    p_prompts.add_argument("--out", type=Path, default=PROMPTS / "needs-images.md")
    p_generate = sub.add_parser("generate", help="draw banners with the OpenAI image API")
    p_generate.add_argument("--limit", type=int, default=2, help="how many banners to draw per line (default 2)")
    p_generate.add_argument("--line", choices=("vertuo", "original"),
                            help="only this line (default: both)")
    p_generate.add_argument("--sku", action="append", default=[], metavar="SKU",
                            help="draw only these sku(s), overwriting their banners"
                                 " (ignores the per-line cap)")
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
        entries = needs_image()
        write_prompts(entries, args.out)
        print(f"wrote {len(entries)} prompt(s) to {args.out}")
    elif args.command == "generate":
        key = os.environ.get("OPENAI_API_KEY", "")
        if not key and not args.dry_run:
            raise SystemExit("set OPENAI_API_KEY to draw banners")
        if args.sku:
            wanted = set(args.sku)
            if len(wanted) > MAX_REDRAW:
                raise SystemExit(f"{len(wanted)} sku(s) named; at most {MAX_REDRAW} per redraw")
            doc = json.loads(CATALOGUE.read_text())
            entries = own_descriptions([e for e in doc["capsules"] if e.get("sku") in wanted], doc)
            # a mistyped or shell-expanded sku used to redraw nothing and exit 0
            missing = wanted - {e["sku"] for e in entries}
            if missing:
                raise SystemExit(f"unknown sku(s): {' '.join(sorted(missing))}")
            limit = len(entries)  # --sku names exactly what to (re)draw, no per-line cap
        else:
            entries = [e for e in needs_image()
                       if not args.line or (e.get("line") or "vertuo") == args.line]
            limit = args.limit
        count = generate(entries, limit, key, dry_run=args.dry_run, size=args.size,
                         quality=args.quality, model=args.model)
        if args.dry_run:
            print(f"{count} of {len(entries)} capsule(s) would be drawn")
        else:
            print(f"installed {count} banner(s) into {IMAGES.relative_to(ROOT)}")
            if args.sku and count < len(entries):
                # a redraw that silently drew nothing is how a banner that came out wrong stays
                raise SystemExit(f"{len(entries) - count} of {len(entries)} named capsule(s) were not drawn")
    elif args.command == "ingest":
        paths = source_files(args.from_dir, args.files)
        count = ingest(paths)
        print(f"installed {count} banner(s) into {IMAGES.relative_to(ROOT)}")
        if paths and not count:
            # every file unreadable or matching no sku is not a success
            raise SystemExit("nothing installed: every file was unreadable or matched no sku")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
