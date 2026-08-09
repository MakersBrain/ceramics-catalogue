# US/CA distributor sites: platform, scrapability, product relevance

Probed 2026-08-09 by `probe-us-ca.py` in this folder. Source: the three US/CA lists
here (Spectrum, Amaco, Mayco), every row with a website. **215 unique domains**,
none of which is in `catalogue-dump/sources.json` — the eighty sources we collect
today are European apart from a handful of brand shops, so nothing here is a
duplicate of existing coverage.

Per-domain detail is in `scrape-candidates-us-ca.csv`.

## Method

The same five read-only passes `scrape-candidates.md` describes for the EU list:
homepage fingerprint; robots/sitemap harvest; open-API test; structured-data test on
a product page; and a brand-token count from the shop's own API where one exists,
otherwise from sitemap URLs. One host at a time, one second between that host's
requests, eight hosts in parallel.

Two things the EU pass learned were folded into the prober rather than done by hand.
A full browser header set, not just a User-Agent — that alone recovered axner.com and
runyanpotterysupply.com. And a `curl_cffi` (curl-impersonate) retry on any 403 or 429,
which recovered twelve more including clay-king.com and claypeople.net. `curl_cffi` is
an optional import; without it the probe still runs and those sites read as blocked.

For every Shopify shop the first pass reported exactly 250 products, which is the
`products.json` page cap and not a size. Those were re-paginated to twenty pages;
real sizes came back 4-18x higher. At a 5000 cap only two shops are still floors —
texasart.com and deserres.ca, both general art retailers — and every shop worth
collecting has an exact count.

## Summary

| Tier | Meaning | Count |
|---|---|---|
| A | Open API, drop-in for an existing scraper | 44 |
| B | Structured data on product pages (json-ld / microdata) | 8 |
| C | Real shop, HTML-only or blocked — needs custom work | 109 |
| D | No shop found by the probe, or WAF-blocked — needs a manual look | 39 |
| X | Unreachable | 15 |

Tier A alone is **83,084 products** across 44 shops, 31 of them over a thousand.
Ten of the 44 are Canadian.

## The bigger ones

You said you would not want all of them. Ranked by what they would actually add,
which is catalogue size crossed with how much of it is the brands we track:

**Collected as of 2026-08-09.** Nine sources were added to `sources.json` and each
was verified against the live site with `catalogue-probe`:

| Source id | Products | Scraper | Verified |
|---|---|---|---|
| krueger-pottery | 4,455 | shopify | 394 records from 500 discovered |
| alabama-art-supply | 4,101 | shopify | collection allowlist, 15 of its 212 |
| sheffield-pottery | 3,807 | shopify | 280 records from 500 |
| sounding-stone | 3,721 | shopify | 357 records from 500 |
| seattle-pottery-supply | 3,430 | shopify | records with 100% price coverage |
| gwn-pottery | 3,327 | shopify | records with 100% price coverage |
| the-ceramic-shop | 18,869 URLs | **nitrosell** (new) | 20 of 25 pages |
| glaser-ceramics | 1,460 pages | pagecrawl | 116 of 150 pages |
| axner | 242 departments | **axner** (new) | 20 of 20 pages |

Brand depth on the six Shopify shops, counted over the whole catalogue rather than
a first page: Krueger mayco 1,509 / amaco 802; Sounding Stone mayco 1,474 /
spectrum 576 / amaco 560 / coyote 434; Alabama Art amaco 529 / mayco 482;
Seattle mayco 659 / amaco 384; GWN mayco 386 / coyote 237; Sheffield amaco 142.

Two scrapers were written because no existing one fit:

- **`nitrosell`** — The Ceramic Shop's `schema.org/Product` scope carries the name
  and nothing else; price, SKU and availability sit outside it, so `pagecrawl`'s
  microdata path built rows with no price and every one was dropped as invalid. The
  platform does publish the whole product as OpenGraph (`product:price:amount`,
  `og:upc`, `og:brand`, `og:availability`), which is machine-written and a better
  source than the body. Reusable for any NitroSell storefront.
- **`axner`** — no XML sitemap, no JSON-LD, no microdata. Discovery walks
  `/sitemap.aspx`, an HTML index of 242 departments, and their `?page=N` pagination.
  Product and category URLs are both hyphenated `.aspx` slugs and cannot be told
  apart by shape, so a product link is recognised by its listing tile class.

Glaser needed no new scraper but does need a careful pattern: its sitemap is 57,003
entries of which some 55,000 are images and `index.php` duplicates, leaving 1,460
real product pages, each carrying JSON-LD.

**Still worth doing, not yet collected:**

- **dickblick.com** — 20,256 product URLs, json-ld. Blick is also 54 of the Mayco rows.
  A general art retailer rather than a ceramics supplier, so its ceramics depth is
  shallower than its size suggests.
