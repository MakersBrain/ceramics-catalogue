#!/usr/bin/env python3
"""Search a ceramics catalogue dump and compare likely-similar products."""

from __future__ import annotations

import argparse
import html
import json
import math
import re
import unicodedata
import webbrowser
import urllib.request
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from difflib import SequenceMatcher
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

STOP = {"amaco", "mayco", "spectrum", "ceramic", "ceramics", "glaze", "stoneware",
        "email", "liquid", "emil", "the", "and", "les", "cousins"}
MODEL = re.compile(r"\b[A-Z]{1,5}[ -]?\d{1,4}(?:[.-][A-Z0-9]+)*\b", re.I)
# A leading hyphen usually means a model number (SW-422), not 422 litres.
AMOUNT = re.compile(r"(?<![-\w])(\d+(?:[.,]\d+)?)\s*(kg|g|ml|cl|l)\b", re.I)
ECB_DAILY = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"


def plain(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def reference(item: dict) -> str | None:
    """Read a product code from either dump format.

    v2 splits the old flat `sku` into a manufacturer code and the retailer's own
    reference; v1 dumps still carry `sku`.
    """
    return item.get("manufacturer_sku") or item.get("supplier_reference") or item.get("sku")


def models(item: dict) -> set[str]:
    values = [reference(item), item.get("name")]
    return {re.sub(r"[^A-Z0-9]", "", x.upper()) for v in values for x in MODEL.findall(str(v or ""))}


def quantity(item: dict) -> tuple[float, str] | None:
    # v2 carries an already-normalised package object; v1 used flat fields.
    package = item.get("package_size")
    if isinstance(package, dict):
        if package.get("grams"):
            return float(package["grams"]), "g"
        if package.get("millilitres"):
            return float(package["millilitres"]), "ml"
        if package.get("value") is not None and package.get("unit"):
            return float(package["value"]), str(package["unit"]).lower()
    if item.get("quantity") is not None and item.get("unit"):
        return float(item["quantity"]), str(item["unit"]).lower()
    raw = item.get("raw") or {}
    candidates = [item.get("name"), item.get("description")]
    for prop in raw.get("additionalProperty", []) if isinstance(raw, dict) else []:
        if isinstance(prop, dict):
            candidates.append(prop.get("value"))
    for value in candidates:
        match = AMOUNT.search(str(value or ""))
        if match:
            amount, unit = float(match.group(1).replace(",", ".")), match.group(2).lower()
            if unit == "kg": return amount * 1000, "g"
            if unit == "l": return amount * 1000, "ml"
            if unit == "cl": return amount * 10, "ml"
            return amount, unit
    return None


def currency(item: dict) -> str:
    """Prefer the source's raw structured-data currency (some normalized rows are wrong)."""
    raw = item.get("raw") or {}
    offers = raw.get("offers", {}) if isinstance(raw, dict) else {}
    if isinstance(offers, list): offers = offers[0] if offers else {}
    return str(offers.get("priceCurrency") or item.get("currency") or "?").upper()


def load(directory: Path) -> list[dict]:
    result: dict[tuple, dict] = {}
    for path in sorted(directory.glob("*.ndjson")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"{path}:{number}: {exc}") from exc
            if item.get("name") and isinstance(item.get("price"), (int, float)):
                item["_currency"] = currency(item)
                item["_quantity"] = quantity(item)
                key = (item.get("source"), plain(reference(item)) or plain(item.get("name")), item.get("price"), item["_currency"])
                previous = result.get(key)
                # Prefer a canonical-looking product URL over a category observation.
                if previous is None or len(str(item.get("product_url"))) > len(str(previous.get("product_url"))):
                    result[key] = item
    return list(result.values())


def load_fx(cache: Path) -> tuple[dict[str, float], str, bool]:
    """Download ECB reference rates, falling back to the last cached response."""
    stale = False
    try:
        request = urllib.request.Request(ECB_DAILY, headers={"User-Agent": "CeramicsPriceExplorer/1.0"})
        with urllib.request.urlopen(request, timeout=10) as response:
            root = ET.fromstring(response.read())
        dated = next(node for node in root.iter() if node.attrib.get("time"))
        rates = {node.attrib["currency"]: float(node.attrib["rate"]) for node in dated if "currency" in node.attrib}
        rates["EUR"] = 1.0
        date = dated.attrib["time"]
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"date": date, "rates": rates}, indent=2) + "\n")
    except (OSError, ValueError, ET.ParseError, StopIteration, KeyError):
        stale = True
        try:
            saved = json.loads(cache.read_text())
            date, rates = saved["date"], {key: float(value) for key, value in saved["rates"].items()}
        except (OSError, ValueError, KeyError):
            date, rates = "unavailable", {"EUR": 1.0}
    return rates, date, stale


