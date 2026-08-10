# Cloudflare hosting assessment

Assessment date: 2026-08-10

## Conclusion

The stack cannot be lifted and shifted to Cloudflare for only the $5/month
Workers Paid subscription. It can run mostly on Cloudflare after a serverless
redesign, with PostgreSQL initially remaining external.

The practical target is a hybrid architecture costing approximately $5–15 per
month for infrequent collection runs:

- Workers for the explorer, API gateway, control endpoints, and authentication.
- External PostgreSQL reached through Hyperdrive or direct TCP.
- R2 for response caches, dumps, and other durable artifacts.
- Queues, Workflows, and/or Durable Objects for orchestration.
- Cloudflare Containers started per collection job and stopped when it ends.
- Browser Run where ordinary Chrome automation works, with Camoufox Containers
  retained for protected shops.

## Component mapping

| Current component | Cloudflare target | Assessment |
| --- | --- | --- |
| Explorer/UI | Workers static assets and Worker SSR | Good fit |
| Read API and control service | Workers or lightweight Containers | Good after adaptation |
| PostgreSQL | External PostgreSQL initially; D1 after a rewrite | Major migration |
| Response cache and dumps | R2 | Good fit |
| Queue and scheduling | Queues plus Workflows/Durable Objects | Replaces the PostgreSQL queue |
| Plain collectors | On-demand Containers | Good fit |
| Camoufox collectors | On-demand Containers | Feasible, but costly if always running |
| Browser automation | Browser Run | Feasible, but not equivalent to Camoufox stealth |

## Important constraints

### Database

Container disks are ephemeral. PostgreSQL cannot safely use a Cloudflare
Container as its durable host. Persistent artifacts need R2 or Durable Object
storage.

D1 uses SQLite semantics rather than PostgreSQL. Moving to D1 requires porting
the schema, canonical-product promotion routines, concurrent job claiming,
leases, and event delivery. PostgreSQL schemas and dumps are not directly
compatible with D1.

Hyperdrive can connect Workers to an external PostgreSQL database and is
included with Workers Paid. It does not support PostgreSQL `LISTEN`/`NOTIFY`,
which the current live control stream uses. That stream would need direct
database connections or replacement with Durable Objects, Queues, or another
event mechanism.

### Python and browsers

Cloudflare Python Workers run on Pyodide. They do not provide normal native
Python process capabilities, functional threading/multiprocessing, or durable
local files. The existing CPython collectors and Camoufox therefore belong in
Containers rather than Python Workers.

Browser Run provides managed Chrome through Quick Actions or direct browser
sessions. It may not replace Camoufox for protected shops: Browser Run traffic
uses Cloudflare IP ranges and carries Cloudflare-identifying headers.

## Cost notes

Workers Paid has a minimum charge of $5/month. As of the assessment date it
includes:

- 10 million Worker requests/month.
- 30 million Worker CPU milliseconds/month.
- 25 GiB-hours of Container memory/month.
- 375 vCPU-minutes of Container CPU/month.
- 200 GB-hours of Container disk/month.
- 10 Browser Run browser-hours/month.
- 10 GB-month of R2 Standard storage through R2's free tier.

Additional Container memory costs approximately $0.009 per GiB-hour. Browser
Run costs $0.09 per browser-hour after the included ten hours.

At the time of measurement, the four local browser workers consumed:

| Worker | Memory |
| --- | ---: |
| browser-1 | 587.6 MiB |
| browser-2 | 563.9 MiB |
| browser-3 | 703.2 MiB |
| browser-4 | 1.156 GiB |

Cloudflare's predefined Container sizes jump from `basic` (1 GiB) to
`standard-1` (4 GiB). Four continuously running `standard-1` instances would
provision 16 GiB and cost roughly $105/month in memory alone, before disk and
active CPU. The economical model is therefore to create Containers on demand,
process a bounded source/job, persist results externally, and stop them.

These are estimates, not a billing guarantee. Recheck current pricing before
migration or deployment.

## Suggested migration sequence

1. Move caches and dump artifacts from shared volumes to R2.
2. Deploy the explorer's static assets and edge/authentication layer to Workers.
3. Keep PostgreSQL external and expose it through Hyperdrive where supported.
4. Replace PostgreSQL polling and live notifications with Queues and Durable
   Objects or Workflows.
5. Package plain and Camoufox collectors as request-controlled, on-demand
   Containers with explicit lifecycle and retry handling.
6. Test Browser Run source by source. Use it only where its detectable browser
   traffic is accepted.
7. Evaluate a D1 port separately after measuring database size, query patterns,
   write rates, and the cost of rewriting PostgreSQL-specific behavior.

## References

- [Workers pricing](https://developers.cloudflare.com/workers/platform/pricing/)
- [Container overview](https://developers.cloudflare.com/containers/)
- [Container limits and instance types](https://developers.cloudflare.com/containers/platform-details/limits/)
- [Container lifecycle and ephemeral disk](https://developers.cloudflare.com/containers/platform-details/architecture/)
- [D1 overview](https://developers.cloudflare.com/d1/)
- [D1 SQL compatibility](https://developers.cloudflare.com/d1/sql-api/sql-statements/)
- [D1 import limitations](https://developers.cloudflare.com/d1/best-practices/import-export-data/)
- [Hyperdrive database support and limitations](https://developers.cloudflare.com/hyperdrive/reference/supported-databases-and-features/)
- [Hyperdrive pricing](https://developers.cloudflare.com/hyperdrive/platform/pricing/)
- [Python Workers standard-library constraints](https://developers.cloudflare.com/workers/languages/python/stdlib/)
- [R2 pricing](https://developers.cloudflare.com/r2/pricing/)
- [Browser Run pricing](https://developers.cloudflare.com/browser-run/pricing/)
- [Browser Run limits](https://developers.cloudflare.com/browser-run/limits/)
- [Browser Run FAQ](https://developers.cloudflare.com/browser-run/faq/)
