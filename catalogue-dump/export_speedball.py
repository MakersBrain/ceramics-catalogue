#!/usr/bin/env python3
"""Export Speedball's public BigCommerce ceramics catalogue to NDJSON.

The storefront token is deliberately discovered from the public category page and
never stored. Each purchasable variant becomes one ceramics.catalogue_item.v1 row.
"""

from __future__ import annotations

import argparse
import html
import json
import mimetypes
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

BASE_URL = "https://www.speedballart.com"
CATEGORY_URL = f"{BASE_URL}/shop/ceramics/"
GRAPHQL_URL = f"{BASE_URL}/graphql"
TOKEN_RE = re.compile(r'\\"storefront_api_token\\":\\"([^\\"]+)')
TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"\s+")

PRODUCT_QUERY = """
query CeramicsProducts($after: String) {
  site {
    category(entityId: 28) {
      products(first: 50, after: $after) {
        pageInfo { hasNextPage endCursor }
        edges { node {
          entityId name path sku description
          availabilityV2 { status description }
          defaultImage { urlOriginal url(width: 800) altText }
          prices { price { value currencyCode } }
          categories { edges { node { entityId name path } } }
          variants(first: 50) {
            pageInfo { hasNextPage endCursor }
            edges { node {
              entityId sku
              defaultImage { urlOriginal url(width: 800) altText }
              prices { price { value currencyCode } }
              inventory { isInStock aggregated { availableToSell warningLevel } }
              options { edges { node {
                displayName values { edges { node { label } } }
              } } }
            } }
          }
        } }
      }
    }
  }
}
"""

VARIANT_QUERY = """
query ProductVariants($productId: Int!, $after: String) {
  site {
    product(entityId: $productId) {
      variants(first: 50, after: $after) {
        pageInfo { hasNextPage endCursor }
        edges { node {
          entityId sku
          defaultImage { urlOriginal url(width: 800) altText }
          prices { price { value currencyCode } }
          inventory { isInStock aggregated { availableToSell warningLevel } }
          options { edges { node {
            displayName values { edges { node { label } } }
          } } }
        } }
      }
    }
  }
}
"""


def request(url: str, *, data: bytes | None = None, headers: dict | None = None):
    common = {
        "User-Agent": "Mozilla/5.0 (compatible; AtelieriaCatalogue/1.0)",
        "Accept": "application/json,text/html,*/*",
        "Referer": CATEGORY_URL,
    }
    common.update(headers or {})
    return urllib.request.urlopen(urllib.request.Request(url, data=data, headers=common), timeout=45)


def storefront_token() -> str:
    with request(CATEGORY_URL) as response:
        page = response.read().decode("utf-8", "replace")
    match = TOKEN_RE.search(page)
    if not match:
        raise RuntimeError("Speedball storefront token not found")
    return match.group(1)


def graphql(token: str, query: str, variables: dict) -> dict:
    payload = json.dumps({"query": query, "variables": variables}).encode()
    with request(
        GRAPHQL_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Origin": BASE_URL,
        },
    ) as response:
        result = json.load(response)
    if result.get("errors"):
        raise RuntimeError(json.dumps(result["errors"], ensure_ascii=False))
    return result["data"]


def clean_html(value: str | None) -> str | None:
    if not value:
        return None
    value = re.sub(r"<(script|style)\b.*?</\1>", " ", value, flags=re.I | re.S)
    value = TAG_RE.sub(" ", value)
    value = SPACE_RE.sub(" ", html.unescape(value)).strip()
    return value or None


def options_for(variant: dict) -> dict[str, str]:
    result = {}
    for edge in variant["options"]["edges"]:
        node = edge["node"]
        values = [item["node"]["label"] for item in node["values"]["edges"]]
        result[node["displayName"]] = " / ".join(values)
    return result


def quantity_unit(options: dict[str, str]) -> tuple[float | None, str | None]:
    size = next((v for k, v in options.items() if k.lower() in {"size", "weight"}), "")
    match = re.search(r"(?i)\b(\d+(?:\.\d+)?)\s*(fl\.?\s*oz|oz|lb|ml|gal(?:lon)?|pint|quart)\b", size)
    if not match:
        return None, None
    units = {
        "fl oz": "floz", "fl. oz": "floz", "oz": "oz", "lb": "lb",
        "ml": "ml", "gal": "gal", "gallon": "gal", "pint": "pint", "quart": "quart",
    }
    return float(match.group(1)), units[SPACE_RE.sub(" ", match.group(2).lower())]


def family_for(product: dict) -> str:
    categories = [edge["node"] for edge in product["categories"]["edges"]]
    scoped = [c for c in categories if c["path"].startswith("/shop/ceramics/")]
    if not scoped:
        return "Ceramics"
    scoped.sort(key=lambda c: c["path"].count("/"), reverse=True)
    return scoped[0]["name"]