def score(query: str, item: dict) -> float:
    q, name = plain(query), plain(item.get("name"))
    qtokens, ntokens = set(q.split()), set(name.split())
    overlap = len(qtokens & ntokens) / max(1, len(qtokens))
    substring = 1.0 if q and q in name else 0.0
    code = 1.0 if re.sub(r"[^a-z0-9]", "", q) in {x.lower() for x in models(item)} else 0.0
    return max(code, substring * .9, overlap * .8, SequenceMatcher(None, q, name).ratio() * .55)


def similarity(left: dict, right: dict) -> float:
    lm, rm = models(left), models(right)
    if lm & rm: return 1.0
    lt = set(plain(left.get("name")).split()) - STOP
    rt = set(plain(right.get("name")).split()) - STOP
    jaccard = len(lt & rt) / max(1, len(lt | rt))
    sequence = SequenceMatcher(None, " ".join(sorted(lt)), " ".join(sorted(rt))).ratio()
    return .7 * jaccard + .3 * sequence


def search(items: list[dict], query: str, limit: int = 20) -> list[dict]:
    ranked = [(score(query, item), item) for item in items]
    return [item for value, item in sorted(ranked, key=lambda x: x[0], reverse=True)[:limit] if value >= .25]


def similar(items: list[dict], chosen: dict, limit: int = 12) -> list[tuple[float, dict]]:
    ranked = [(similarity(chosen, item), item) for item in items if item is not chosen and item.get("source") != chosen.get("source")]
    return [(value, item) for value, item in sorted(ranked, key=lambda x: x[0], reverse=True)[:limit] if value >= .28]


def money(item: dict, rates: dict[str, float] | None = None) -> str:
    original = f"{item['price']:.2f} {item['_currency']}"
    if not rates or item["_currency"] == "EUR": return original
    rate = rates.get(item["_currency"])
    return original if not rate else f"{original}<small>≈ {item['price'] / rate:.2f} EUR</small>"


def money_text(item: dict, rates: dict[str, float] | None = None) -> str:
    original = f"{item['price']:.2f} {item['_currency']}"
    rate = rates.get(item["_currency"]) if rates else None
    return original if not rate or item["_currency"] == "EUR" else f"{original} (≈ {item['price'] / rate:.2f} EUR)"


def original_money(item: dict) -> str:
    return f"{item['price']:.2f} {item['_currency']}"


def qty(item: dict) -> str:
    q = item["_quantity"]
    return "unknown pack size" if not q else f"{q[0]:g} {q[1]}"




# ---------------------------------------------------------------- comparison

def to_eur(amount: float | None, code: str | None, rates: dict[str, float]) -> float | None:
    """Convert an observed amount to indicative EUR using the ECB reference rate."""
    if amount is None or not code:
        return None
    if code == "EUR":
        return float(amount)
    rate = rates.get(code)
    return float(amount) / rate if rate else None


