"""Pull US/CA distributor and dealer listings for Spectrum, Amaco and Mayco.

Read-only: each brand's own public "where to buy" pages, one polite pass.
Writes three CSVs shaped like the EU files already in distributors/.
"""

from __future__ import annotations

import csv
import difflib
import html
import re
import sys
import time
from pathlib import Path

import httpx

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0 Safari/537.36"
)
DELAY = 1.0

US_STATES = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR",
    "California": "CA", "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE",
    "District of Columbia": "DC", "Florida": "FL", "Georgia": "GA",
    "Hawaii": "HI", "Idaho": "ID", "Illinois": "IL", "Indiana": "IN",
    "Iowa": "IA", "Kansas": "KS", "Kentucky": "KY", "Louisiana": "LA",
    "Maine": "ME", "Maryland": "MD", "Massachusetts": "MA", "Michigan": "MI",
    "Minnesota": "MN", "Mississippi": "MS", "Missouri": "MO", "Montana": "MT",
    "Nebraska": "NE", "Nevada": "NV", "New Hampshire": "NH", "New Jersey": "NJ",
    "New Mexico": "NM", "New York": "NY", "North Carolina": "NC",
    "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK", "Oregon": "OR",
    "Pennsylvania": "PA", "Rhode Island": "RI", "South Carolina": "SC",
    "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX", "Utah": "UT",
    "Vermont": "VT", "Virginia": "VA", "Washington": "WA",
    "West Virginia": "WV", "Wisconsin": "WI", "Wyoming": "WY",
}
CA_PROVINCES = {
    "Alberta": "AB", "British Columbia": "BC", "Manitoba": "MB",
    "New Brunswick": "NB", "Newfoundland and Labrador": "NL",
    "Northwest Territories": "NT", "Nova Scotia": "NS", "Nunavut": "NU",
    "Ontario": "ON", "Prince Edward Island": "PE", "Quebec": "QC",
    "Saskatchewan": "SK", "Yukon": "YT",
}
ABBR_TO_STATE = {v: k for k, v in US_STATES.items()}
ABBR_TO_PROV = {v: k for k, v in CA_PROVINCES.items()}

client = httpx.Client(
    headers={"User-Agent": UA},
    cookies={"fusion_auth_guard": "success"},
    follow_redirects=True,
    timeout=40.0,
)


MISSING: list[str] = []


def get(url: str, optional: bool = False) -> str:
    r = client.get(url)
    time.sleep(DELAY)
    if optional and r.status_code == 404:
        MISSING.append(url)
        return ""
    r.raise_for_status()
    return r.text


def text(fragment: str) -> str:
    """Strip tags, unescape entities, collapse whitespace."""
    s = re.sub(r"(?s)<(script|style).*?</\1>", " ", fragment)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def lines(fragment: str) -> list[str]:
    """Same, but <br> and block ends become line breaks."""
    s = re.sub(r"(?s)<(script|style).*?</\1>", " ", fragment)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"</(p|div|li|h\d|address|span)>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    return [re.sub(r"[ \t]+", " ", ln).strip() for ln in s.split("\n") if ln.strip()]


def resolve_region(addr: str, country: str) -> tuple[str, str]:
    """Return (region, note) for a listed address. Empty region when unclear."""
    table = US_STATES if country == "United States" else CA_PROVINCES
    for full in table:
        if re.search(rf"\b{re.escape(full)}\b", addr, re.I):
            return full, ""
    abbrs = {v: k for k, v in table.items()}
    for token in re.findall(r"\b[A-Za-z]{2}\b", addr):
        if token.upper() in abbrs:
            return abbrs[token.upper()], "Region inferred from the two-letter code in the listed address."
    for segment in re.split(r"[,\n]", addr):
        segment = segment.strip()
        if len(segment) < 4:
            continue
        near = difflib.get_close_matches(segment.title(), list(table), n=1, cutoff=0.9)
        if near:
            return near[0], f'Region matched past a misspelling in the listed address ("{segment}").'
    return "", "Region not stated in the listing."


