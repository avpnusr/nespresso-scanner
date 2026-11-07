#!/usr/bin/env python3
"""Scan the Nespresso product catalogue into catalogue.json.

The Nespresso Stats dashboard needs to know which capsules exist so it can hint
at new ones. We read the public sitemap for capsule product URLs, then the
product JSON each product page embeds for its own front end, and keep the few
fields an inventory app cares about.

    python3 scan.py             # write catalogue.json (only if it changed)
    python3 scan.py --selftest  # parser check, no network

No dependencies: Python 3 standard library only, like the dashboard.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

BASE = "https://www.nespresso.com"
MARKET = "de"
LANG = "de"
PREFIX = f"/{MARKET}/{LANG}"
UA = "nespresso-scanner/0.1 (+https://github.com/avpnusr/nespresso-scanner)"
OUT = Path(__file__).resolve().parent / "catalogue.json"
IMAGES = Path(__file__).resolve().parent / "images"
RAW_BASE = "https://raw.githubusercontent.com/avpnusr/nespresso-scanner/main"

# Refuse to overwrite a good catalogue with a broken haul: fewer than this many
# capsules, or more than this share of product pages failing, is an outage, not
# a catalogue update.
MIN_ENTRIES = 20
MAX_ERROR_SHARE = 0.1

# The de sitemap omits capsules that are still sold (verified live, missing from
# /de/de/sitemap since 2026-10): fetch their product pages directly. Best-effort —
# if one is delisted it only adds to the error count, which stays within
# MAX_ERROR_SHARE. "Yuzu Vanilla Over Ice" sold in other markets but not in de.
EXTRA_PAGE_SLUGS = (
    "vertuo/nespresso-kaffee-kapsel-vertuo-melozio",
    "vertuo/nespresso-kaffee-kapsel-vertuo-intenso",
    "vertuo/nespresso-kaffee-kapsel-vertuo-stormio",
    "vertuo/odacio-vertuo-coffee-pods",
    "vertuo/nespresso-kaffee-kapsel-vertuo-arondio",
    "vertuo/master-origin-peru-organic-vertuo-coffee-pods",
)

FAMILIES = {
    "vertuo": ["Espresso", "Double Espresso", "Gran Lungo", "Mug", "Alto", "Carafe", "Alto XL"],
    "original": ["Ristretto", "Espresso", "Lungo"],
}

# Product-page cup-size slugs -> the dashboard's cup sizes. Flavoured and
# barista capsules list preparation slugs; those are mug-sized. Anything that
# does not fit the product's line falls back to that line's default family.
CUP_SIZE_FAMILY = {
    "espresso-vertuo": "Espresso",
    "double-espresso-vertuo": "Double Espresso",
    "gran-lungo": "Gran Lungo",
    "coffee": "Mug",
    "doppio": "Double Espresso",
    "carafe-vertuo": "Carafe",
    "espresso": "Espresso",
    "ristretto": "Ristretto",
    "lungo": "Lungo",
    "milk": "Mug",
    "reverso": "Mug",
    "icedrecipe-vertuo": "Mug",
    "cappuccino-and-latte-macchiato": "Mug",
}
DEFAULT_FAMILY = {"vertuo": "Mug", "original": "Espresso"}


def warn(message: str) -> None:
    """stderr, or an Actions annotation in CI: a green run must still show this."""
    print(("::warning::" if os.environ.get("GITHUB_ACTIONS") else "note: ") + message,
          file=sys.stderr)


def family_for(cups: list[str], line: str) -> str:
    valid = FAMILIES[line]
    for slug in cups:
        fam = CUP_SIZE_FAMILY.get(slug)
        if fam is None and "alto" in slug:  # the store's Alto slugs are not in the table
            fam = "Alto XL" if "xl" in slug else "Alto"
        if fam in valid:
            return fam
    return DEFAULT_FAMILY[line]


def clean_name(name: str) -> str:
    """Nespresso sprinkles zero-width chars into some names, and a name can carry markup."""
    return clean_text(re.sub(r"\s+", " ", name))


def _balanced(text: str, start: int) -> str | None:
    """The JSON object literal starting at or after `start`, brace-balanced and
    string-aware (a `}` inside a description must not end it early)."""
    i = text.find("{", start)
    if i < 0:
        return None
    depth = 0
    in_str = False
    escaped = False
    for j in range(i, len(text)):
        ch = text[j]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[i : j + 1]
    return None


def product_from_page(html: str) -> dict | None:
    """The capsule product a product page embeds in `window.ui.push(...)`."""
    for m in re.finditer(r"window\.ui\.push\(", html):
        raw = _balanced(html, m.end())
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        product = ((obj.get("configuration") or {}).get("eCommerceData") or {}).get("product")
        if isinstance(product, dict) and product.get("type") == "capsule":
            return product
    return None


def clean_text(text: str) -> str:
    """Strip the zero-width chars Nespresso sprinkles into its texts, and any HTML."""
    return re.sub(r"<[^>]+>", "", (text or "").replace("\u200b", "").replace("\u00ad", "")).strip()


def ingredients_text(product: dict) -> str:
    """The 'Zutaten' section's list — the flavour cues an image scene can lean on."""
    sections = product.get("ingredients") or []
    for i, section in enumerate(sections):
        if ((section.get("type") or {}).get("code") == "TITLE"
                and (section.get("text") or "").strip().lower() in ("zutaten", "ingredients")):
            for after in sections[i + 1:]:
                if (after.get("type") or {}).get("code") == "DESCRIPTION":
                    text = clean_text(after.get("text"))
                    # the description can BE the nutrition small print (Arpeggio Extra), or carry
                    # it inline (Active), so cut it off before the first sentence is taken
                    text = re.split(r"Enth\u00e4lt\s+geringf\u00fcgige\s+Mengen", text, maxsplit=1,
                                    flags=re.IGNORECASE)[0].strip()
                    # the list is one sentence; the next ones are nutrition small print
                    return text.split(".", 1)[0] + "." if "." in text else text
    return ""