def unit_price(item: dict, rates: dict[str, float]) -> tuple[float, str] | None:
    """Price per litre or per kilogram, in EUR, however the dump expressed it."""
    published = item.get("unit_price")
    if isinstance(published, dict) and published.get("value") is not None:
        value = to_eur(published["value"], published.get("currency") or item.get("currency"), rates)
        if value is not None:
            return value, str(published.get("per") or "unit")
    # v1 dumps and rows whose scraper did not compute one.
    price = to_eur(item.get("price"), item["_currency"], rates)
    measured = item.get("_quantity")
    if price is None or not measured:
        return None
    amount, unit = measured
    if unit == "g" and amount:
        return price / (amount / 1000.0), "kg"
    if unit == "ml" and amount:
        return price / (amount / 1000.0), "l"
    return None


def product_key(item: dict) -> tuple[str, str]:
    """Group offers that are the same thing.

    A manufacturer code is the only honest cross-supplier key, so it wins where
    a scraper found one. Everything else groups per supplier product, which at
    least collapses that product's own pack sizes together.
    """
    code = str(item.get("manufacturer_sku") or "").strip().upper()
    if code:
        return ("ref", code)
    return ("product", str(item.get("parent_external_id") or item.get("product_url") or item.get("name")))


def offer_view(item: dict, rates: dict[str, float]) -> dict:
    normalised = unit_price(item, rates)
    package = item.get("package_size") if isinstance(item.get("package_size"), dict) else None
    measured = item.get("_quantity")
    size = None
    if package:
        size = package.get("millilitres") or package.get("grams")
    elif measured and measured[1] in {"ml", "g"}:
        size = measured[0]
    return {
        # Pack size in ml or g, used only to compare like with like.
        "_ml_or_g": size,
        "source": item.get("source"),
        "name": item.get("name"),
        "variant": item.get("variant_title"),
        "url": item.get("product_url"),
        "price": item.get("price"),
        "currency": item["_currency"],
        "price_eur": to_eur(item.get("price"), item["_currency"], rates),
        "unit_price": normalised[0] if normalised else None,
        "unit_per": normalised[1] if normalised else None,
        "pack": qty(item) if item.get("_quantity") else (package or {}).get("evidence"),
        "vat": item.get("vat_status"),
        "stock": item.get("availability", "").rsplit("/", 1)[-1] if item.get("availability") else None,
        "reference": item.get("supplier_reference"),
    }


def specification(items: list[dict]) -> dict:
    """Merge the technical facts the suppliers published for one product."""
    firing = next((x["firing"] for x in items if isinstance(x.get("firing"), dict)), None)
    claims: dict[str, dict] = {}
    for item in items:
        for claim in item.get("claims") or []:
            if isinstance(claim, dict) and claim.get("type"):
                claims.setdefault(claim["type"], claim)
    return {
        "family": next((x.get("family") for x in items if x.get("family")), None),
        "form": next((x.get("form") for x in items if x.get("form")), None),
        "surface": next((x.get("surface") for x in items if x.get("surface")), None),
        "colour": next((x["colour"].get("name") for x in items
                        if isinstance(x.get("colour"), dict) and x["colour"].get("name")), None),
        "brand": next((x.get("brand") for x in items if x.get("brand")), None),
        "firing": firing,
        "claims": list(claims.values()),
        "image": next((x.get("image_url") for x in items if x.get("image_url")), None),
    }