def dedupe(rows: list[dict]) -> list[dict]:
    """Drop rows the source itself lists twice, identical field for field."""
    seen: set[tuple] = set()
    out: list[dict] = []
    for row in rows:
        key = tuple(sorted(row.items()))
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    if len(out) != len(rows):
        print(f"  dropped {len(rows) - len(out)} exact duplicate rows from the source")
    return out


def write(name: str, header: list[str], rows: list[dict]) -> None:
    path = OUT / name
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=header, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    print(f"{path}: {len(rows)} rows")


# --------------------------------------------------------------------------
# Spectrum: one page per state/province, <h4> name + <p> address block.
# --------------------------------------------------------------------------

SPECTRUM_INDEXES = [
    ("United States", "https://www.spectrumglazes.com/spectrum-distributors-in-the-united-states/", "Glaze"),
    ("Canada", "https://www.spectrumglazes.com/spectrum-distributors-in-canada/", "Glaze"),
    ("United States", "https://www.spectrumglazes.com/distributors/spectrum-stamp-distributors-in-the-united-states/", "Stamp"),
    ("Canada", "https://www.spectrumglazes.com/distributors/spectrum-stamp-distributors-in-canada/", "Stamp"),
]


def spectrum_entry_content(page: str) -> str:
    m = re.search(r'(?s)<div class="entry-content">(.*?)<!-- \.entry-content -->', page)
    return m.group(1) if m else ""


def spectrum() -> list[dict]:
    rows: list[dict] = []
    for country, index_url, product_line in SPECTRUM_INDEXES:
        body = spectrum_entry_content(get(index_url))
        regions = re.findall(r'href="(https://www\.spectrumglazes\.com/[^"]+)"[^>]*>([^<]+)</a>', body)
        for url, region in regions:
            region = html.unescape(region).strip()
            content = spectrum_entry_content(get(url, optional=True))
            # Each listing is an <h4> name followed by one <p> block.
            for m in re.finditer(r"(?s)<h4[^>]*>(.*?)</h4>\s*(<p[^>]*>.*?</p>)", content):
                name = text(m.group(1))
                if not name:
                    continue
                block = m.group(2)
                site = ""
                a = re.search(r'<a[^>]+href="(https?://[^"]+)"', block)
                if a:
                    site = a.group(1)
                parts = lines(block)
                phone = fax = email = ""
                street: list[str] = []
                for ln in parts:
                    low = ln.lower()
                    if re.match(r"(ph|phone|tel|t|toll[- ]?free)\s*[:.]", low):
                        phone = phone or ln.split(":", 1)[-1].strip()
                    elif re.match(r"(fax|fx|f)\s*[:.]", low):
                        fax = fax or ln.split(":", 1)[-1].strip()
                    elif "@" in ln and " " not in ln:
                        email = ln
                    elif re.match(r"^(https?://)?(www\.)?[\w-]+(\.[\w-]+)+/?$", ln):
                        continue  # the bare domain, already captured as the link
                    else:
                        street.append(ln)
                source_address = ", ".join(street)
                note = ""
                rows.append({
                    "country": country,
                    "region": region,
                    "name": name,
                    "address": f"{source_address}, {country}" if source_address else "",
                    "source_address": source_address,
                    "website_as_listed": site,
                    "email_as_listed": email,
                    "phone_as_listed": phone,
                    "fax_as_listed": fax,
                    "product_line": product_line,
                    "type": "Distributor",
                    "address_note": note,
                    "source_url": url,
                })
    return rows


# --------------------------------------------------------------------------
# Amaco: /find-a-distributor?country=USA|Canada, card-location blocks.
# --------------------------------------------------------------------------