def store_url(path: str | None) -> str:
    """An absolute URL on the store, or "" when it points anywhere else.

    A product page can name any host in its `url`/`image`, and the banner generator
    later fetches that photo in CI and uploads the bytes to a third-party API.
    """
    url = urllib.parse.urljoin(BASE, path or "")
    return url if url.startswith(BASE + "/") else ""


def entry_from_product(product: dict) -> dict | None:
    tech = " ".join(product.get("technologies") or [])
    line = "vertuo" if "vertuo" in tech else "original" if "original" in tech else None
    if line is None:
        return None
    name = clean_name(product.get("name") or "")
    if not name:
        return None
    cups = [
        c.rstrip("/").split("/")[-1].removeprefix("capsule-cupSize-").lower()
        for c in product.get("cupSizes") or []
    ]
    ranges = [
        r.rstrip("/").split("/")[-1].removeprefix("capsule-range-")
        for r in product.get("ranges") or []
    ]
    url = store_url(product.get("url"))
    image = store_url((product.get("image") or {}).get("url"))
    # no sku means no stable key, and no banner ever keys to it, which costs a paid drawing on
    # every run: a page without one is this page's error, not a capsule
    sku = str(product.get("internationalId") or product.get("id") or "").split("/")[-1]
    if not sku:
        return None
    try:
        # "0" and 0 and None all mean we did not get a price: a client would render it free
        price = float(product.get("price") or 0)
    except (TypeError, ValueError):
        raise ValueError("no price on the page") from None
    if price <= 0:
        raise ValueError("no price on the page")
    return {
        "sku": sku,
        "name": name,
        "line": line,
        "family": family_for(cups, line),
        "intensity": int(product.get("intensity") or 0),
        "special": "limited-edition" in url or any("limited-edition" in r for r in ranges),
        # the banner generator leans on these for the scene; without them the image model
        # invents colours and flavours from the capsule's name alone
        "description": clean_text(product.get("aromaticProfileDescription")
                                  or product.get("description")),
        "ingredients": ingredients_text(product),
        "available": bool(product.get("available") and product.get("inStock")),
        "price": round(float(price), 2),
        "url": url,
        # the store photo is only a reference for the banner generator; `image` is
        # reserved for the AI banner and set below once one exists (images.py).
        "source_image": image,
    }


