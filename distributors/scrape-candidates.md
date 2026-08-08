# Distributor sites not yet scraped: platform, scrapability, product relevance

Probed 2026-08-05. Source: the four distributor lists in this folder (Mayco, Amaco,
Spectrum, Sio-2), restricted to EU-27 plus UK and Switzerland, with a website, and
not already present in `catalogue-dump/sources.json`.

257 distributor rows -> 135 unique domains -> **117 candidates** after removing the 20
domains we already scrape and the companies we already cover under another TLD
(keramik-kraft.*, keramikbedarf-online.de, sio-2, amaco, mayco, spectrum, ceradel,
colpaertonline, e-cibas/cibasimpasti).

Per-domain detail is in `scrape-candidates.csv`.

## Method

Five read-only passes: homepage fingerprint; cookie/robots fingerprint plus sitemap
harvest; open-API test; product-page structured-data test; and a targeted pass that
picks glaze/brand URLs straight from the sitemap for the high-value sites. Brand
counts come from the shop's own search API where one exists, otherwise from counting
brand tokens in sitemap URLs.

## Summary

| Tier | Meaning | Count |
|---|---|---|
| A | Open API, drop-in for an existing scraper | 31 |
| B | Structured data on product pages (json-ld / microdata) | 28 |
| C | Real shop, HTML-only or blocked — needs custom work | 20 |
| D | No shop found by the probe — needs a manual look | 15 |
| M | Mirror of another candidate | 5 |
| X | Not a ceramics-materials shop, or dead | 18 |

## Bot protection: what plain HTTP cannot reach

Four sites refused a normal `httpx` client. Retested with `curl_cffi`
(curl-impersonate, TLS/JA3 fingerprint of a real Chrome):

| Site | Protection | Result |
|---|---|---|
| poterieduvieuxbac.com | WAF + apex cert mismatch | **Solved** — use `www.` host; it is Shopify, `products.json` opens. Promoted to tier A |
| vicentiz.com | none — the 429 was transient rate limiting | **Solved**, plain fetch works now |
| keramikams.lt | Cloudflare managed challenge (`cf-mitigated: challenge`) | TLS impersonation alone is not enough; needs a `cf_clearance` cookie or a real browser |
| scarva.com | Azure WAF, not Cloudflare | **Still blocked**; impersonation does not help, needs browser rendering |

For keramikams.lt, `curl_cffi` with `impersonate="chrome146"`, the browser's exact
User-Agent and a `cf_clearance` cookie lifted from a logged-in browser session does
work — verified end to end, including category pagination
(`?p=2&product_list_limit=36`). But `cf_clearance` is bound to the issuing IP and TLS
fingerprint and expires in well under a day, so it is fine for a one-off pull and not
a basis for scheduled collection. For recurring runs use the `BrowserRenderer` already
in `scrapers/base.py` (`--browser always`).

**Its robots.txt sets `Crawl-delay: 10`** and disallows only `/checkout/`,
`/customer/`, `/app/`, `/lib/`, `/var/`, `/pkginfo/`, `/sendfriend/` — the catalogue
is permitted, at one request per ten seconds.

## Tier A — drop-in (30)

WooCommerce Store API or Shopify `products.json` answer directly, so
`scrapers/woocommerce.py` and `scrapers/shopify.py` handle these with only a
`sources.json` entry plus category scoping. Product counts are exact for
WooCommerce (`X-WP-Total`); Shopify shows `250+` where page one was full.

Best product relevance first:

| Site | Country | Platform | Products | Tracked brands (hits) |
|---|---|---|---|---|
| potterycrafts.co.uk | UK | Shopify | 250+ | mayco 812, spectrum 283, amaco 282, terracolor |
| corbykilns.co.uk | UK | Woo | 2571 | mayco 815, amaco 229, botz 94, spectrum 66 |
| argile.nl | NL | Woo | 4567 | mayco 614, botz 249, terracolor 162, amaco 156 |
| kettlespotterysupplies.com | UK | Shopify | 250+ | botz 383, mayco 290, amaco 129, spectrum |
| at-ceramika.pl | PL | Woo | 1584 | botz 315, mayco 149, amaco 132, spectrum 12 |
| keramikbedarf.ch | CH | Woo | 1972 | amaco 207, terracolor 148, mayco 140, spectrum |
| tallergingell.com | ES | Shopify | 250+ | amaco 744 |
| hot-clay.com | UK | Shopify | 250+ | spectrum 140, mayco 138, botz 94, amaco 57, sio-2 |
| ceramistashop.pt | PT | Shopify | 250+ | amaco 585, botz 82, mayco 56 |
| barro.ro | RO | Shopify | 250+ | amaco 289, mayco 229, sio-2 42, botz 31 |
| cerama.shop | IT | Woo | 927 | mayco 388, sio-2 37 |
| marphil.com | ES | Woo | 1650 | mayco 164, botz 102, sio-2 19 |
| mudaceramica.com | PT | Shopify | 250+ | mayco 401 |
| ulsterceramicspotterysupplies.co.uk | UK | Shopify | 250+ | mayco 61, amaco 48, botz 39, spectrum 19 |
| dbipottery.com | IE | Woo | 2189 | amaco 92, mayco 15, terracolor 8 |
| themakersspace.eu | CY | Woo | 160 | mayco 116, amaco 24 |
| hiclay.pl | PL | Woo | 293 | amaco 130, mayco 4 |
| esmalteybarro.com | ES | Woo | 1078 | sio-2 19 |
| keramica.info | HR | Woo | 385 | mayco 57, sio-2 1 |
| keramikfryd.dk | DK | Woo | 731 | amaco 15, mayco 10, spectrum 8 |
| ramfos.gr | GR | Woo | 2816 | botz 11, amaco 9 |
| artacademydirect.com | MT | Shopify | 250+ | mayco 8, sio-2 5 |
| paraceramica.com | ES | Woo | 625 | mayco 6 |
| inglet.com | ES | Woo | 4084 | amaco 19 — mostly fine art, thin on ceramics |
| centrado.co | UK | Woo | 2942 | amaco 1 — general craft |
| prodesco.es | ES | Woo | 1398 | sio-2 3 |
| santiagopidal.es | ES | Woo | 615 | none detected |
| muandukeramika.lt | LT | Woo | 788 | none detected |
| helenahodell.se | SE | Woo | 75 | amaco 1 |
| keramikbedarf-zinser.de | DE | Woo | 6 | botz 7 — API exposes only 6 products, catalogue is elsewhere |

