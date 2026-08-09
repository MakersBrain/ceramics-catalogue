"""Ceramicolours (ceramicolours.it), a bespoke PHP storefront.

Listing pages carry the code, name, image and firing temperature; the product
page adds the published price and the pack sizes the shop offers.

Per-pack prices exist only after the page's own `updatePrice()` runs, so the
product page is opened in the browser and each pack option is selected in turn
to read the figure the shop itself displays. That matters: the pricing is not
linear, so multiplying a unit price by a pack size would have been wrong as well
as invented (25 kg is priced at 17.13 EUR/kg where 1 kg is 26.65 EUR/kg).
"""

from __future__ import annotations

import html as html_lib
import re
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

from . import domain
from . import record as record_module
from .pagecrawl import PageScraper, canonical

CARD = re.compile(
    r'<a href="(Articolo\.php\?[^"]+)"[^>]*class="product-name">(.*?)</a>', re.I | re.S,
)


class CeramicoloursScraper(PageScraper):
    platform = "custom"
    method = "dom"

    async def discover_from_sitemaps(self) -> list[str]:
        return []  # The site publishes no sitemap.

    async def discover_from_categories(self, limit: int | None = None) -> list[str]:
        """Read the category ids the config allows, then page through each."""
        wanted = {str(value) for value in (self.config.get("category_ids") or [])}
        home = await self.load(self.base_url)
        if home is None:
            # Every category is discovered from this one page, so failing it is
            # failing to enumerate the shop rather than finding it empty.
            self.enumeration_failed(self.base_url, "the category index could not be read")
            return []
        categories = []
        for href in re.findall(r'href="(Articoli\.php\?[^"]+)"', home, re.I):
            url = urljoin(self.base_url, html_lib.unescape(href))
            identifier = parse_qs(urlparse(url).query).get("Id", [""])[0]
            if not wanted or identifier in wanted:
                categories.append(re.sub(r"&page=\d+", "", url))
        categories = list(dict.fromkeys(categories))
        self.note(f"{len(categories)} in-scope category pages")

        products: list[str] = []
        for category in categories:
            for page in range(1, self.config.get("category_page_limit", 25) + 1):
                document = await self.load(f"{category}&page={page}")
                if document is None:
                    # Ending the walk here leaves the rest of this category
                    # unseen, and a dump missing them is not a shop that stopped
                    # selling them — `plan_load` would retire every one.
                    self.enumeration_failed(f"{category}&page={page}", "category page could not be read")
                    break
                found = [
                    canonical(urljoin(self.base_url, html_lib.unescape(match.group(1))))
                    for match in CARD.finditer(document)
                ]
                fresh = [url for url in found if url not in products]
                products.extend(fresh)
                if not fresh:
                    break
        return products

    def is_product_url(self, url: str) -> bool:
        return "Articolo.php" in url

    #: Selects every pack in turn and reads the price the page computes for it.
    PACK_PRICE_SCRIPT = """
    async () => {
      const select = document.querySelector('#product-pack-field');
      const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
      const read = id => {
        const node = document.querySelector(id);
        return node ? node.textContent.trim() : '';
      };
      if (!select) return [];
      const results = [];
      for (const option of Array.from(select.options)) {
        select.value = option.value;
        select.dispatchEvent(new Event('change'));
        if (typeof updatePrice === 'function') { try { updatePrice(); } catch (error) {} }
        await wait(500);
        results.push({
          pack: option.textContent.trim(),
          value: option.value,
          price: read('#product-price'),
          unit_price: read('#product-unit-price'),
        });
      }
      return results;
    }
    """

    async def parse_page(self, document: str, url: str) -> list[tuple[dict[str, Any], bool | None]]:
        packs: list[dict[str, Any]] = []
        if "product-pack-field" in document:
            try:
                packs = await self.fetcher.evaluate_in_browser(
                    url, self.PACK_PRICE_SCRIPT, wait_ms=1500, wait_for="#product-pack-field",
                ) or []
                self.result.rendered_pages += 1
            except Exception as error:  # noqa: BLE001 - fall back to the static price
                self.fail(url, f"pack pricing unavailable: {error}")
        return self.parse(document, url, packs)

    def parse(
        self, document: str, url: str, packs: list[dict[str, Any]] | None = None,
    ) -> list[tuple[dict[str, Any], bool | None]]:
        name = self._text(document, r'<h1[^>]*>(.*?)</h1>') or self._text(
            document, r'class="product-name"[^>]*>(.*?)</a>',
        )
        if not name:
            return []
        code = parse_qs(urlparse(url).query).get("cod", [""])[0]
        # The listing and product pages state the firing temperature next to a
        # "Temp." label rather than in any structured field.
        temperature = self._text(document, r'Temp\.\s*</span>\s*(.*?)</p>')
        attributes = {}
        if temperature:
            attributes["Temperatura"] = temperature
        images = [
            urljoin(url, html_lib.unescape(src))
            for src in re.findall(r'<img[^>]+src="([^"]*upload-immagini[^"]*)"', document, re.I)
        ]
        description = self._text(document, r'class="product-description"[^>]*>(.*?)</div>')
        breadcrumb = [
            domain.clean(value)
            for value in re.findall(r'<li[^>]*class="breadcrumb[^"]*"[^>]*>.*?<a[^>]*>(.*?)</a>', document, re.I | re.S)
        ]
        common = dict(
            source=self.name,
            product_url=canonical(url),
            parent_url=canonical(url),
            name=name,
            product_name=name,
            brand=self.config.get("brand"),
            supplier_reference=domain.clean(code) or None,
            description=description or None,
            category_path=[value for value in breadcrumb if value] or None,
            image_url=images[0] if images else None,
            all_image_urls=list(dict.fromkeys(images)) or None,
            currency="EUR",
            # #product-price is labelled "Prezzo (IVA inclusa)" on the page.
            vat=self.config.get("vat_status", "inclusive"),
            extraction_method="browser",
            source_detail_level="product_page",
        )

        rows = []
        for pack in packs or []:
            price, _ = record_module.parse_price(pack.get("price"))
            if price is None:
                continue
            # Pack values are kilograms in this shop's own selector.
            label = f"{domain.clean(pack.get('pack'))} kg".strip()
            rows.append((
                record_module.build(
                    **common,
                    variant_id=str(pack.get("value") or ""),
                    variant_title=label,
                    price=price,
                    price_text=domain.clean(pack.get("price")) or None,
                    technical_attributes=(
                        attributes | {"Confezione": label, "Prezzo unitario": domain.clean(pack.get("unit_price"))}
                    ) or None,
                    raw={"url": url, "code": code, "temperature": temperature, "pack": pack},
                ),
                True,  # The configured category ids already scope this source.
            ))
        if rows:
            return rows

        # No pack selector, or the browser was unavailable: use the stated price.
        price, _ = record_module.parse_price(self._text(document, r'Prezzo:\s*</span>\s*(.*?)</p>'))
        if price is None:
            return []
        return [(
            record_module.build(
                **{**common, "extraction_method": self.method},
                price=price,
                technical_attributes=attributes or None,
                raw={"url": url, "code": code, "temperature": temperature},
            ),
            True,
        )]

    @staticmethod
    def _text(document: str, pattern: str) -> str:
        match = re.search(pattern, document, re.I | re.S)
        return domain.clean(match.group(1)) if match else ""