def capsule_urls(html: str) -> list[str]:
    urls = {
        urllib.parse.urljoin(BASE, m.group(1))
        for m in re.finditer(r'href="(' + re.escape(PREFIX) + r'/order/capsules/[^"#]+)"', html)
    }
    return sorted(urls)


def fetch(url: str, timeout: int = 30, retries: int = 2) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "de-DE,de;q=0.9"})
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", "replace")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempt == retries:
                raise
            time.sleep(2 * (attempt + 1))
    raise AssertionError("unreachable")


def scan(workers: int = 4) -> tuple[dict, int, list[str]]:
    sitemap = capsule_urls(fetch(f"{BASE}{PREFIX}/sitemap"))
    if not sitemap:
        raise SystemExit("no capsule URLs found in the sitemap — refusing to write")
    urls = list(sitemap)
    seen = set(urls)
    extras: set[str] = set()
    for slug in EXTRA_PAGE_SLUGS:  # sitemap-omitted pages, best effort
        url = f"{BASE}{PREFIX}/order/capsules/{slug}"
        if url not in seen:
            seen.add(url)
            urls.append(url)
            extras.add(url)

    def one(url: str) -> tuple[str, dict | None | Exception]:
        try:
            product = product_from_page(fetch(url))
            if product is None:
                # A soft 404 (200 + not-found HTML) or a render hiccup never raises, so
                # fetch() retries don't see it; refetch once.
                product = product_from_page(fetch(url))
            return url, product
        except Exception as exc:  # one bad page must not sink the scan
            return url, exc

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(one, urls))

    # A SKU can be reachable twice (renamed URL); keep the in-stock listing, visibly.
    entries: dict[str, tuple[dict, bool]] = {}
    broken: list[str] = []
    unreachable: list[str] = []  # unreadable, so the last known row is still the best we have
    empty: list[str] = []  # 200 without a product: the store says it is not a capsule
    for url, product in results:
        if isinstance(product, Exception):
            broken.append(url)
            # a hard 404/410 is the store saying the page is gone, not a flake
            if not (isinstance(product, urllib.error.HTTPError) and product.code in (404, 410)):
                unreachable.append(url)
            continue
        if product is None:  # read fine: the page says this is no longer a capsule product
            broken.append(url)
            empty.append(url)
            continue
        try:
            entry = entry_from_product(product)
        except (TypeError, ValueError, AttributeError):  # our parse, not the store's word
            broken.append(url)
            unreachable.append(url)
            continue
        if entry is None:  # a capsule page we cannot key (no sku yet): our problem too
            broken.append(url)
            unreachable.append(url)
            continue
        # Key the row by the url it was fetched from, not the product's canonical one: the store
        # redirects an alias to its canonical page, and a failed fetch can only name the alias,
        # so url-keyed recovery would silently stop working for those pages.
        entry["url"] = url
        key = entry["sku"]
        stocked = bool(product.get("inStock"))
        if key in entries:
            warn(f"{key} reachable twice — keeping the "
                 f"{'in-stock' if stocked and not entries[key][1] else 'first'} listing")
        if key not in entries or (stocked and not entries[key][1]):
            entries[key] = (entry, stocked)

    # The sitemap-omitted extras are best effort: their failures must not spend the error budget
    # meant for transient sitemap failures.
    errors = sum(1 for url in broken if url not in extras)
    if errors and errors > len(sitemap) * MAX_ERROR_SHARE:
        raise SystemExit(f"{errors}/{len(sitemap)} product pages failed — refusing to write")
    if len(entries) < MIN_ENTRIES:
        raise SystemExit(f"only {len(entries)} capsules parsed from {len(urls)} URLs — refusing to write")
    if len(broken) > errors:
        warn(f"{len(broken) - errors}/{len(extras)} sitemap-omitted page(s) unavailable")
    if empty:
        warn(f"{len(empty)} page(s) loaded without a capsule product — treated as delisted")

    # Only a generated banner (images.py) becomes `image`; capsules without one keep no
    # `image` at all, so a client never falls back to a Nespresso store photo.
    generated = {p.stem for p in IMAGES.glob("*.webp")}
    for entry, _ in entries.values():
        if entry["sku"] in generated:
            entry["image"] = f"{RAW_BASE}/images/{entry['sku']}.webp"

    capsules = sorted((e for e, _ in entries.values()), key=lambda c: (c["line"], c["family"], c["name"]))
    doc = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": f"{BASE}{PREFIX}/order/capsules",
        "market": MARKET,
        "language": LANG,
        "count": len(capsules),
        "capsules": capsules,
    }
    return doc, errors, unreachable


