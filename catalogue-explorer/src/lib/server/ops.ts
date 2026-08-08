import { sql } from './db';

/**
 * Server-side reporting queries for /ops/metrics.
 *
 * These read the database directly, the same way `explore.ts` does, rather than
 * going through catalogue-control. The control API is for *acting* on runs; a
 * chart of the last thirty days is a reporting query and putting it behind an
 * HTTP hop would only add a serialisation step.
 */

export interface RunPoint {
	day: string;
	runs: number;
	failed: number;
	records: number;
	median_seconds: number | null;
}

export async function runsOverTime(days = 30): Promise<RunPoint[]> {
	return sql<RunPoint[]>`
		select to_char(date_trunc('day', created_at), 'YYYY-MM-DD') as day,
		       count(*)::int                                        as runs,
		       count(*) filter (where status in ('failed', 'degraded'))::int as failed,
		       coalesce(sum((summary->>'records')::bigint), 0)::int as records,
		       percentile_cont(0.5) within group (
		         order by extract(epoch from (finished_at - started_at))
		       )                                                    as median_seconds
		  from catalogue.runs
		 where created_at > now() - make_interval(days => ${days})
		 group by 1
		 order by 1
	`;
}

export interface SourcePoint {
	source_id: string;
	day: string;
	records: number;
}

export async function recordsPerSource(days = 30, limit = 12): Promise<SourcePoint[]> {
	return sql<SourcePoint[]>`
		with ranked as (
		  select source_id, sum((summary->>'records')::bigint) as total
		    from catalogue.jobs
		   where state = 'succeeded' and finished_at > now() - make_interval(days => ${days})
		   group by source_id
		   order by total desc
		   limit ${limit}
		)
		select j.source_id,
		       to_char(date_trunc('day', j.finished_at), 'YYYY-MM-DD') as day,
		       max((j.summary->>'records')::int)                       as records
		  from catalogue.jobs j
		  join ranked r on r.source_id = j.source_id
		 where j.state = 'succeeded' and j.finished_at > now() - make_interval(days => ${days})
		 group by 1, 2
		 order by 1, 2
	`;
}

export interface HostErrorPoint {
	host: string;
	errors: number;
	jobs: number;
}

export async function errorRateByHost(days = 7): Promise<HostErrorPoint[]> {
	return sql<HostErrorPoint[]>`
		select host,
		       coalesce(sum((summary->>'error_count')::int), 0)::int as errors,
		       count(*)::int                                          as jobs
		  from catalogue.jobs
		 where finished_at > now() - make_interval(days => ${days})
		 group by host
		having coalesce(sum((summary->>'error_count')::int), 0) > 0
		 order by errors desc
		 limit 15
	`;
}

export interface StalenessBand {
	band: string;
	sources: number;
}

export async function stalenessBands(): Promise<StalenessBand[]> {
	return sql<StalenessBand[]>`
		with last_success as (
		  select source_id, max(finished_at) as at
		    from catalogue.jobs
		   where state = 'succeeded' and (summary->>'records')::int > 0
		   group by source_id
		)
		select case
		         when at > now() - interval '36 hours' then 'fresh'
		         when at > now() - interval '7 days'   then 'ageing'
		         else 'stale'
		       end            as band,
		       count(*)::int  as sources
		  from last_success
		 group by 1
	`;
}

export async function offersPerDay(days = 30): Promise<{ day: string; offers: number }[]> {
	return sql<{ day: string; offers: number }[]>`
		select to_char(date_trunc('day', observed_at), 'YYYY-MM-DD') as day,
		       count(*)::int                                         as offers
		  from catalogue.offer_observations
		 where observed_at > now() - make_interval(days => ${days})
		 group by 1
		 order by 1
	`;
}
