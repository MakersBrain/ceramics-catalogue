# Spectrum, Amaco and Mayco distributors in the US and Canada

Pulled 2026-08-09 from each brand's own "where to buy" pages, by
`pull-us-ca.py` in this folder. One polite read-only pass, one second between
requests, no logins and no paid data. These are the US/CA companions to the EU
lists already here.

| File | Rows | US | CA |
|---|---|---|---|
| `spectrum-us-ca-distributors.csv` | 91 | 70 | 21 |
| `amaco-us-ca-distributors.csv` | 223 | 202 | 21 |
| `mayco-us-ca-distributors-dealers.csv` | 175 | 167 | 8 |

215 distinct domains across the three, and **none of them is in
`catalogue-dump/sources.json`** — the eighty sources we collect today are
European apart from a handful of brand shops. So this is a new region, not an
extension of the current crawl.

## Where each list comes from

**Spectrum** — `spectrumglazes.com/distributors/`, which fans out to one page
per state and province, for glazes and separately for stamps. 42 region pages.
The `product_line` column says which list a row came from; a company selling
both appears twice, once per line. `spectrum-stamp-distributors-in-canada/manitoba/`
is a dead link on their site and is the only region page that returned 404.

**Amaco** — `amaco.com/find-a-distributor?country=USA` and `?country=Canada`.
The page renders every card server-side; the USA page states "Showing 202
Stores" and the file has 202 US rows. Amaco gates the page behind a cookie its
own JavaScript sets (`fusion_auth_guard=success`); the puller sends it, which is
what the browser does on first load.

**Mayco** — `maycocolors.com/distributors`, a FacetWP archive whose unfiltered
HTML carries all 315 worldwide listings at once, so no pagination or map API is
involved. Filtered here to rows whose address ends United States or Canada.
Mayco is the only one of the three that distinguishes **Distributor** (156) from
**Dealer** (19); the `type` column carries its label verbatim.

## Columns

`country, region, name, address, source_address, website_as_listed,
email_as_listed, phone_as_listed, type, address_note, source_url` — the EU files'
shape plus `region` (state or province, which the EU lists did not need),
`source_url`, and for Spectrum `fax_as_listed` and `product_line`.

`source_address` is always what the site printed. `address` is the usable form.
`address_note` is non-empty only where the two differ or where the region was
not stated outright:

- Amaco: 4 regions read off a two-letter code, 1 past a misspelling
  ("Lousiana"), 1 not stated at all (a Toronto address that names no province).
- Mayco: 2 regions off a two-letter code, and 7 ZIPs whose leading zero the
  listing drops (Boston as `2215`) restored to five digits.
- Spectrum: none — the state or province is the page the listing sits on.

## Caveats

Contact detail is uneven by source, not by our parsing: Spectrum prints phone and
fax but never email, Mayco prints almost neither (2 phones, 1 email across 175
rows). Duplicate company names are mostly real branches — Blick Art Materials
alone is 54 Mayco rows. One exact duplicate row in Mayco's own output was
dropped.

None of this is joined to the catalogue yet. Turning it into crawl candidates
would mean the same probe pass `scrape-candidates.md` documents for the EU list.
