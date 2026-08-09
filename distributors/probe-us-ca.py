"""Probe the US/CA distributor domains for platform, open API and catalogue size.

The same five read-only passes `scrape-candidates.md` describes for the EU list:
homepage fingerprint, robots/sitemap harvest, open-API test, structured-data test
on a product page, and a brand-token count off the sitemap or the shop's own
search API. One host at a time, a second between that host's requests.

    python probe-us-ca.py [outdir]

Writes scrape-candidates-us-ca.csv.
"""

from __future__ import annotations

import asyncio
import csv
import json
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx

HERE = Path(__file__).resolve().parent
OUT = Path(sys.argv[1] if len(sys.argv) > 1 else HERE)
SOURCES = HERE.parent / "catalogue-dump" / "sources.json"

try:  # optional: only needed for the hosts that refuse a plain client
    from curl_cffi import requests as curl_requests
except ImportError:  # pragma: no cover - the probe degrades to httpx alone
    curl_requests = None

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
# A bare User-Agent is not enough for several of these shops; the WAFs look at
# the whole header set. Anything still 403 after this gets the curl_cffi retry.
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "sec-ch-ua": '"Chromium";v="140", "Not=A?Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Linux"',
}
HOSTS_IN_PARALLEL = 8
PER_HOST_DELAY = 1.0
TIMEOUT = 25.0

BRANDS = [
    "mayco", "amaco", "spectrum", "duncan", "botz", "speedball",
    "laguna", "coyote", "georgies", "standard", "terracolor", "sio-2",
]
GLAZE_WORDS = ("glaze", "underglaze", "engobe", "clay", "kiln", "pottery", "ceramic")

# Homepage markers, most specific first.
PLATFORM_MARKERS = [
    ("shopify", ("cdn.shopify.com", "Shopify.theme", "shopify-section")),
    ("woocommerce", ("woocommerce", "wp-content/plugins/woocommerce")),
    ("bigcommerce", ("cdn11.bigcommerce.com", "bigcommerce.com/s-")),
    ("magento", ("Magento_", "mage/cookies", "static/version")),
    ("squarespace", ("static1.squarespace.com", "squarespace-cdn")),
    ("wix", ("static.parastorage.com", "wixstatic.com")),
    ("prestashop", ("prestashop", "/modules/ps_")),
    ("shopware", ("shopware", "/widgets/checkout")),
    ("ecwid", ("app.ecwid.com", "ecwid.com/script.js")),
    ("lightspeed", ("lightspeedhq", "assets.webshopapp.com")),
    ("opencart", ("index.php?route=common", "catalog/view/theme")),
    ("volusion", ("volusion.com",)),
    ("wordpress", ("wp-content", "wp-includes")),
]

CART_SIGNALS = ("add to cart", "add-to-cart", "/cart", "shopping cart", "my basket")


def brand_hits(blob: str) -> str:
    blob = blob.lower()
    hits = [(b, blob.count(b)) for b in BRANDS]
    return ";".join(f"{b}:{n}" for b, n in sorted(hits, key=lambda x: -x[1]) if n)


def hits_total(spec: str) -> int:
    return sum(int(p.split(":")[1]) for p in spec.split(";") if ":" in p)


class Probe:
    def __init__(self, domain: str, listed_by: str, name: str, country: str):
        self.domain = domain
        self.listed_by = listed_by
        self.name = name
        self.country = country
        self.base = ""
        self.platform = "unknown"
        self.access = ""
        self.size = 0
        self.size_source = ""
        self.brands = ""
        self.shop = ""
        self.glaze_urls = 0
        self.note = ""
        self.tier = "D"

    def row(self) -> dict:
        score = self.size + hits_total(self.brands) * 3 + self.glaze_urls
        return {
            "tier": self.tier,
            "domain": self.domain,
            "country": self.country,
            "name": self.name,
            "listed_by": self.listed_by,
            "platform": self.platform,
            "access": self.access,
            "catalogue_size": self.size,
            "size_source": self.size_source,
            "brand_hits": self.brands,
            "shop_signals": self.shop,
            "glaze_urls": self.glaze_urls,
            "score": score,
            "url": self.base or f"https://{self.domain}",
            "note": self.note,
        }


class Reply:
    """The two response shapes the probe needs, from either transport."""

    def __init__(self, status: int, text: str, content: bytes, headers: dict, url: str):
        self.status_code = status
        self.text = text
        self.content = content
        self.headers = {k.lower(): v for k, v in headers.items()}
        self.url = url

    def json(self):
        return json.loads(self.text)