def amaco() -> list[dict]:
    rows: list[dict] = []
    for country, param in (("United States", "USA"), ("Canada", "Canada")):
        page = get(f"https://amaco.com/find-a-distributor?country={param}")
        cards = re.split(r'<div class="card-location', page)[1:]
        for card in cards:
            card = card.split('<div class="card-location')[0]
            m = re.search(r'(?s)<h4 class="card-location__title[^"]*">(.*?)</h4>', card)
            if not m:
                continue
            name = text(m.group(1))
            site = ""
            a = re.search(r'<h4 class="card-location__title[^"]*">\s*<a[^>]+href="(https?://[^"]+)"', card, re.S)
            if a:
                site = a.group(1)
            addr = ""
            am = re.search(r'(?s)<p class="card-location__address[^"]*">(.*?)</p>', card)
            if am:
                addr = ", ".join(lines(am.group(1)))
                addr = re.sub(r",\s*,", ",", addr).strip(" ,")
                addr = re.sub(r"\s*,\s*", ", ", addr)
            phone = ""
            pm = re.search(r'href="tel:([^"]+)"', card)
            if pm:
                phone = html.unescape(pm.group(1)).strip()
            email = ""
            em = re.search(r'href="mailto:([^"]+)"', card)
            if em:
                email = html.unescape(em.group(1)).strip()
            region, note = resolve_region(addr, country)
            rows.append({
                "country": country,
                "region": region,
                "name": name,
                "address": f"{addr}, {country}" if addr else "",
                "source_address": addr,
                "website_as_listed": site,
                "email_as_listed": email,
                "phone_as_listed": phone,
                "type": "Distributor",
                "address_note": note,
                "source_url": f"https://amaco.com/find-a-distributor?country={param}",
            })
    return rows


# --------------------------------------------------------------------------
# Mayco: one FacetWP archive page carries every listing worldwide.
# --------------------------------------------------------------------------


def mayco() -> list[dict]:
    page = get("https://www.maycocolors.com/distributors")
    rows: list[dict] = []
    blocks = re.split(r'<div class="distributor">', page)[1:]
    for block in blocks:
        block = block.split('<div class="distributor">')[0]
        m = re.search(r'(?s)<h4 class="dist-title">(.*?)</h4>', block)
        if not m:
            continue
        name = text(m.group(1))
        site = ""
        a = re.search(r'(?s)<h4 class="dist-title">.*?<a[^>]+href="([^"]+)"', block)
        if a:
            site = html.unescape(a.group(1)).strip()
        am = re.search(r"(?s)<address>(.*?)</address>", block)
        addr = text(am.group(1)) if am else ""
        addr = re.sub(r"\s*,\s*", ", ", addr).strip(" ,")
        if not re.search(r"(United States|Canada)\s*$", addr):
            continue
        country = "United States" if addr.endswith("United States") else "Canada"
        kind = "Distributor" if "<span>Distributor</span>" in block else ""
        if not kind:
            km = re.search(r"(?s)carries-products.*?<span>(.*?)</span>", block)
            kind = text(km.group(1)) if km else ""
        region, note = resolve_region(addr, country)
        address = addr
        if country == "United States":
            # The source drops leading zeros from north-eastern ZIPs.
            fixed = re.sub(r"(?<=, )(\d{4})(?=, United States$)", lambda m: m.group(1).zfill(5), address)
            if fixed != address:
                address = fixed
                note = (note + " " if note else "") + "Restored the leading zero the listing drops from the ZIP."
        email = ""
        em = re.search(r'href="mailto:([^"]+)"', block)
        if em:
            email = html.unescape(em.group(1)).strip()
        phone = ""
        pm = re.search(r'href="tel:([^"]+)"', block)
        if pm:
            phone = html.unescape(pm.group(1)).strip()
        rows.append({
            "country": country,
            "region": region,
            "name": name,
            "address": address,
            "source_address": addr,
            "website_as_listed": site,
            "email_as_listed": email,
            "phone_as_listed": phone,
            "type": kind or "Dealer",
            "address_note": note,
            "source_url": "https://www.maycocolors.com/distributors",
        })
    return dedupe(rows)


BASE = ["country", "region", "name", "address", "source_address",
        "website_as_listed", "email_as_listed", "phone_as_listed"]

if __name__ == "__main__":
    write("spectrum-us-ca-distributors.csv",
          BASE + ["fax_as_listed", "product_line", "type", "address_note", "source_url"],
          spectrum())
    if MISSING:
        print("404 (skipped):")
        for u in MISSING:
            print("  ", u)
    write("amaco-us-ca-distributors.csv", BASE + ["type", "address_note", "source_url"], amaco())
    write("mayco-us-ca-distributors-dealers.csv", BASE + ["type", "address_note", "source_url"], mayco())