# The published key order, so a single changed field stays a one-line diff: ingest()
# writes the file too, and used to keep whatever order it had loaded from the file.
CAPSULE_KEYS = ("sku", "name", "line", "family", "intensity", "special", "description",
                "ingredients", "available", "price", "url", "source_image", "image")


def ordered(capsule: dict) -> dict:
    """A capsule with its keys in the published order (unknown keys kept at the end)."""
    out = {k: capsule[k] for k in CAPSULE_KEYS if k in capsule}
    out.update({k: v for k, v in capsule.items() if k not in out})
    return out


def dumps(doc: dict) -> str:
    """Compact: one capsule per line, so a twice-daily refresh diffs cleanly."""
    meta = {k: doc[k] for k in ("generated", "source", "market", "language", "count")}
    head = json.dumps(meta, ensure_ascii=False, indent=2)[:-1].rstrip() + ",\n"
    body = ",\n".join("    " + json.dumps(ordered(c), ensure_ascii=False) for c in doc["capsules"])
    return head + '  "capsules": [\n' + body + "\n  ]\n}\n"


def save(doc: dict, out: Path = OUT) -> None:
    """Atomic: a crash mid-write must not leave a torn file for the clients polling it."""
    staged = out.with_name(f".{out.name}.{os.getpid()}.tmp")
    staged.write_text(dumps(doc))
    os.replace(staged, out)


def write(doc: dict, out: Path = OUT, unreachable: Iterable[str] = (),
          accept_loss: bool = False) -> bool:
    if out.exists():
        try:
            old = json.loads(out.read_text())
        except ValueError:
            old = None
        if isinstance(old, dict) and old:
            # A page we could not read is not a delisting: keep its previous row. Rows are keyed by
            # the url we actually fetched, so the claim is exact - a failure for a page we have no
            # row for cannot resurrect anything, and a delisted row is never claimed.
            down = {u.rstrip("/") for u in unreachable}
            have = {c.get("sku") for c in doc["capsules"]}
            used = {c.get("url") for c in doc["capsules"]}
            generated = {p.stem for p in IMAGES.glob("*.webp")}
            keep = []
            for row in old.get("capsules") or []:
                if row.get("sku") in have or row.get("url") in used:
                    continue
                if (row.get("url") or "").rstrip("/") not in down:
                    continue
                # a copy: dropping the image must not edit the file we compare against below
                kept = {k: v for k, v in row.items()
                        if k != "image" or row.get("sku") in generated}
                # we could not confirm it is orderable this run, and an unavailable row is never
                # queued for a paid banner. shortcut: no expiry - a page that fails forever keeps
                # the row (and its last price) until the page answers again.
                kept["available"] = False
                keep.append(kept)
            if keep:
                warn(f"kept {len(keep)} capsule(s) whose page could not be read")
                doc = {**doc,
                       "count": doc["count"] + len(keep),
                       "capsules": sorted(doc["capsules"] + keep,
                                          key=lambda c: (c["line"], c["family"], c["name"]))}
            # A truncated sitemap still parses: every fetched page succeeds, so the error share
            # cannot see it. A real delisting is a capsule or two.
            known = {c.get("sku") for c in old.get("capsules") or []}
            gone = known - {c.get("sku") for c in doc["capsules"]}
            if not accept_loss and len(gone) > len(known) * 0.2:
                raise SystemExit(f"{len(gone)}/{len(known)} known capsules gone from the store "
                                 "— refusing to write (truncated sitemap?)")
            if old.get("capsules") == doc["capsules"] and old.get("source") == doc["source"]:
                print(f"catalogue unchanged ({doc['count']} capsules)")
                return False
    save(doc, out)
    print(f"wrote {doc['count']} capsules to {out.name}")
    return True