def _impersonate(url: str) -> Reply | None:
    if curl_requests is None:
        return None
    try:
        r = curl_requests.get(url, impersonate="chrome", timeout=TIMEOUT)
    except Exception:  # noqa: BLE001 - a failed probe is a result
        return None
    return Reply(r.status_code, r.text, r.content, dict(r.headers), str(r.url))


async def fetch(client: httpx.AsyncClient, url: str, **kw) -> Reply | None:
    reply: Reply | None = None
    try:
        r = await client.get(url, **kw)
        reply = Reply(r.status_code, r.text, r.content, dict(r.headers), str(r.url))
    except (httpx.HTTPError, ValueError, UnicodeError):
        reply = None
    if reply is None or reply.status_code in (403, 429):
        # TLS fingerprint, not headers. Costs a thread, so only on refusal.
        retry = await asyncio.to_thread(_impersonate, url)
        if retry is not None and retry.status_code < 400:
            reply = retry
    await asyncio.sleep(PER_HOST_DELAY)
    return reply


async def homepage(client: httpx.AsyncClient, p: Probe) -> str:
    for candidate in (f"https://{p.domain}", f"https://www.{p.domain}", f"http://{p.domain}"):
        r = await fetch(client, candidate)
        if r is not None and r.status_code < 400 and r.text:
            p.base = r.url
            return r.text
        if r is not None and r.status_code in (403, 429):
            p.base = r.url
            p.note = f"homepage {r.status_code} (WAF; needs a browser)"
            return ""
    p.note = p.note or "unreachable"
    p.tier = "X"
    return ""


def fingerprint(p: Probe, html: str) -> None:
    low = html.lower()
    for label, markers in PLATFORM_MARKERS:
        if any(m.lower() in low for m in markers):
            p.platform = label
            break
    signals = [s for s in CART_SIGNALS if s in low]
    p.shop = "cart" if signals else ""


async def open_api(client: httpx.AsyncClient, p: Probe) -> None:
    """Shopify products.json and the WooCommerce Store API, in that order."""
    if p.platform in ("shopify", "unknown", "wordpress"):
        r = await fetch(client, urljoin(p.base, "/products.json?limit=250"))
        if r is not None and r.status_code == 200 and "application/json" in r.headers.get("content-type", ""):
            try:
                items = r.json().get("products", [])
            except (json.JSONDecodeError, ValueError):
                items = []
            if items:
                p.platform = "shopify"
                p.access = "shopify /products.json"
                p.size = len(items)
                p.size_source = "api page1 (250 cap)" if len(items) >= 250 else "api page1"
                blob = " ".join(
                    f"{i.get('vendor', '')} {i.get('title', '')} {i.get('product_type', '')}"
                    for i in items
                )
                p.brands = brand_hits(blob)
                return
    if p.platform in ("woocommerce", "wordpress", "unknown"):
        r = await fetch(client, urljoin(p.base, "/wp-json/wc/store/v1/products?per_page=100"))
        if r is not None and r.status_code == 200:
            try:
                items = r.json()
            except (json.JSONDecodeError, ValueError):
                items = []
            if isinstance(items, list) and items:
                p.platform = "woocommerce"
                p.access = "woo store api v1"
                total = r.headers.get("x-wp-total")
                p.size = int(total) if total and total.isdigit() else len(items)
                p.size_source = "x-wp-total" if total else "api page1"
                blob = " ".join(str(i.get("name", "")) for i in items)
                p.brands = brand_hits(blob)
                return


async def sitemaps(client: httpx.AsyncClient, p: Probe) -> None:
    """Harvest product URLs from robots.txt sitemaps, one level of index."""
    urls: list[str] = []
    seen_maps: list[str] = []
    r = await fetch(client, urljoin(p.base, "/robots.txt"))
    if r is not None and r.status_code == 200:
        # robots.txt may give these relative; the RFC says absolute, sites differ.
        seen_maps += [urljoin(p.base, u) for u in re.findall(r"(?im)^\s*sitemap:\s*(\S+)", r.text)]
    if not seen_maps:
        seen_maps = [urljoin(p.base, "/sitemap.xml")]
    collected: list[str] = []
    for sm in seen_maps[:3]:
        r = await fetch(client, sm)
        if r is None or r.status_code != 200:
            continue
        try:
            root = ET.fromstring(r.content)
        except ET.ParseError:
            continue
        locs = [e.text.strip() for e in root.iter() if e.tag.endswith("}loc") and e.text]
        if root.tag.endswith("}sitemapindex"):
            child = [u for u in locs if re.search(r"product|shop|item", u, re.I)] or locs
            for sub in child[:4]:
                rr = await fetch(client, sub)
                if rr is None or rr.status_code != 200:
                    continue
                try:
                    sub_root = ET.fromstring(rr.content)
                except ET.ParseError:
                    continue
                collected += [e.text.strip() for e in sub_root.iter() if e.tag.endswith("}loc") and e.text]
        else:
            collected += locs
        if len(collected) > 20000:
            break
    urls = collected
    if not urls:
        return
    product_urls = [u for u in urls if re.search(r"/(product|products|shop|p)/", u, re.I)]
    if not p.size:
        p.size = len(product_urls) or len(urls)
        p.size_source = "sitemap product urls" if product_urls else "sitemap urls"
    if not p.brands:
        p.brands = brand_hits(" ".join(urls))
    p.glaze_urls = sum(1 for u in urls if any(w in u.lower() for w in GLAZE_WORDS))
    if not p.shop and product_urls:
        p.shop = "product urls"