def spread(offers: list[dict]) -> dict | None:
    """How much dearer one supplier is than another for a comparable pack.

    Only offers of a similar pack size are compared. A small pot always costs
    more per litre than a large tub, so ranking a supplier's 59 ml jar against
    another's 472 ml pot would misrepresent both.
    """
    sized = [o for o in offers if o.get("unit_price") is not None and o.get("_ml_or_g") and o.get("source")]
    if not sized:
        return None
    # Bucket by pack size on a log scale, so 470 ml and 473 ml compare but
    # 59 ml and 472 ml do not.
    buckets: dict[int, dict[str, dict]] = {}
    for offer in sized:
        bucket = round(math.log10(offer["_ml_or_g"]) * 3)
        cheapest = buckets.setdefault(bucket, {})
        current = cheapest.get(offer["source"])
        if current is None or offer["unit_price"] < current["unit_price"]:
            cheapest[offer["source"]] = offer
    best = None
    for bucket, by_source in buckets.items():
        if len(by_source) < 2:
            continue
        low = min(by_source.values(), key=lambda o: o["unit_price"])
        high = max(by_source.values(), key=lambda o: o["unit_price"])
        if low["unit_price"] <= 0:
            continue
        percent = (high["unit_price"] / low["unit_price"] - 1) * 100
        if best is None or percent > best["percent"]:
            best = {
                "percent": round(percent, 1),
                "cheapest": low["source"], "cheapest_unit": round(low["unit_price"], 2),
                "dearest": high["source"], "dearest_unit": round(high["unit_price"], 2),
                "per": low.get("unit_per") or "unit",
                "pack": low.get("pack"),
                "compared_packs": sorted({o["pack"] for o in by_source.values() if o.get("pack")}),
            }
    return best if best and best["percent"] >= 1 else None


def build_groups(items: list[dict], rates: dict[str, float]) -> list[dict]:
    """Collapse every observation into comparable product groups, once."""
    buckets: dict[tuple[str, str], list[dict]] = {}
    for item in items:
        buckets.setdefault(product_key(item), []).append(item)

    groups = []
    for (kind, key), members in buckets.items():
        offers = sorted(
            (offer_view(item, rates) for item in members),
            key=lambda offer: (offer["unit_price"] is None, offer["unit_price"] or offer["price_eur"] or 0),
        )
        sources = sorted({offer["source"] for offer in offers if offer["source"]})
        # product_name is the identity without the pack suffix; fall back to the
        # shortest observed name, which is the least variant-specific one.
        titles = [m.get("product_name") for m in members if m.get("product_name")]
        titles = titles or [m.get("name") for m in members if m.get("name")]
        title = min(titles, key=len) if titles else key
        groups.append({
            "key": f"{kind}:{key}",
            "reference": key if kind == "ref" else None,
            "title": title,
            "sources": sources,
            "supplier_count": len(sources),
            "offers": offers,
            "spread": spread(offers),
            "spec": specification(members),
            "search_text": plain(" ".join(filter(None, [
                key if kind == "ref" else "",
                title,
                " ".join(str(m.get("supplier_reference") or "") for m in members),
            ]))),
        })
    return groups


def rank(groups: list[dict], query: str, limit: int = 40) -> list[dict]:
    """Rank groups for a reference or name query."""
    needle = plain(query)
    compact = re.sub(r"[^a-z0-9]", "", needle)
    if not needle:
        # No query: lead with the products whose price differs most between
        # suppliers, which is where a purchasing decision actually matters.
        comparable = [group for group in groups if group.get("spread")]
        comparable.sort(key=lambda group: -group["spread"]["percent"])
        return comparable[:limit]
    scored = []
    for group in groups:
        reference_code = re.sub(r"[^a-z0-9]", "", (group["reference"] or "").lower())
        if compact and reference_code and compact == reference_code:
            value = 1.0
        elif compact and reference_code and compact in reference_code:
            value = 0.9
        elif needle in group["search_text"]:
            value = 0.75
        else:
            tokens = set(needle.split())
            overlap = len(tokens & set(group["search_text"].split())) / max(1, len(tokens))
            value = overlap * 0.6
        if value >= 0.3:
            scored.append((value, group))
    scored.sort(key=lambda pair: (-pair[0], -pair[1]["supplier_count"]))
    return [group for _, group in scored[:limit]]


# ---------------------------------------------------------------------- web