- **clay-king.com** — 9,701 products behind an open WooCommerce Store API, and the
  largest open catalogue in the whole set. An earlier draft of this file said it
  needed TLS impersonation; that was wrong and worth correcting. Only its *homepage*
  is behind the WAF — `/wp-json/wc/store/v1/products` answers a plain `httpx` client
  with 200. It is a one-line `sources.json` entry whenever you want it. The same is
  true of claypeople.net, whose homepage 403s and whose Store API does not.
- **tuckerspottery.com** (CA) — Lightspeed, 3,669 URLs, coyote 189. The biggest
  Canadian one not already collected.

## The four Cloudflare sites

The plan was to solve the challenge in a browser, keep the `cf_clearance` cookie and
carry on with a normal client. **That does not work, and the cookie is not what is
doing the work.** Tested end to end against nmclay.com with Camoufox solving the
challenge and the cookie handed straight to a normal client:

| client | with cf_clearance | without |
|---|---|---|
| `httpx` | **403** | 403 |
| `curl_cffi` (impersonating Chrome) | 200 | **200** |

The cookie changes nothing in either direction. What decides the outcome is the TLS
fingerprint: Cloudflare refuses the Python client's handshake and accepts an
impersonated one, cookie or no cookie. So the mechanism to build is a
TLS-impersonating transport, not a cookie broker.

**That transport now exists** as the third rung of `Fetcher.response`: declared
research agent, then a browser User-Agent, then a browser TLS handshake via
`curl_cffi`. It is an optional dependency (`--extra impersonate`) and an
`--impersonate never|auto` switch, and a host that refuses the handshake too still
surfaces its own 403 rather than an error about our tooling. Measured on four hosts
that are a hard 403 without it — claypeople.net, stoneleafpottery.com,
thetiltedkiln.com, ceramicarts.com — all four return 200 with it.

That is not enough to make nmclay.com a source, though. Repeated over six spaced
requests it answered 200 twice and 403 four times, so impersonation alone is too
flaky to schedule. The honest path there is `render: true` and the browser worker,
which the codebase already routes for (§5.5) — Camoufox loads the site cleanly.

**The other three are not bot challenges at all, and I have not tried to get past
them.** baileypottery.com and kurtzbros.com return Cloudflare's "Attention Required"
page, which is a firewall rule the site owner wrote. columbusclay.com passes the
Cloudflare layer and then returns "Your access to this site has been limited by the
site owner" — that is Wordfence, again the owner's own rule. dogwoodceramics.com sits
on a managed challenge that headless Camoufox does not clear after thirty seconds.
A generic bot check is one thing; an explicit block by the operator is a decision
about us specifically, and working around it is a different act. If any of the three
matter, the route is to ask them.

## Out of scope

A large part of tier C is not a ceramics-materials retailer. They are in the lists
because they stock one brand's craft line, and they should not become sources:

General industrial and school supply — mcmaster.com (273k products, matched only on
the word "standard"), schoolspecialty.com, unitednow.com, bramespecialty.com,
compositesone.com, notionsmarketing.com, pyramidspcatalog.com, virco.com, wbmason.com,
nu-idea.com. Big-box craft — michaels.com, joann.com, hobbylobby.com. Picture framing,
which is a whole cluster of the Amaco list — larsonjuhl.com, decormoulding.com,
internationalmoulding.com, picturedepot.ca, deltapictureframe.com, macphersonart.com.
And walthers.com is model railway, bigyflyco.com is fly fishing, jewelrysupply.com and
sculpt.com are their respective trades.

Two artefacts in the source data rather than sites: one Mayco row lists a `tel:` link
where its website should be, which is why `tel` appears as a domain, and one lists a
Facebook page.

## Caveats

- **Brand hits are keyword counts and have false positives.** "standard" is an ordinary
  English word, which is the whole of mcmaster.com's 2,411 and walthers.com's 14,227.
  "spectrum" is the retailer's own name at spectrum-nasco.ca and spectrumed.ca, giving
  them a meaningless 78,713. Read the counts only where the token is unambiguous.
- Counts for the WooCommerce shops come from a 100-item first page, so clay-king.com's
  brand depth is understated by roughly a hundredfold. Its size, 9,701, is exact.
- Two Shopify sizes are still floors, capped at 5,000: texasart.com and deserres.ca.
- Where `size_source` is `sitemap urls`, the number counts URLs of every kind, not
  products. Only `sitemap product urls` and the `api` sources are product counts.
- Platform labels are fingerprints. `unknown` (106 domains) means no distinctive
  marker, not no platform.
- carolinaclay.com is Shopify but answers `products.json` with 401 — the endpoint can
  be switched off, and here it is.
- **hyatts.com's robots.txt allows Googlebot, Bingbot and DuckDuckBot and denies every
  other named crawler.** Nothing in this probe checks terms of service or robots policy
  for any site, and this one is a reminder that the answer is not always yes. That is a
  separate decision per source before anything is scheduled.
