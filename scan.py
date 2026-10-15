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
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

BASE = "https://www.nespresso.com"
MARKET = "de"
LANG = "de"
PREFIX = f"/{MARKET}/{LANG}"
UA = "nespresso-scanner/0.1 (+https://git.khmls.net/klein/nespresso-scanner)"
OUT = Path(__file__).resolve().parent / "catalogue.json"

# Refuse to overwrite a good catalogue with a broken haul: fewer than this many
# capsules, or more than this share of product pages failing, is an outage, not
# a catalogue update.
MIN_ENTRIES = 20
MAX_ERROR_SHARE = 0.1

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


def family_for(cups: list[str], line: str) -> str:
    valid = FAMILIES[line]
    for slug in cups:
        fam = CUP_SIZE_FAMILY.get(slug)
        if fam in valid:
            return fam
    return DEFAULT_FAMILY[line]


def clean_name(name: str) -> str:
    """Nespresso sprinkles zero-width and soft hyphens into some names."""
    return re.sub(r"\s+", " ", name.replace("\u200b", "").replace("\u00ad", "")).strip()


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
    url = product.get("url") or ""
    image = (product.get("image") or {}).get("url") or ""
    return {
        "sku": str(product.get("internationalId") or product.get("id") or "").split("/")[-1],
        "name": name,
        "line": line,
        "family": family_for(cups, line),
        "intensity": int(product.get("intensity") or 0),
        "special": "limited-edition" in url or any("limited-edition" in r for r in ranges),
        "available": bool(product.get("available") and product.get("inStock")),
        "price": round(float(product.get("price") or 0), 2),
        "url": urllib.parse.urljoin(BASE, url),
        "image": urllib.parse.urljoin(BASE, image),
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


def scan(workers: int = 4) -> tuple[dict, int]:
    urls = capsule_urls(fetch(f"{BASE}{PREFIX}/sitemap"))
    if not urls:
        raise SystemExit("no capsule URLs found in the sitemap — refusing to write")

    def one(url: str) -> tuple[str, dict | None | Exception]:
        try:
            return url, product_from_page(fetch(url))
        except Exception as exc:  # one bad page must not sink the scan
            return url, exc

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(one, urls))

    # A SKU can be reachable twice (renamed URL); keep the in-stock listing.
    entries: dict[str, tuple[dict, bool]] = {}
    errors = 0
    for url, product in results:
        if isinstance(product, Exception) or product is None:
            errors += 1
            continue
        entry = entry_from_product(product)
        if entry is None:
            errors += 1
            continue
        key = entry["sku"] or f"{entry['line']}/{entry['name']}"
        stocked = bool(product.get("inStock"))
        if key not in entries or (stocked and not entries[key][1]):
            entries[key] = (entry, stocked)

    if errors and errors > len(urls) * MAX_ERROR_SHARE:
        raise SystemExit(f"{errors}/{len(urls)} product pages failed — refusing to write")
    if len(entries) < MIN_ENTRIES:
        raise SystemExit(f"only {len(entries)} capsules parsed from {len(urls)} URLs — refusing to write")

    capsules = sorted((e for e, _ in entries.values()), key=lambda c: (c["line"], c["family"], c["name"]))
    doc = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": f"{BASE}{PREFIX}/order/capsules",
        "market": MARKET,
        "language": LANG,
        "count": len(capsules),
        "capsules": capsules,
    }
    return doc, errors


def dumps(doc: dict) -> str:
    """Compact: one capsule per line, so a twice-daily refresh diffs cleanly."""
    meta = {k: doc[k] for k in ("generated", "source", "market", "language", "count")}
    head = json.dumps(meta, ensure_ascii=False, indent=2)[:-1].rstrip() + ",\n"
    body = ",\n".join("    " + json.dumps(c, ensure_ascii=False) for c in doc["capsules"])
    return head + '  "capsules": [\n' + body + "\n  ]\n}\n"


def write(doc: dict) -> bool:
    if OUT.exists():
        try:
            old = json.loads(OUT.read_text())
        except ValueError:
            old = None
        if old and old.get("capsules") == doc["capsules"] and old.get("source") == doc["source"]:
            print(f"catalogue unchanged ({doc['count']} capsules)")
            return False
    OUT.write_text(dumps(doc))
    print(f"wrote {doc['count']} capsules to {OUT.name}")
    return True


def selftest() -> None:
    page = (
        '<html><script>window.ui.push({"configuration":{"eCommerceData":{"product":'
        '{"id":"erp.de.b2c/prod/7048.20","internationalId":"7048.20","name":"Altissio\u200b",'
        '"type":"capsule","intensity":9,"price":0.52,"available":true,"inStock":true,'
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
        "available": True,
        "price": 0.52,
        "url": f"{BASE}/de/de/order/capsules/vertuo/altissio",
        "image": f"{BASE}/ecom/x.png",
    }, entry_from_product(product)
    # a `}` inside a string must not cut the object short
    assert _balanced('{"text":"a } b","n":1}', 0) == '{"text":"a } b","n":1}'
    assert family_for(["coffee", "milk"], "vertuo") == "Mug"
    assert family_for(["cappuccino-and-latte-macchiato"], "original") == "Espresso"
    assert clean_name("Hazelnut\n Brownie\u200b  Coffee ") == "Hazelnut Brownie Coffee"
    assert capsule_urls('<a href="/de/de/order/capsules/vertuo/x">y</a>') == [
        f"{BASE}/de/de/order/capsules/vertuo/x"
    ]
    print("selftest ok")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selftest", action="store_true", help="check the parser without network")
    args = parser.parse_args()
    if args.selftest:
        selftest()
        return
    doc, errors = scan()
    if errors:
        print(f"note: {errors} page(s) skipped", file=sys.stderr)
    write(doc)


if __name__ == "__main__":
    main()