PAGE = """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Ceramics price explorer</title><style>
:root{--bg:#f7f3ed;--card:#fff;--ink:#29231e;--muted:#6d6258;--line:#e2d9cd;--accent:#7a5c3e;--good:#1f6f43;--warn:#fff3cd}
@media(prefers-color-scheme:dark){:root{--bg:#1a1714;--card:#23201c;--ink:#ede7df;--muted:#a2968a;--line:#3a342d;--accent:#d0a878;--good:#6cc08b;--warn:#3a3320}}
*{box-sizing:border-box}body{font:16px/1.5 system-ui,sans-serif;margin:0;background:var(--bg);color:var(--ink)}
.wrap{max-width:1080px;margin:0 auto;padding:28px 18px 60px}
h1{margin:0 0 4px;font-size:1.5rem}.sub{color:var(--muted);margin:0 0 18px;font-size:.9rem}
form{display:flex;gap:8px;margin-bottom:10px}
input[type=search]{flex:1;padding:12px 14px;font-size:16px;border:1px solid var(--line);border-radius:9px;background:var(--card);color:var(--ink)}
button{padding:12px 18px;font-size:15px;border:0;border-radius:9px;background:var(--accent);color:#fff;cursor:pointer}
.filters{display:flex;gap:14px;flex-wrap:wrap;align-items:center;margin-bottom:18px;font-size:.87rem;color:var(--muted)}
select{padding:6px 8px;border:1px solid var(--line);border-radius:7px;background:var(--card);color:var(--ink)}
.note{background:var(--warn);padding:10px 12px;border-radius:8px;font-size:.85rem;margin-bottom:18px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:16px}
.head{display:flex;gap:12px;align-items:flex-start;flex-wrap:wrap}
.head img{width:56px;height:56px;object-fit:cover;border-radius:8px;background:var(--bg)}
.title{flex:1;min-width:200px}.title h2{margin:0;font-size:1.05rem}
.ref{display:inline-block;font:600 .78rem ui-monospace,monospace;background:var(--bg);border:1px solid var(--line);padding:1px 7px;border-radius:5px;margin-right:6px}
.tags{margin:6px 0 0;display:flex;gap:6px;flex-wrap:wrap}
.tag{font-size:.75rem;color:var(--muted);border:1px solid var(--line);padding:1px 7px;border-radius:20px}
.scroll{overflow-x:auto;margin-top:12px}
table{width:100%;border-collapse:collapse;font-size:.88rem;min-width:640px}
th{text-align:left;font-weight:600;color:var(--muted);font-size:.78rem;text-transform:uppercase;letter-spacing:.03em}
td,th{padding:8px 10px;border-top:1px solid var(--line);vertical-align:top}
tbody tr:first-child td{border-top:1px solid var(--line)}
.best td{background:color-mix(in srgb,var(--good) 9%,transparent)}
.best .unit{color:var(--good);font-weight:600}
.unit{white-space:nowrap}.num{white-space:nowrap;text-align:right}
a{color:var(--accent)}small{display:block;color:var(--muted);font-size:.78rem}
.empty{color:var(--muted);padding:30px 0;text-align:center}
.spread{margin:8px 0 0;font-size:.83rem;color:var(--muted)}.spread b{color:var(--accent)}
.count{color:var(--muted);font-size:.85rem;margin-bottom:12px}
</style></head><body><div class=wrap>
<h1>Ceramics price explorer</h1>
<p class=sub id=sub></p>
<form id=f><input type=search id=q placeholder="Product reference or name - PC-47, SW-229, EG140, Stroke &amp; Coat" autofocus></input><button>Search</button></form>
<div class=filters>
  <label><input type=checkbox id=multi> Only products sold by 2+ suppliers</label>
  <label><input type=checkbox id=stock> In stock only</label>
  <label>Family <select id=family><option value="">any</option></select></label>
</div>
<p class=note id=note></p>
<div id=out></div></div>
<script>
const $=id=>document.getElementById(id);
let meta={};
const money=(v,c)=>v==null?"-":v.toFixed(2)+" "+(c||"");
async function boot(){
  meta=await (await fetch('/api/meta')).json();
  $('sub').textContent=`${meta.offers} offers - ${meta.groups} products - ${meta.sources} suppliers`;
  $('note').textContent=`Prices are observations, not quotes. EUR conversions use ECB rates for ${meta.rate_date}${meta.stale?" (cached)":""} and are indicative. Compare pack size, VAT and shipping before buying.`;
  meta.families.forEach(f=>{const o=document.createElement('option');o.value=o.textContent=f;$('family').append(o)});
  const p=new URLSearchParams(location.search); if(p.get('q'))$('q').value=p.get('q');
  go();
}
async function go(){
  const q=$('q').value.trim();
  history.replaceState({},'',q?'?q='+encodeURIComponent(q):location.pathname);
  $('out').innerHTML='<p class=empty>Searching...</p>';
  const r=await (await fetch('/api/search?'+new URLSearchParams({
    q,multi:$('multi').checked?1:'',stock:$('stock').checked?1:'',family:$('family').value}))).json();
  render(r.groups,q);
}
function render(groups,q){
  if(!groups.length){$('out').innerHTML='<p class=empty>No matches. Try a manufacturer code such as PC-47.</p>';return}
  const heading=q?`${groups.length} matching product${groups.length>1?'s':''}`
                 :`Biggest price differences between suppliers - ${groups.length} products carrying the same manufacturer code`;
  $('out').innerHTML=`<p class=count>${heading}</p>`+groups.map(g=>{
    const s=g.spec, fire=s.firing?(s.firing.cone_min?`cone ${s.firing.cone_min}${s.firing.cone_max!==s.firing.cone_min?'-'+s.firing.cone_max:''}`:`${s.firing.min_celsius}-${s.firing.max_celsius}°C`):null;
    const tags=[s.family,s.form,s.surface,s.colour,fire,s.brand].filter(Boolean)
      .map(t=>`<span class=tag>${esc(t)}</span>`).join('');
    const safe=(s.claims||[]).filter(c=>c.type==='food_contact_suitability')
      .map(c=>`<span class=tag>${c.claim?'food-safe claimed':'not food-safe'}</span>`).join('');
    const rows=g.offers.map((o,i)=>`<tr class="${i===0&&o.unit_price!=null?'best':''}">
      <td>${esc(o.source)}<small>${esc(o.variant||o.name||'')}</small></td>
      <td>${esc(o.pack||'-')}</td>
      <td class=num>${money(o.price,o.currency)}${o.price_eur!=null&&o.currency!=='EUR'?`<small>≈ ${o.price_eur.toFixed(2)} EUR</small>`:''}</td>
      <td class="num unit">${o.unit_price!=null?o.unit_price.toFixed(2)+' EUR/'+o.unit_per:'-'}</td>
      <td>${esc(o.vat||'-')}<small>${esc(o.stock||'')}</small></td>
      <td><a href="${esc(o.url)}" target=_blank rel=noopener>open</a></td></tr>`).join('');
    const sp=g.spread?`<p class=spread><b>${g.spread.percent}% dearer</b> at ${esc(g.spread.dearest)}
      (${g.spread.dearest_unit} EUR/${esc(g.spread.per)}) than at ${esc(g.spread.cheapest)}
      (${g.spread.cheapest_unit} EUR/${esc(g.spread.per)})<br>compared on ${esc((g.spread.compared_packs||[]).join(' vs ')||g.spread.pack||'similar packs')}</p>`:'';
    return `<div class=card><div class=head>
      ${s.image?`<img src="${esc(s.image)}" alt="" loading=lazy onerror="this.remove()">`:''}
      <div class=title><h2>${g.reference?`<span class=ref>${esc(g.reference)}</span>`:''}${esc(g.title)}</h2>
      <div class=tags>${tags}${safe}</div>${sp}</div></div>
      <div class=scroll><table><thead><tr><th>Supplier</th><th>Pack</th><th>Price</th><th>Per unit</th><th>VAT / stock</th><th></th></tr></thead>
      <tbody>${rows}</tbody></table></div></div>`;
  }).join('');
}
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
$('f').addEventListener('submit',e=>{e.preventDefault();go()});
['multi','stock','family'].forEach(id=>$(id).addEventListener('change',go));
boot();
</script></body></html>"""