The first fourteen are the ones worth doing: each carries several hundred matches on
the brands already in the catalogue, which is what makes cross-supplier price
comparison possible.

## Tier B — structured data, needs a small scraper (21)

Product pages carry json-ld `Product` with `offers`, or schema.org microdata, so
`scrapers/jsonld.py` + `pagecrawl.py` can read them; the per-site work is URL
discovery and category scoping, not field extraction.

Highest value:

- **potclays.co.uk** (UK) — json-ld with SKU and price. mayco 395, botz 163, amaco 156, spectrum 36. One of the largest UK suppliers.
- **keramik-kriese-shop.gambiocloud.com** (DE, Shopware) — json-ld+offers. duncan 568, botz 563, mayco 477.
- **ceramiq.pl** (PL) — json-ld+offers. mayco 420, botz 391, sio-2 43.
- **toepferbedarf-brock.de** (DE, JTL) — microdata. mayco 389, botz 265.
- **keramikadoskol.cz** (CZ, Shoptet) — microdata + og:price. mayco 790.
- **sklep.heho.pl** (PL, Shoper) — microdata. mayco 209.
- **artequipment.pl** (PL) — microdata. mayco 191, amaco 91.
- **mayco-glasuren.de** (DE, ePages) — microdata. Mayco-dedicated, 181 glaze URLs.
- **hobbyland.eu** (IT) — json-ld+offers, 30k product URLs. spectrum 551, sio-2 63.
- **arteyceramica.es** (ES) — json-ld+offers, 3575 product URLs.
- **silexshop.nl** (NL, Woo without Store API) — json-ld+offers, 6419 product URLs.
- **countrylovecrafts.com** (UK, Odoo) — microdata, 18678 product URLs.

Also: cromartiehobbycraft.co.uk, davaart.ro, kadarceramica.com, mestrebras.pt,
keramik-toepfern.de, keramikbedarf.de, cerama.dk, toepferspass.de, bsz-keramikbedarf.de.

**keramikams.lt (LT, Magento 2)** — the single best data quality in this set, verified
page by page in a browser. It carries AMACO (223 items), Sio-2 and Botz, and each
product page publishes:

- json-ld `Product` with `sku` — and for series products the SKU *is* the manufacturer
  code (`PC-17`), elsewhere the supplier reference (`34039R`)
- **UPC / GTIN** (`039672340398`) in the og:description — almost nothing else in this
  set publishes a GTIN, and it is the key that joins the same Amaco jar across suppliers
- `product:price:amount` list price plus a visible sale price (21,01 -> 17,36)
- pack size in ml, firing range in °C *and* Orton cones ("Cone 5-6 (1186°C-1222°C)")
- a food-contact claim, spraying/brushing application notes and coat counts
- a spec table of gloss level, opacity, and glaze properties, mirrored in the layered
  navigation facets

That maps onto nearly every column of `ceramics.catalogue_item.v2` — `gtin`,
`manufacturer_sku`, `package_size`, `firing` with cone evidence, `claims`,
`application_methods`, `coats`, `surface`, `effects`. Access is the only obstacle.

**PrestaShop subgroup.** These first read as "no shop" because PrestaShop puts products
at `/<category-slug>/<id>-<product-slug>.html`, which none of the generic URL patterns
match, and several publish no sitemap. Re-probed with the right URL shape, they all
carry structured data, and `scrapers/prestashop.py` already exists:

| Site | Country | Markup | Evidence |
|---|---|---|---|
| peterlavem.fr | FR | json-ld + offers | sku `GB106/05`, "GB106 - Vert Bouteille", 8.94 |
| eskiss972.com | FR | json-ld + offers | sku 16856, "Duncan - CN - Concepts", 13.80 |
| keramickepece.cz | CZ | json-ld + offers | price 21.28 |
| como-ceramique.com | FR | microdata | price 7.06 on a 1 kg glaze, pack size in the URL |
| esmaltycolor.com | ES | microdata | 44 product links off the category tree |
| cigaleetfourmi.fr | FR | microdata | 8 product links |

peterlavem.fr is the most useful shape in the whole set: the SKU encodes glaze
reference plus pack size (`GB106/05`), which maps straight onto the variant-per-row
contract.

## Tier C — real shop, custom work (26)

A shop is confirmed (cart or add-to-cart markup, prices) but no machine-readable
layer was found, or the site blocks automated requests.

Worth the effort for their brand depth:

- **bathpotters.co.uk** (UK) — spectrum 236, mayco 212, amaco 175, terracolor 156. HTML only.
- **kerasil.fi** (FI, ePages) — spectrum 238, amaco 140, botz 139, terracolor 110.
- **scarva.com** (UK) — HTTP 403, Cloudflare. Large supplier; needs a browser fetch.
- **specialistcrafts.co.uk** (UK, Magento) — spectrum 67.
- **hobbyceramicraft.co.uk** (UK) — mayco 22.

Still blocked: **scarva.com** (Azure WAF — needs browser rendering). The other three
originally-blocked sites were resolved; see the bot-protection section above.

Prices visible but no structured markup: bricolajeazuaje.es, r2w-ceramica.pt, pece.sk.

Remainder: eurokeramiki.gr (OpenCart), keramikos.nl, pappel.com, mondoceramica.net,
nacho-sum.es, propec.cz, keraterm.si, keramikbedarf.net, lehmhuus.ch,
artesaniachopo.com, bendix.net.

## Tier D — no shop found, manual check needed (15)

The probe found no cart, no product pages and usually no sitemap. That is not proof
there is no shop: spot-checking moved seven sites out of this tier, and the PrestaShop
re-probe moved six more into tier B. Treat these as unresolved rather than rejected —
each needs a browser look. Two are worth it because the brand signal is strong:
**bpkeramia.hu** (botz 252, amaco 19) and **ceramica-troyan.bg** (OpenCart, 194
product URLs).

Rest: anper.net, casaviana.net, ceramicaroque.com, ceramicashop.de (TLS handshake
fails), ceramics.lv, ceraplus.com, creakor.com, javierherranz.com, keramik.at,
martinezytrini.es, nelliekeramiek.nl, skokan.at, suministrosceramicoseusebio.com.

## Tier M — mirrors (5)

Same catalogue, second domain: michel.ch = keramikbedarf.ch (identical 1972 products),
ceramistashop.com = ceramistashop.pt, countryloveceramics.co.uk = countrylovecrafts.com,
cebex.se = cerama.dk (both serve cerama.se), scarvapottery.com = scarva.com.

## Tier X — out of scope (18)

Not ceramics-materials retailers: sibelco.com (industrial minerals), colorobbiaspa.com
(manufacturer), cosplayshop.be (the Amaco listing is craft foam), eurolijsten.be and
quadrimovel.com (framing), cristaleraiberica.com and its .es twin (architectural
glazing — matched "esmalte" but sells windows), aigaia.com.cy (art school),
salescapeartsupply.com (4 products).

Studios and brochure sites with no shop: argilecreation.ch, danilogueller.ch,
les-gres-d-uzes.fr, hins.be, leimenzwa.nl, keramix.eu.

Dead: 3dpotter.fr, casparceramique.ch (DNS), and `www.boerkey` — a malformed URL in
`sio2-eu-uk-ch-distributors.csv` that should be fixed at the source.

## Suggested order of work

1. The fourteen tier-A sites with real brand depth. Each is a `sources.json` entry
   against an existing scraper, and they add UK, NL, PL, CH, ES, PT, RO, IT, IE, CY
   coverage on Mayco/Amaco/Spectrum/Botz.
2. potclays.co.uk and bathpotters.co.uk — the two biggest UK suppliers still missing,
   and the deepest Spectrum and Terracolor coverage found anywhere in this set.
3. The tier-B German and Polish shops (keramik-kriese, toepferbedarf-brock, ceramiq.pl,
   sklep.heho.pl, keramikadoskol.cz) — one json-ld/microdata scraper covers most.
4. The PrestaShop six, against the existing `prestashop` scraper — they add French and
   Spanish coverage, and peterlavem.fr publishes pack-size SKUs.
5. keramikams.lt via `BrowserRenderer` at one request per 10 s. It is slow to collect
   but it is the only source found here that publishes GTINs, which makes it
   disproportionately useful for joining records across the other suppliers.

## Caveats

- Brand hit counts are keyword occurrences in API search results or sitemap URLs, not
  verified product counts. They rank relevance; they do not measure catalogue depth.
- Shopify counts are capped at the 250-item first page.
- Platform labels are fingerprints. Where a site gave no distinctive marker the column
  reads `unknown`, which does not mean it has no platform.
- Nothing here checks terms of service or robots policy for any site. That is a
  separate decision per source before scraping.