def selftest() -> None:
    page = (
        '<html><script>window.ui.push({"configuration":{"eCommerceData":{"product":'
        '{"id":"erp.de.b2c/prod/7048.20","internationalId":"7048.20","name":"Altissio\u200b",'
        '"type":"capsule","intensity":9,"price":0.52,"available":true,"inStock":true,'
        '"aromaticProfileDescription":"Intensiv und w\u00fcrzig\u200b",'
        '"ingredients":[{"type":{"code":"TITLE"},"text":"Zutaten"},'
        '{"type":{"code":"DESCRIPTION"},"text":"Gemahlener <strong>R\u00f6stkaffee</strong>"}],'
        '"url":"/de/de/order/capsules/vertuo/altissio","image":{"url":"/ecom/x.png"},'
        '"technologies":["nesclub2.de.b2c/machineTechno/vertuo"],'
        '"cupSizes":["nesclub2.de.b2c/cat/capsule-cupSize-espresso-vertuo"],'
        '"ranges":["nesclub2.de.b2c/cat/capsule-range-espresso-vertuo"]}}}}});</script></html>'
    )
    product = product_from_page(page)
    assert product is not None, "product not extracted"
    assert entry_from_product(product) == {
        "sku": "7048.20",
        "name": "Altissio",
        "line": "vertuo",
        "family": "Espresso",
        "intensity": 9,
        "special": False,
        "description": "Intensiv und würzig",
        "ingredients": "Gemahlener Röstkaffee",
        "available": True,
        "price": 0.52,
        "url": f"{BASE}/de/de/order/capsules/vertuo/altissio",
        "source_image": f"{BASE}/ecom/x.png",
    }, entry_from_product(product)
    # a `}` inside a string must not cut the object short
    assert _balanced('{"text":"a } b","n":1}', 0) == '{"text":"a } b","n":1}'
    assert family_for(["coffee", "milk"], "vertuo") == "Mug"
    assert family_for(["cappuccino-and-latte-macchiato"], "original") == "Espresso"
    # Alto is a declared cup size whose slug is not in the table: it must not become Mug
    assert family_for(["alto-vertuo"], "vertuo") == "Alto"
    assert family_for(["alto-xl-vertuo"], "vertuo") == "Alto XL"
    assert family_for(["alto-vertuo"], "original") == "Espresso", "Alto is Vertuo only"
    assert clean_name("Hazelnut\n Brownie\u200b  Coffee ") == "Hazelnut Brownie Coffee"
    # clean_text strips HTML and the zero-width chars the page sprinkles in
    assert clean_text("<strong>Kakao</strong>\u200b") == "Kakao"
    assert clean_name("<b>Hazelnut</b> Brownie") == "Hazelnut Brownie"
    # an off-store url/image must be dropped: the banner generator would fetch it in CI
    assert store_url("//evil.example/x") == "" and store_url("https://evil.example/x") == ""
    assert store_url("/de/de/order/capsules/vertuo/x") == f"{BASE}/de/de/order/capsules/vertuo/x"
    assert ingredients_text({"ingredients": [
        {"type": {"code": "TITLE"}, "text": "Inhalt"},
        {"type": {"code": "DESCRIPTION"}, "text": "10 Kapseln"}]}) == ""
    assert ingredients_text({"ingredients": [
        {"type": {"code": "TITLE"}, "text": "Zutaten"},
        {"type": {"code": "DESCRIPTION"},
            "text": "R\u00f6stkaffee, nat\u00fcrliches Aroma. Enth\u00e4lt geringf\u00fcgige Mengen von Energie."}]}) \
        == "Röstkaffee, natürliches Aroma."
    assert capsule_urls('<a href="/de/de/order/capsules/vertuo/x">y</a>') == [
        f"{BASE}/de/de/order/capsules/vertuo/x"
    ]
    # the scan's own guards: a haul below MIN_ENTRIES, or one with too many failed pages, must
    # abort instead of publishing. fetch() is stubbed, so this stays offline.
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        global IMAGES, OUT, EXTRA_PAGE_SLUGS, fetch
        saved = IMAGES, OUT, EXTRA_PAGE_SLUGS, fetch
        try:
            IMAGES, OUT = Path(tmp) / "images", Path(tmp) / "catalogue.json"
            EXTRA_PAGE_SLUGS = ()  # the sitemap stub already lists every page

            def fake(pages: int, dead: tuple[str, ...] = (), empty: tuple[str, ...] = (),
                     gone: tuple[str, ...] = (), broke: tuple[str, ...] = ()):
                def f(url: str, *a, **k) -> str:
                    if url.endswith("/sitemap"):
                        return "<html>" + "".join(
                            f'<a href="{PREFIX}/order/capsules/vertuo/p{i}"></a>' for i in range(pages)
                        ) + "</html>"
                    tail = url.rsplit("/", 1)[-1]
                    if tail in dead:
                        raise OSError("boom")
                    if tail in gone:  # the store says the page is gone
                        raise urllib.error.HTTPError(url, 404, "gone", {}, None)
                    if tail in empty:  # 200, but the page carries no product
                        return "<html><body>not a capsule</body></html>"
                    product = {"internationalId": tail, "name": "N" + tail, "type": "capsule",
                               "price": 1, "available": True, "inStock": True,
                               "technologies": ["vertuo"]}
                    if tail in broke:  # a capsule page whose payload lost its price
                        product["price"] = None
                    page = {"configuration": {"eCommerceData": {"product": product}}}
                    return f"<script>window.ui.push({json.dumps(page)});</script>"
                return f

            fetch = fake(19)  # fewer than MIN_ENTRIES
            try:
                scan()
            except SystemExit:
                pass
            else:
                raise AssertionError("a 19-capsule haul must be refused")
            fetch = fake(30, tuple(f"p{i}" for i in range(4)))  # 13% > MAX_ERROR_SHARE
            try:
                scan()
            except SystemExit:
                pass
            else:
                raise AssertionError("a 13% failed-page haul must be refused")
            fetch = fake(30)
            assert scan()[0]["count"] == 30, "a healthy haul must still parse"
            # the error budget: exactly MAX_ERROR_SHARE passes, one more aborts
            fetch = fake(80, tuple(f"p{i}" for i in range(8)))
            assert scan()[0]["count"] == 72, "8 of 80 (exactly 10%) must pass"
            fetch = fake(80, tuple(f"p{i}" for i in range(9)))
            try:
                scan()
            except SystemExit:
                pass
            else:
                raise AssertionError("9 of 80 must be refused")
            # the budget divides by sitemap pages only: extras must not dilute it
            EXTRA_PAGE_SLUGS = tuple(f"x{i}" for i in range(10))
            fetch = fake(30, tuple(f"p{i}" for i in range(4)))  # 4 of 30 = 13%
            try:
                scan()
            except SystemExit:
                pass
            else:
                raise AssertionError("4 dead of 30 sitemap pages must be refused")
            # an empty sitemap is a sitemap problem, even when the extras would still give URLs
            EXTRA_PAGE_SLUGS = ("gone-1",)
            fetch = fake(0)
            try:
                scan()
            except SystemExit as exc:
                assert "sitemap" in str(exc), exc
            else:
                raise AssertionError("an empty sitemap must be refused as a sitemap problem")
            # dead sitemap-omitted pages are best effort: they must not spend the error budget
            EXTRA_PAGE_SLUGS = tuple(f"gone-{i}" for i in range(4))
            fetch = fake(30, EXTRA_PAGE_SLUGS)
            assert scan()[0]["count"] == 30, "dead extras must not abort a healthy sitemap"
            # only a page that could not be read is carried over: a 200 without a product is the
            # store saying this is no longer a capsule, so its row must drop out
            fetch = fake(30, dead=("p7",))
            _, errs, down = scan()
            assert errs == 1 and down == [f"{BASE}{PREFIX}/order/capsules/vertuo/p7"], (errs, down)
            fetch = fake(30, empty=("p7",))
            _, errs, down = scan()
            assert errs == 1 and down == [], (errs, down)
            # a 404 is the store saying the page is gone, so it must not be carried
            fetch = fake(30, gone=("p7",))
            _, errs, down = scan()
            assert errs == 1 and down == [], (errs, down)
            # a page that reads but no longer parses is our problem: that capsule is carried
            fetch = fake(30, broke=("p7",))
            _, errs, down = scan()
            assert errs == 1 and down == [f"{BASE}{PREFIX}/order/capsules/vertuo/p7"], (errs, down)
        finally:
            IMAGES, OUT, EXTRA_PAGE_SLUGS, fetch = saved
    # the small print the page adds after the real list must never be published as ingredients
    assert ingredients_text({"ingredients": [
        {"type": {"code": "TITLE"}, "text": "Zutaten"},
        {"type": {"code": "DESCRIPTION"},
            "text": "Enth\u00e4lt geringf\u00fcgige Mengen von Energie, Fett."}]}) == ""
    # a page with no sku is a page error, so it never publishes "" and never queues a banner
    assert entry_from_product({k: v for k, v in product.items()
                               if k not in ("id", "internationalId")}) is None
    # neither does a page without a price: 0.0 reads as a free capsule
    for bad in (None, 0, ""):
        try:
            entry_from_product({**product, "price": bad})
        except ValueError:
            pass
        else:
            raise AssertionError(f"a price of {bad!r} must not be published")
    # the file's key order is canonical, so a one-field change is a one-line diff
    doc = {"generated": "x", "source": "s", "market": "de", "language": "de", "count": 1}
    assert dumps({**doc, "capsules": [{"image": "i", "sku": "1", "name": "n"}]}) == \
        dumps({**doc, "capsules": [{"name": "n", "sku": "1", "image": "i"}]})
    assert '"sku": "1", "name": "n", "image": "i"' in dumps(
        {**doc, "capsules": [{"image": "i", "name": "n", "sku": "1"}]})
    # write() is atomic, does not rewrite an unchanged catalogue, and refuses a haul
    # that lost a large share of the capsules it already knew
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "catalogue.json"
        full = [{"sku": f"{i}.20", "name": f"n{i}", "line": "vertuo", "family": "Mug",
                 "url": f"{BASE}/p/{i}", "image": f"{RAW_BASE}/images/{i}.20.webp"}
                for i in range(10)]
        assert write({**doc, "count": 10, "capsules": full}, out)
        assert not write({**doc, "count": 10, "capsules": full}, out), "unchanged must not rewrite"
        try:
            write({**doc, "count": 6, "capsules": full[:6]}, out)
        except SystemExit:
            pass
        else:
            raise AssertionError("a truncated haul must be refused")
        assert json.loads(out.read_text())["capsules"] == full, "the old catalogue must survive"
        # a capsule whose page could not be read is carried over, never published as gone. The
        # stored url is the store's canonical one and may differ from the href we fetched, which
        # is exactly why the carry-over must not match on it.
        assert write({**doc, "count": 8, "capsules": full[:8]}, out)
        newbie = {"sku": "99.20", "name": "n99", "line": "vertuo", "family": "Mug",
                  "url": f"{BASE}/p/99"}
        assert write({**doc, "count": 8, "capsules": full[:7] + [newbie]}, out,
                     unreachable=[full[7]["url"] + "/"])
        back = json.loads(out.read_text())["capsules"]
        assert [c["sku"] for c in back] == [c["sku"] for c in full[:8]] + ["99.20"], back
        assert back[7]["sku"] == "7.20" and back[7]["name"] == "n7", back[7]
        # ... without an image url for a banner file that is not there, and not offered as sold:
        # we could not confirm it is orderable, and unavailable rows are never drawn
        assert "image" not in back[7], back[7]
        assert back[7]["available"] is False, back[7]
        # a page the store answers with 404 is gone, not flaky, so it must not be carried
        assert write({**doc, "count": 8, "capsules": full[:7] + [newbie]}, out, unreachable=[])
        assert "7.20" not in {c["sku"] for c in json.loads(out.read_text())["capsules"]}, \
            "a 404 is a delisting, so the row must drop"
        # a carried row keeps its banner while the file is there, and loses it when it is not
        write_images = Path(tmp) / "write-images"
        write_images.mkdir()
        (write_images / "5.20.webp").write_bytes(b"x")
        real_images = IMAGES
        IMAGES = write_images
        try:
            out.write_text(dumps({**doc, "count": 8, "capsules": full[:8]}))
            without5 = [c for c in full[:8] if c["sku"] != "5.20"]
            assert write({**doc, "count": 7, "capsules": without5}, out,
                         unreachable=[full[5]["url"]])
            kept5 = [c for c in json.loads(out.read_text())["capsules"] if c["sku"] == "5.20"][0]
            assert kept5["image"] == full[5]["image"], kept5
        finally:
            IMAGES = real_images
        # a real bulk loss is still refused, and --accept-loss is the operator override
        try:
            write({**doc, "count": 4, "capsules": full[:4]}, out)
        except SystemExit:
            pass
        else:
            raise AssertionError("a bulk loss must be refused")
        assert write({**doc, "count": 4, "capsules": full[:4]}, out, accept_loss=True)
        # a failed atomic replace must leave the previous file untouched
        before = out.read_text()
        real_replace = os.replace
        os.replace = lambda *a, **k: (_ for _ in ()).throw(OSError("disk full"))
        try:
            try:
                write({**doc, "count": 4, "capsules": [dict(c, name="x") for c in full[:4]]}, out)
            except OSError:
                pass
            else:
                raise AssertionError("a failed replace must not be swallowed")
        finally:
            os.replace = real_replace
        assert out.read_text() == before, "a failed write must leave the old catalogue intact"
        # a previous file that is not a JSON object must not crash the run
        junk = Path(tmp) / "junk.json"
        junk.write_text("[1]")
        assert write({**doc, "count": 1, "capsules": full[:1]}, junk)
        # a product that moved to a new sku while its old page fails must not appear twice
        out.write_text(dumps({**doc, "count": 8, "capsules": full[:8]}))
        moved = dict(full[7], sku="77.20")
        assert write({**doc, "count": 8, "capsules": full[:7] + [moved]}, out,
                     unreachable=[full[7]["url"]])
        after = [c["sku"] for c in json.loads(out.read_text())["capsules"]]
        assert after == [f"{i}.20" for i in range(7)] + ["77.20"], after
        # a delisting plus an unrelated flake: the flake's own row is kept, the delisted one drops
        prev = {**doc, "count": 8, "capsules": full[:8]}
        out.write_text(dumps(prev))
        assert write({**doc, "count": 7, "capsules": full[:6] + [newbie]}, out,
                     unreachable=[full[7]["url"]])
        got = [c["sku"] for c in json.loads(out.read_text())["capsules"]]
        assert "7.20" in got and "6.20" not in got, got
        # ... and a failure of a page the catalogue has no row for cannot resurrect anything
        out.write_text(dumps(prev))
        assert write({**doc, "count": 7, "capsules": full[:7]}, out,
                     unreachable=[f"{BASE}/p/99"])
        got = [c["sku"] for c in json.loads(out.read_text())["capsules"]]
        assert got == [c["sku"] for c in full[:7]], got
        # end to end: scan() must key the row by the url it fetched, or a failed page cannot
        # claim it back (the product's own url may be a different canonical one)
        fetch = fake(30)
        haul, _, none_down = scan()
        assert none_down == [], none_down
        e2e = Path(tmp) / "e2e.json"
        e2e.write_text(dumps({**doc, "count": len(haul["capsules"]), "capsules": haul["capsules"]}))
        victim = haul["capsules"][-1]
        fetch = fake(30, dead=(victim["sku"],))
        haul2, _, down2 = scan()
        assert write(haul2, e2e, unreachable=down2), "the flaked capsule must be kept"
        kept = {c["sku"] for c in json.loads(e2e.read_text())["capsules"]}
        assert victim["sku"] in kept, (victim["sku"], kept)
    print("selftest ok")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selftest", action="store_true", help="check the parser without network")
    parser.add_argument("--accept-loss", action="store_true",
                        help="write even when more than 20%% of the known capsules are gone"
                             " (a bulk store change, not a truncated sitemap)")
    args = parser.parse_args()
    if args.selftest:
        selftest()
        return
    doc, errors, unreachable = scan()
    if errors:
        warn(f"{errors} sitemap page(s) skipped")
    write(doc, unreachable=unreachable, accept_loss=args.accept_loss)


if __name__ == "__main__":
    main()