def strip_internal(group: dict) -> dict:
    """Drop the keys that exist only for ranking and pack matching."""
    clean = {k: v for k, v in group.items() if k != "search_text"}
    clean["offers"] = [{k: v for k, v in o.items() if not k.startswith("_")} for o in group["offers"]]
    return clean


def serve(items: list[dict], host: str, port: int, rates: dict[str, float], rate_date: str, stale: bool) -> None:
    groups = build_groups(items, rates)
    families = sorted({group["spec"]["family"] for group in groups if group["spec"]["family"]})
    meta = {
        "offers": len(items),
        "groups": len(groups),
        "sources": len({item.get("source") for item in items}),
        "families": families,
        "rate_date": rate_date,
        "stale": stale,
    }

    def filtered(query: str, params: dict[str, list[str]]) -> list[dict]:
        found = rank(groups, query)
        if params.get("multi", [""])[0]:
            found = [group for group in found if group["supplier_count"] > 1]
        if family := params.get("family", [""])[0]:
            found = [group for group in found if group["spec"]["family"] == family]
        if params.get("stock", [""])[0]:
            trimmed = []
            for group in found:
                offers = [offer for offer in group["offers"] if (offer["stock"] or "InStock") == "InStock"]
                if offers:
                    trimmed.append({**group, "offers": offers})
            found = trimmed
        return found

    class Handler(BaseHTTPRequestHandler):
        def reply(self, body: bytes, kind: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            route = urlparse(self.path)
            params = parse_qs(route.query)
            if route.path == "/api/meta":
                self.reply(json.dumps(meta).encode(), "application/json; charset=utf-8")
            elif route.path == "/api/search":
                found = filtered(params.get("q", [""])[0], params)
                payload = {"groups": [strip_internal(group) for group in found]}
                self.reply(json.dumps(payload).encode(), "application/json; charset=utf-8")
            else:
                self.reply(PAGE.encode(), "text/html; charset=utf-8")

        def log_message(self, fmt, *args):
            pass

    print(f"Price explorer: http://{host}:{port}   ({len(groups)} products, Ctrl-C to stop)")
    ThreadingHTTPServer((host, port), Handler).serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("query", nargs="?", help="product name/code; omit when using --serve")
    parser.add_argument("--data", type=Path, default=Path(__file__).with_name("catalogue-dumps"))
    parser.add_argument("--serve", action="store_true", help="run the browser-based explorer")
    parser.add_argument("--fx-cache", type=Path, default=Path(__file__).with_name("catalogue-dumps") / "fx-rates.json")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    items = load(args.data)
    rates, rate_date, stale = load_fx(args.fx_cache)
    if args.serve:
        serve(items, args.host, args.port, rates, rate_date, stale)
    elif not args.query:
        parser.error("provide a query or use --serve")
    else:
        for group in rank(build_groups(items, rates), args.query, limit=10):
            reference = f"[{group['reference']}] " if group["reference"] else ""
            print(f"\n{reference}{group['title']}  ({group['supplier_count']} supplier(s))")
            for offer in group["offers"]:
                unit = f"{offer['unit_price']:.2f} EUR/{offer['unit_per']}" if offer["unit_price"] is not None else "-"
                print(
                    f"  {offer['source']:<22} {offer['pack'] or '-':<16}"
                    f" {offer['price']:>9.2f} {offer['currency'] or '':<4} {unit:>16}   {offer['url']}"
                )


if __name__ == "__main__":
    main()