async def structured_data(client: httpx.AsyncClient, p: Probe) -> None:
    """If no open API, check whether a product page carries json-ld or microdata."""
    if p.access or not p.base:
        return
    r = await fetch(client, p.base)
    if r is None or r.status_code != 200:
        return
    links = re.findall(r'href="([^"]+)"', r.text)
    cand = [urljoin(p.base, u) for u in links if re.search(r"/(product|products|shop)/[^\"/]+", u, re.I)]
    if not cand:
        return
    rr = await fetch(client, cand[0])
    if rr is None or rr.status_code != 200:
        return
    body = rr.text
    if re.search(r'"@type"\s*:\s*"Product"', body):
        p.access = "json-ld Product"
    elif "itemtype" in body and "schema.org/Product" in body:
        p.access = "microdata Product"


def tier(p: Probe) -> str:
    if p.tier == "X":
        return "X"
    if p.access in ("shopify /products.json", "woo store api v1"):
        return "A"
    if p.access in ("json-ld Product", "microdata Product"):
        return "B"
    if p.shop or p.size:
        return "C"
    return "D"


async def probe_one(sem: asyncio.Semaphore, p: Probe) -> dict:
    async with sem:
        async with httpx.AsyncClient(
            headers=HEADERS, follow_redirects=True, timeout=TIMEOUT, verify=False
        ) as client:
            html = await homepage(client, p)
            if html:
                fingerprint(p, html)
                await open_api(client, p)
                await sitemaps(client, p)
                await structured_data(client, p)
            p.tier = tier(p)
    print(f"  {p.tier} {p.domain:38s} {p.platform:12s} {p.access or '-':22s} size={p.size}")
    return p.row()


def candidates() -> list[Probe]:
    have = set()
    if SOURCES.exists():
        blob = SOURCES.read_text()
        have = {m.lower().removeprefix("www.") for m in re.findall(r"https?://([^/\s\"]+)", blob)}
    # Brand-owned and non-retail domains: their catalogues are the brands themselves.
    skip_exact = {
        "amaco.com", "shop.amaco.com", "maycocolors.com", "spectrumglazes.com",
        "sio-2.com", "speedballart.com", "duncanceramics.com",
    }
    files = [
        ("spectrum-us-ca-distributors.csv", "spectrum"),
        ("amaco-us-ca-distributors.csv", "amaco"),
        ("mayco-us-ca-distributors-dealers.csv", "mayco"),
    ]
    by_domain: dict[str, Probe] = {}
    listed: dict[str, set[str]] = {}
    for fname, brand in files:
        path = HERE / fname
        for row in csv.DictReader(path.open(encoding="utf-8")):
            site = row["website_as_listed"].strip()
            if not site:
                continue
            host = urlparse(site if "//" in site else f"https://{site}").netloc.lower()
            host = host.removeprefix("www.").split(":")[0]
            if not host or host in skip_exact or host in have:
                continue
            listed.setdefault(host, set()).add(brand)
            if host not in by_domain:
                by_domain[host] = Probe(host, "", row["name"], row["country"])
    for host, p in by_domain.items():
        p.listed_by = "/".join(sorted(listed[host]))
    return list(by_domain.values())


async def main() -> None:
    probes = candidates()
    print(f"{len(probes)} candidate domains")
    sem = asyncio.Semaphore(HOSTS_IN_PARALLEL)
    rows = await asyncio.gather(*(probe_one(sem, p) for p in probes))
    rows.sort(key=lambda r: (r["tier"], -r["score"]))
    path = OUT / "scrape-candidates-us-ca.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()), lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    print(f"\n{path}: {len(rows)} rows")
    print(dict(Counter(r["tier"] for r in rows)))


if __name__ == "__main__":
    asyncio.run(main())
