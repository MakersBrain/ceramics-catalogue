<script lang="ts">
	import { onMount } from 'svelte';
	import { page } from '$app/state';
	import { invalidateAll } from '$app/navigation';
	import { enhance } from '$app/forms';
	import { OpsStream } from '$lib/ops/stream.svelte';
	import Unavailable from '$lib/ops/Unavailable.svelte';
	import { relative, duration, count, stateTone } from '$lib/ops/format';

	let { data, form } = $props();

	// The second, narrower subscription, closed on navigate. Progress is asked
	// for explicitly because it is the expensive topic, and only this page shows
	// it.
	let stream = $state<OpsStream | null>(null);

	onMount(() => {
		const live = new OpsStream('jobs,progress,runs', page.params.id);
		live.connect(() => invalidateAll());
		stream = live;
		return () => live.disconnect();
	});

	const jobs = $derived(data.jobs ?? []);
	const active = $derived(data.run && ['queued', 'running'].includes(data.run.status));

	/** Live counters where the stream has them, the load's snapshot otherwise. */
	function live(job: any) {
		const pushed = stream?.progress[job.id];
		return {
			records: pushed?.records ?? job.records ?? 0,
			requests: pushed?.requests ?? job.requests ?? 0,
			errors: pushed?.errors ?? job.error_count ?? 0,
			phase: pushed?.phase ?? job.phase,
			inFlight: pushed?.in_flight ?? job.in_flight ?? []
		};
	}

	/**
	 * A bar against the previous run's record count, which is the only figure
	 * that makes "812 records" mean anything while a source is still going.
	 */
	function share(job: any, records: number): number | null {
		if (!job.previous_records) return null;
		return Math.min(100, (100 * records) / job.previous_records);
	}
</script>

<svelte:head><title>Run · operations</title></svelte:head>

{#if data.unavailable}
	<Unavailable reason={data.unavailable} />
{:else if data.run}
	<div class="mb-4 flex flex-wrap items-center gap-3">
		<a class="link text-sm" href="/ops/runs">← runs</a>
		<h1 class="text-lg font-semibold">
			{data.run.kind} run
			<span class="badge {stateTone(data.run.status)} ml-2">{data.run.status}</span>
		</h1>
		<span class="text-muted-foreground text-sm">
			{relative(data.run.created_at)} · {duration(data.run.started_at, data.run.finished_at)}
			{#if data.run.requested_by}· {data.run.requested_by}{/if}
		</span>

		{#if active}
			<form method="POST" action="?/cancel" use:enhance class="ml-auto">
				<button class="btn btn-error btn-outline btn-sm" type="submit">Cancel run</button>
			</form>
		{/if}
	</div>

	{#if form?.error}
		<div class="alert alert-error mb-4 text-sm">{form.error}</div>
	{/if}

	<div class="overflow-x-auto">
		<table class="table table-sm bg-card rounded shadow-sm">
			<thead>
				<tr>
					<th>Source</th>
					<th>State</th>
					<th>Phase</th>
					<th class="w-48">Records</th>
					<th class="text-right">Requests</th>
					<th class="text-right">Errors</th>
					<th>Attempt</th>
					<th></th>
				</tr>
			</thead>
			<tbody>
				{#each jobs as job (job.id)}
					{@const now = live(job)}
					{@const bar = share(job, now.records)}
					<tr class="hover">
						<td>
							<a class="link font-medium" href="/ops/runs/{data.run.id}/jobs/{job.id}">
								{job.source_id}
							</a>
							<div class="text-muted-foreground/70 text-xs">{job.host}</div>
						</td>
						<td><span class="badge badge-sm {stateTone(job.state)}">{job.state}</span></td>
						<td class="text-muted-foreground text-xs">{now.phase ?? '—'}</td>
						<td>
							<div class="flex items-center gap-2">
								<span class="tabular-nums">{count(now.records)}</span>
								{#if bar !== null}
									<progress class="progress progress-primary h-1.5 w-20" value={bar} max="100"
									></progress>
									<span class="text-muted-foreground/70 text-xs" title="previous run">
										/{count(job.previous_records)}
									</span>
								{/if}
							</div>
							{#if now.inFlight.length}
								<div class="text-muted-foreground/70 truncate text-xs" title={now.inFlight[0].url}>
									{now.inFlight[0].seconds}s · {now.inFlight[0].url}
								</div>
							{/if}
						</td>
						<td class="text-right tabular-nums">{count(now.requests)}</td>
						<td class="text-right tabular-nums {now.errors ? 'text-destructive' : ''}">
							{count(now.errors)}
						</td>
						<td class="text-xs">{job.attempt}/{job.max_attempts}</td>
						<td>
							<div class="flex gap-1">
								{#each ['pause', 'resume', 'cancel', 'retry'] as action (action)}
									<form method="POST" action="?/job" use:enhance>
										<input type="hidden" name="id" value={job.id} />
										<input type="hidden" name="action" value={action} />
										<button class="btn btn-ghost btn-xs" type="submit">{action}</button>
									</form>
								{/each}
							</div>
						</td>
					</tr>
				{/each}
			</tbody>
		</table>
	</div>

	{#if data.run.summary}
		<p class="text-muted-foreground mt-4 text-sm">
			{count(data.run.summary.records)} records ·
			{data.run.summary.succeeded} succeeded ·
			{data.run.summary.failed} failed ·
			{data.run.summary.cancelled} cancelled
		</p>
	{/if}
{/if}
