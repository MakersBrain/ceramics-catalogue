#!/usr/bin/env python3
"""Smoke-test every configured source and report what each one actually yields.

Development aid, like probe.py, but it walks many sources and writes a machine
readable summary instead of a human report. It never writes a dump.

    ./.venv/bin/python smoke_sources.py --only-new --limit 40 --out smoke.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx

from ateliera_catalogue import scrapers
from ateliera_catalogue.scrapers.base import USER_AGENT, BrowserRenderer, Fetcher, HostLimiter

HERE = Path(__file__).resolve().parent

#: the 20 sources that predate the distributor sweep
BASELINE = {
    "les-cousins", "mayco", "spectrum", "keramiekenglazuur", "menomuza",
    "solutions-ceramiques", "ceradel", "penguin-pottery", "ceramique-peinture",
    "amaco", "speedball", "1240-design", "ceram-decor", "colpaert-online",
    "sio-2", "e-cibas", "keramikbedarf-online", "art4fun", "ceramicolours",
    "keramik-kraft",
}

VAT_HINTS = (
    ("inclusive", ("inc vat", "incl. vat", "inkl. mwst", "incl. btw", "iva incl",
                   "ttc", "inclusief btw", "med moms", "incl moms", "s dph", "brutto")),
    ("exclusive", ("ex vat", "excl. vat", "zzgl. mwst", "excl. btw", "sin iva",
                   "netto", "bez dph", "ht ", "plus vat")),
)


async def run_one(name: str, config: dict[str, Any], limit: int, delay: float,
                  browser_mode: str) -> dict[str, Any]:
    started = time.monotonic()
    out: dict[str, Any] = {"source": name, "scraper": config.get("scraper"),
                           "url": config.get("url"), "country": config.get("country")}
    browser = BrowserRenderer(browser_mode != "never")
    try:
        async with httpx.AsyncClient(
            headers={"user-agent": USER_AGENT}, timeout=30, follow_redirects=True,
        ) as client:
            limiter = HostLimiter(delay, 4)
            fetcher = Fetcher(client, limiter, browser, browser_mode)
            scraper = scrapers.build(config["scraper"], name, config, fetcher)
            result = await scraper.run(limit)
    except Exception as exc:  # a source that explodes must not stop the sweep
        out.update(status="crash", error=f"{type(exc).__name__}: {exc}"[:300])
        return out
    finally:
        await browser.close()

    records = result.records
    out.update(
        status="ok" if records else "no-records",
        records=len(records),
        requests=result.requests,
        discovered=result.discovered,
        truncated=result.truncated,
        rendered=result.rendered_pages,
        notes=result.notes[:4],
        errors=[f"{e['url']} -> {e['error'][:120]}" for e in result.errors[:3]],
        elapsed=round(time.monotonic() - started, 1),
    )
    if records:
        filled = {k: sum(1 for r in records if r.get(k) not in (None, [], {}))
                  for k in records[0]}
        key_fields = ("name", "price", "currency", "package_size", "family",
                      "firing", "brand", "manufacturer_sku", "gtin", "availability",
                      "unit_price", "image_url")
        out["coverage"] = {k: filled.get(k, 0) for k in key_fields}
        sample = records[0]
        out["sample"] = {k: sample.get(k) for k in ("name", "price", "currency",
                                                    "price_text", "brand", "family")}
        # VAT evidence straight from the observed price text
        texts = " ".join(str(r.get("price_text") or "") for r in records[:40]).lower()
        out["price_text_sample"] = next(
            (str(r.get("price_text")) for r in records if r.get("price_text")), None)
        out["vat_evidence"] = next(
            (label for label, needles in VAT_HINTS if any(n in texts for n in needles)), None)
        out["configured_vat"] = config.get("vat_status")
    return out


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--delay", type=float, default=1.0)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--browser", choices=("never", "auto", "always"), default="never")
    ap.add_argument("--only-new", action="store_true", help="skip the 20 pre-existing sources")
    ap.add_argument("--sources", default="", help="comma-separated subset")
    ap.add_argument("--timeout", type=float, default=300, help="per-source seconds")
    ap.add_argument("--out", default="smoke.json")
    a = ap.parse_args()
    logging.basicConfig(level=logging.ERROR, format="%(levelname)s %(message)s")

    config = json.loads((HERE / "sources.json").read_text())
    names = [n for n in config
             if (not a.only_new or n not in BASELINE)
             and (not a.sources or n in a.sources.split(","))]
    print(f"smoke-testing {len(names)} sources (limit={a.limit})", flush=True)

    sem = asyncio.Semaphore(a.concurrency)
    results: list[dict[str, Any]] = []

    async def guarded(name: str) -> None:
        async with sem:
            try:
                res = await asyncio.wait_for(
                    run_one(name, config[name], a.limit, a.delay, a.browser), a.timeout)
            except TimeoutError:
                res = {"source": name, "scraper": config[name].get("scraper"),
                       "status": "timeout", "elapsed": a.timeout}
            except Exception as exc:
                res = {"source": name, "status": "crash",
                       "error": f"{type(exc).__name__}: {exc}"[:300]}
            results.append(res)
            print(f"  {res['status']:10} {name:26} "
                  f"records={res.get('records', 0):4} "
                  f"disc={res.get('discovered', 0):5} {res.get('error', '')[:60]}", flush=True)
            Path(a.out).write_text(json.dumps(results, indent=1, ensure_ascii=False))

    await asyncio.gather(*(guarded(n) for n in names))
    Path(a.out).write_text(json.dumps(results, indent=1, ensure_ascii=False))
    ok = sum(1 for r in results if r["status"] == "ok")
    print(f"\n{ok}/{len(results)} produced records -> {a.out}")


if __name__ == "__main__":
    asyncio.run(main())
