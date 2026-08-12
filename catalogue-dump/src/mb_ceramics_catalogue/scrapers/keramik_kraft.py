"""Keramik-Kraft (keramik-kraft.com), a bespoke 4D storefront.

Collected only because the operator explicitly decided to. This site's
robots.txt allows named search engines and disallows everything else, so the
source must set `ignore_robots` and a reduced request rate for this scraper to
run at all. It uses only public pages and touches no login, cart or other
access control.

Product pages carry no price: the article detail is assembled client-side and
the server response only repeats the category heading. The listing pages, by
contrast, render a complete card per article, so this scraper reads the listings
and never visits a product page. Each pack size is already its own card, which
is exactly the one-row-per-variant shape the dump wants.

Both the gross and the net price are printed ("4,97 € (4,18 € HT)"), so the VAT
basis is observed rather than assumed.
"""

from __future__ import annotations

import html as html_lib
import re
from typing import Any
from urllib.parse import urljoin, urlparse

from . import domain
from . import record as record_module
from .pagecrawl import PageScraper, canonical

CARD = re.compile(
    r'<div[^>]+class="product\b[^"]*"[^>]*>(.*?)<!--\s*/product',
    re.I | re.S,
)
NAME = re.compile(r'<p[^>]+class="text-sm[^"]*"[^>]*>(.*?)</p>', re.I | re.S)
CODE = re.compile(r'<p[^>]+class="p mb-1"[^>]*>(.*?)</p>', re.I | re.S)
PRICE = re.compile(
    r'([0-9]{1,3}(?:[.\s][0-9]{3})*,[0-9]{2})\s*(?:&euro;|€)'
    r'(?:\s*<i[^>]*>\s*\(?\s*([0-9]{1,3}(?:[.\s][0-9]{3})*,[0-9]{2})\s*(?:&euro;|€)\s*HT)?',
    re.I,
)
LINK = re.compile(r'href="([^"]*_[A-Za-z0-9.\-]+\.html[^"]*)"', re.I)
IMAGE = re.compile(r'<img[^>]+src="([^"]+)"', re.I)


class KeramikKraftScraper(PageScraper):
    platform = "custom"
    method = "dom"

    async def scrape(self, limit: int | None = None) -> Any:
        """Walk the material sections and read the article cards on each listing."""
        queue = [urljoin(self.base_url, path) for path in (self.config.get("category_paths") or [])] or [self.base_url]
        seen: set[str] = set()
        origin = urlparse(self.base_url).netloc
        page_limit = self.config.get("category_page_limit", 150)

        while queue and len(seen) < page_limit:
            if limit is not None and len(self.result.records) >= limit:
                self.result.truncated = True
                break
            url = queue.pop(0)
            if url in seen:
                continue
            seen.add(url)
            document = await self.load(url)
            if document is None:
                self.enumeration_failed(url, "listing page could not be read")
                continue
            found = self.read_cards(document, url)
            self.result.discovered += found
            for candidate in self.category_links(document, url, origin):
                if candidate not in seen and candidate not in queue:
                    queue.append(candidate)
        self.result.truncated = self.result.truncated or bool(queue)
        return self.result

    def category_links(self, document: str, page_url: str, origin: str) -> list[str]:
        found = []
        for href in re.findall(r'href="([^"]+)"', document):
            candidate = canonical(urljoin(page_url, html_lib.unescape(href)))
            if urlparse(candidate).netloc == origin and self._is_category(candidate):
                found.append(candidate)
        return list(dict.fromkeys(found))

    def _is_category(self, url: str) -> bool:
        """Category pages are the .html tree under the language prefix."""
        path = urlparse(url).path
        if not path.endswith(".html") or self.is_product_url(url):
            return False
        if re.search(r"/(?:error_page|menu|index\d*)\.html$", path, re.I):
            return False
        return bool(re.match(r"^/[a-z]{2}/", path))

    def is_product_url(self, url: str) -> bool:
        return bool(re.search(r"_[A-Za-z0-9.\-]+\.html", url))

    def read_cards(self, document: str, page_url: str) -> int:
        """Turn each article card on a listing page into one variant row."""
        categories = self._breadcrumb(document, page_url)
        count = 0
        for match in CARD.finditer(document):
            card = match.group(1)
            name_match = NAME.search(card)
            price_match = PRICE.search(card)
            if not name_match or not price_match:
                continue
            count += 1
            # "Gl. Craquelé Karibik 1020-1080°C<br/>==>1020-1080°C Bt. á 0,25 kg"
            parts = [
                domain.clean(part).lstrip("=>").strip()
                for part in re.split(r"<br\s*/?>", name_match.group(1))
            ]
            parts = [part for part in parts if part]
            name, variant = (parts[0], " ".join(parts[1:])) if parts else ("", "")
            if not name:
                continue
            gross, _ = record_module.parse_price(price_match.group(1))
            net, _ = record_module.parse_price(price_match.group(2)) if price_match.group(2) else (None, None)
            if gross is None:
                continue
            code = domain.clean(CODE.search(card).group(1)) if CODE.search(card) else ""
            link = LINK.search(card)
            product_url = canonical(urljoin(page_url, html_lib.unescape(link.group(1)))) if link else page_url
            # Some cards omit the pack line, but the article URL always encodes
            # it between the name and the trailing article code.
            variant = variant or self._variant_from_url(product_url)
            image = IMAGE.search(card)
            brand = self._brand(name) or self.config.get("brand")
            attributes = {"Netto-Preis EUR": net} if net is not None else {}
            row = record_module.build(
                source=self.name,
                product_url=product_url,
                parent_url=product_url,
                name=f"{name} {variant}".strip(),
                product_name=name,
                variant_title=variant or None,
                brand=brand,
                manufacturer_sku=domain.manufacturer_code(brand, name, code),
                supplier_reference=code or None,
                category_path=categories or None,
                image_url=urljoin(page_url, html_lib.unescape(image.group(1))) if image else None,
                price=gross,
                currency="EUR",
                price_text=domain.clean(price_match.group(0)) or None,
                vat="inclusive",
                vat_rate=self.config.get("vat_rate"),
                availability="https://schema.org/InStock",
                technical_attributes=attributes or None,
                extraction_method=self.method,
                source_detail_level="listing",
                raw={"page": page_url, "code": code, "gross": gross, "net": net, "variant": variant},
            )
            self.add(row, self.category_allows(" ".join(categories), name))
        return count

    @staticmethod
    def _variant_from_url(product_url: str) -> str:
        """Read the pack description encoded in an article URL.

        `...Aneto-3D-1230_-Hubel-á-5-kg_T763.html` -> `Hubel á 5 kg`
        """
        stem = urlparse(product_url).path.rsplit("/", 1)[-1].removesuffix(".html")
        parts = stem.split("_")
        if len(parts) < 3:
            return ""
        return " ".join(part for part in "_".join(parts[1:-1]).split("-") if part).strip()

    @staticmethod
    def _breadcrumb(document: str, page_url: str) -> list[str]:
        """The section path is the URL's own directory names."""
        parts = urlparse(page_url).path.rsplit("/", 1)[0].strip("/").split("/")
        return [part.replace("--", " - ").replace("-", " ") for part in parts[1:] if part]

    @staticmethod
    def _brand(name: str) -> str | None:
        known = ("Botz", "Mayco", "Duncan", "Amaco", "Terracolor", "Ceraline", "Wolbring")
        for maker in known:
            if re.search(rf"\b{maker}\b", name, re.I):
                return maker
        return None