def image_suffix(url: str) -> str:
    path = urllib.parse.urlparse(url).path
    suffix = Path(path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
        return ".jpg" if suffix == ".jpeg" else suffix
    guessed = mimetypes.guess_extension(mimetypes.guess_type(path)[0] or "")
    return guessed or ".jpg"


def safe_name(sku: str, entity_id: int) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", sku).strip("-.")
    return name or f"variant-{entity_id}"


def download_image(url: str, target: Path, *, refresh: bool = False) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if not refresh and target.exists() and target.stat().st_size:
        return
    last_error = None
    for attempt in range(3):
        try:
            with request(url) as response, target.open("wb") as output:
                while chunk := response.read(1024 * 256):
                    output.write(chunk)
            return
        except (OSError, urllib.error.URLError) as error:
            last_error = error
            time.sleep(attempt + 1)
    raise RuntimeError(f"image download failed: {url}: {last_error}")


def all_products(token: str) -> list[dict]:
    products, cursor = [], None
    while True:
        data = graphql(token, PRODUCT_QUERY, {"after": cursor})
        connection = data["site"]["category"]["products"]
        for edge in connection["edges"]:
            product = edge["node"]
            variants = product["variants"]
            variant_nodes = [item["node"] for item in variants["edges"]]
            variant_cursor = variants["pageInfo"]["endCursor"]
            while variants["pageInfo"]["hasNextPage"]:
                page = graphql(token, VARIANT_QUERY, {"productId": product["entityId"], "after": variant_cursor})
                variants = page["site"]["product"]["variants"]
                variant_nodes.extend(item["node"] for item in variants["edges"])
                variant_cursor = variants["pageInfo"]["endCursor"]
            product["variant_nodes"] = variant_nodes
            products.append(product)
        if not connection["pageInfo"]["hasNextPage"]:
            return products
        cursor = connection["pageInfo"]["endCursor"]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("catalogue-dumps-new/speedball-ceramics.ndjson"))
    parser.add_argument("--images", type=Path, default=Path("catalogue-images/speedball"))
    parser.add_argument("--no-images", action="store_true")
    parser.add_argument("--refresh-images", action="store_true")
    args = parser.parse_args()

    token = storefront_token()
    products = all_products(token)
    fetched_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    rows, image_count = [], 0
    for product in products:
        description = clean_html(product.get("description"))
        family = family_for(product)
        product_url = urllib.parse.urljoin(BASE_URL, product["path"])
        product_image = product.get("defaultImage") or {}
        for variant in product["variant_nodes"]:
            sku = (variant.get("sku") or product.get("sku") or "").strip()
            options = options_for(variant)
            option_text = " — ".join(options.values())
            name = product["name"] + (f" — {option_text}" if option_text else "")
            price_node = (variant.get("prices") or product.get("prices") or {}).get("price") or {}
            inventory = variant.get("inventory") or {}
            aggregated = inventory.get("aggregated") or {}
            image_node = variant.get("defaultImage") or product_image
            image_url = image_node.get("url") or image_node.get("urlOriginal")
            image_path = None
            if image_url and not args.no_images:
                relative = Path("catalogue-images/speedball") / (
                    safe_name(sku, variant["entityId"]) + image_suffix(image_url)
                )
                download_image(image_url, args.images / relative.name, refresh=args.refresh_images)
                image_path = relative.as_posix()
                image_count += 1
            quantity, unit = quantity_unit(options)
            currency = price_node.get("currencyCode")
            price = price_node.get("value")
            in_stock = bool(inventory.get("isInStock"))
            row = {
                "format": "ceramics.catalogue_item.v1",
                "source": "speedball",
                "external_id": f"speedball:{variant['entityId']}",
                "name": name,
                "description": description,
                "brand": "Speedball",
                "sku": sku or None,
                "family": family,
                "product_url": product_url,
                "image_url": image_url,
                "image_path": image_path,
                "price": price,
                "currency": currency,
                "price_text": f"${price:.2f} USD" if price is not None and currency == "USD" else None,
                "vat_status": None,
                "quantity": quantity,
                "unit": unit,
                "availability": "https://schema.org/InStock" if in_stock else "https://schema.org/OutOfStock",
                "fetched_at": fetched_at,
                "raw": {
                    "bigcommerce_product_id": product["entityId"],
                    "bigcommerce_variant_id": variant["entityId"],
                    "options": options,
                    "available_to_sell": aggregated.get("availableToSell"),
                    "warning_level": aggregated.get("warningLevel"),
                    "product_availability": product.get("availabilityV2"),
                    "image_alt": image_node.get("altText"),
                    "image_original_url": image_node.get("urlOriginal"),
                    "categories": [edge["node"] for edge in product["categories"]["edges"]],
                },
            }
            rows.append(row)
    rows.sort(key=lambda row: (row["family"], row["name"], row["sku"] or ""))
    with args.output.open("w", encoding="utf-8") as output:
        for row in rows:
            output.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"products={len(products)} variants={len(rows)} images={image_count} output={args.output}")


if __name__ == "__main__":
    main()
