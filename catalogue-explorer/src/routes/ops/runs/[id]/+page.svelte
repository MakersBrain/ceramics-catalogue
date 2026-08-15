<script lang="ts">
	import { onMount } from 'svelte';
	import { page } from '$app/state';
	import { invalidateAll } from '$app/navigation';
	import { enhance } from '$app/forms';
	import { OpsStream } from '$lib/ops/stream.svelte';
	import Unavailable from '$lib/ops/Unavailable.svelte';
	import { relative, duration, count, stateTone } from '$lib/ops/format';
	import * as Table from '$lib/components/ui/table';
	import { StatusBadge } from '$lib/components/ui/status-badge';
	import { Button } from '$lib/components/ui/button';
	import { Progress } from '$lib/components/ui/progress';
	import { Notice } from '$lib/components/ui/notice';

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
		<a
			class="text-accent-foreground text-sm underline-offset-4 hover:underline"
			href="/ops/runs">← runs</a
		>
		<h1 class="text-lg font-semibold">
			{data.run.kind} run
			<StatusBadge tone={stateTone(data.run.status)} class="ml-2">{data.run.status}</StatusBadge>
		</h1>
		<span class="text-muted-foreground text-sm">
			{relative(data.run.created_at)} · {duration(data.run.started_at, data.run.finished_at)}
			{#if data.run.requested_by}· {data.run.requested_by}{/if}
		</span>

		{#if active}
			<form method="POST" action="?/cancel" use:enhance class="ml-auto">
				<Button variant="destructive" size="sm" type="submit">Cancel run</Button>
			</form>
		{/if}
	</div>

	{#if form?.error}
		<Notice kind="error" class="mb-4">{form.error}</Notice>
	{/if}

	<div class="bg-card overflow-hidden rounded-lg border">
		<Table.Root>
			<Table.Header>
				<Table.Row>
					<Table.Head>Source</Table.Head>
					<Table.Head>State</Table.Head>
					<Table.Head>Phase</Table.Head>
					<Table.Head class="w-48">Records</Table.Head>
					<Table.Head class="text-right">Requests</Table.Head>
					<Table.Head class="text-right">Errors</Table.Head>
					<Table.Head>Attempt</Table.Head>
					<Table.Head><span class="sr-only">Job controls</span></Table.Head>
				</Table.Row>
			</Table.Header>
			<Table.Body>
				{#each jobs as job (job.id)}
					{@const now = live(job)}
					{@const bar = share(job, now.records)}
					<Table.Row>
						<Table.Cell>
							<a
								class="text-accent-foreground font-medium underline-offset-4 hover:underline"
								href="/ops/runs/{data.run.id}/jobs/{job.id}"
							>
								{job.source_id}
							</a>
							<div class="text-muted-foreground/70 text-xs">{job.host}</div>
						</Table.Cell>
						<Table.Cell><StatusBadge tone={stateTone(job.state)}>{job.state}</StatusBadge></Table.Cell>
						<Table.Cell class="text-muted-foreground text-xs">{now.phase ?? '—'}</Table.Cell>
						<Table.Cell>
							<div class="flex items-center gap-2">
								<span class="tabular-nums">{count(now.records)}</span>
								{#if bar !== null}
									<Progress
										value={bar}
										class="w-20"
										label="progress against the previous run's record count"
									/>
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
						</Table.Cell>
						<Table.Cell class="text-right tabular-nums">{count(now.requests)}</Table.Cell>
						<Table.Cell class="text-right tabular-nums {now.errors ? 'text-destructive' : ''}">
							{count(now.errors)}
						</Table.Cell>
						<Table.Cell class="text-xs">{job.attempt}/{job.max_attempts}</Table.Cell>
						<Table.Cell>
							<div class="flex gap-1">
								{#each ['pause', 'resume', 'cancel', 'retry'] as action (action)}
									<form method="POST" action="?/job" use:enhance>
										<input type="hidden" name="id" value={job.id} />
										<input type="hidden" name="action" value={action} />
										<Button variant="ghost" size="xs" type="submit">{action}</Button>
									</form>
								{/each}
							</div>
						</Table.Cell>
					</Table.Row>
				{/each}
			</Table.Body>
		</Table.Root>
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
