<script lang="ts">
	import { getContext } from 'svelte';
	import { onMount } from 'svelte';
	import { enhance } from '$app/forms';
	import type { OpsStream } from '$lib/ops/stream.svelte';
	import type { QueueStatus } from '$lib/ops/types';
	import WorkerCard from '$lib/ops/WorkerCard.svelte';
	import Unavailable from '$lib/ops/Unavailable.svelte';
	import { compact, count, relative, duration, stateTone } from '$lib/ops/format';
	import * as Table from '$lib/components/ui/table';
	import { Card, CardContent, CardHeader, CardTitle } from '$lib/components/ui/card';
	import { Metric } from '$lib/components/ui/metric';
	import { StatusBadge } from '$lib/components/ui/status-badge';
	import { Button } from '$lib/components/ui/button';
	import { NativeSelect } from '$lib/components/ui/native-select';
	import { checkboxClass } from '$lib/components/ui/checkbox';

	let { data, form } = $props();
	const stream = getContext<OpsStream>('ops-stream');

	let picking = $state(false);
	let chosen = $state<string[]>([]);
	let polledQueueStats = $state<QueueStatus | undefined>();
	let queueRefreshError = $state<string | undefined>();
	const queueStats = $derived(polledQueueStats ?? data.queueStats);

	// The stream's roster is authoritative once it arrives; the loaded list is
	// only there so the first paint is not empty.
	const workers = $derived(stream.workers.length ? stream.workers : (data.workers ?? []));
	const queued = $derived(stream.queue.queued ?? 0);
	const running = $derived(stream.queue.running ?? 0);
	const brokerReady = $derived(
		(queueStats?.broker?.routes ?? []).reduce((total, route) => total + (route.ready ?? 0), 0)
	);
	const brokerInFlight = $derived(
		(queueStats?.broker?.routes ?? []).reduce((total, route) => total + (route.in_flight ?? 0), 0)
	);
	const lastRun = $derived((data.runs ?? [])[0]);
	const nextFire = $derived(
		(data.schedules ?? []).filter((s: any) => s.enabled && s.next_fire_at).sort((a: any, b: any) =>
			a.next_fire_at.localeCompare(b.next_fire_at)
		)[0]
	);

	function bytes(value: number | null | undefined): string {
		if (value == null) return '—';
		if (value < 1024) return `${value} B`;
		if (value < 1024 ** 2) return `${(value / 1024).toFixed(1)} KiB`;
		return `${(value / 1024 ** 2).toFixed(1)} MiB`;
	}

	onMount(() => {
		let live = true;
		const refresh = async () => {
			try {
				const response = await fetch('/ops/queue');
				if (!response.ok) throw new Error(`queue status returned ${response.status}`);
				const next = (await response.json()) as QueueStatus;
				if (live) {
					polledQueueStats = next;
					queueRefreshError = undefined;
				}
			} catch (error) {
				if (live) queueRefreshError = error instanceof Error ? error.message : String(error);
			}
		};
		const timer = window.setInterval(refresh, 5000);
		return () => {
			live = false;
			window.clearInterval(timer);
		};
	});
</script>

<svelte:head><title>Operations · catalogue</title></svelte:head>

{#if data.unavailable}
	<Unavailable reason={data.unavailable} />
{:else}
	<div class="grid gap-6">
		<section class="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
			<Metric
				label="Queue"
				value={queued}
				detail={queueStats
					? `${brokerReady} broker ready · ${queueStats.outbox.pending} outbox`
					: `${running} running`}
			/>

			<Metric
				label="Last run"
				value={lastRun ? lastRun.status : '—'}
				tone={lastRun ? undefined : 'text-muted-foreground/70'}
				detail={lastRun
					? `${relative(lastRun.created_at)} · ${lastRun.succeeded}/${lastRun.jobs} ok`
					: undefined}
			/>

			<Metric
				label="Next scheduled"
				value={nextFire ? relative(nextFire.next_fire_at) : '—'}
				detail={nextFire?.id ?? 'no schedule enabled'}
			/>

			<Metric
				label="Unacknowledged"
				value={stream.unacknowledged.length}
				tone={stream.unacknowledged.length ? 'text-warning' : undefined}
			>
				{#snippet children()}
					<div
						class="font-heading text-2xl leading-tight font-semibold tabular-nums {stream
							.unacknowledged.length
							? 'text-warning'
							: ''}"
					>
						{stream.unacknowledged.length}
					</div>
					<a
						class="text-accent-foreground text-xs underline-offset-4 hover:underline"
						href="/ops/notifications">notifications</a
					>
				{/snippet}
			</Metric>
		</section>

		<Card>
			<CardHeader class="border-b">
				<div class="flex flex-wrap items-center justify-between gap-2">
					<div>
						<CardTitle>Queue delivery</CardTitle>
						<p class="text-muted-foreground mt-1 text-xs">
							PostgreSQL authority → transactional outbox → NATS JetStream
						</p>
					</div>
					<StatusBadge tone={queueStats?.broker ? 'good' : 'bad'}>
						{queueStats?.broker ? 'broker connected' : 'broker unavailable'}
					</StatusBadge>
				</div>
			</CardHeader>
			<CardContent>
				{#if queueStats}
					<div class="grid gap-5 lg:grid-cols-3">
						<div>
							<h3 class="eyebrow mb-2">Jobs</h3>
							<dl class="grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
								<dt class="text-muted-foreground">Eligible</dt><dd class="text-right tabular-nums">{count(queueStats.eligible)}</dd>
								<dt class="text-muted-foreground">Leased</dt><dd class="text-right tabular-nums">{count(queueStats.jobs.leased ?? 0)}</dd>
								<dt class="text-muted-foreground">Running</dt><dd class="text-right tabular-nums">{count(queueStats.jobs.running ?? 0)}</dd>
								<dt class="text-muted-foreground">Oldest wait</dt><dd class="text-right tabular-nums">{compact(queueStats.oldest_queued_age_seconds ?? 0)}</dd>
							</dl>
						</div>

						<div>
							<h3 class="eyebrow mb-2">Outbox</h3>
							<dl class="grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
								<dt class="text-muted-foreground">Ready</dt><dd class="text-right tabular-nums">{count(queueStats.outbox.ready)}</dd>
								<dt class="text-muted-foreground">Delayed</dt><dd class="text-right tabular-nums">{count(queueStats.outbox.delayed)}</dd>
								<dt class="text-muted-foreground">With errors</dt><dd class="text-right tabular-nums">{count(queueStats.outbox.errored)}</dd>
								<dt class="text-muted-foreground">Published 1h</dt><dd class="text-right tabular-nums">{count(queueStats.outbox.published_last_hour)}</dd>
							</dl>
						</div>

						<div>
							<h3 class="eyebrow mb-2">JetStream</h3>
							{#if queueStats.broker}
								<dl class="grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
									<dt class="text-muted-foreground">Messages</dt><dd class="text-right tabular-nums">{count(queueStats.broker.messages)}</dd>
									<dt class="text-muted-foreground">In flight</dt><dd class="text-right tabular-nums">{count(brokerInFlight)}</dd>
									<dt class="text-muted-foreground">Consumers</dt><dd class="text-right tabular-nums">{count(queueStats.broker.consumers)}</dd>
									<dt class="text-muted-foreground">Storage</dt><dd class="text-right tabular-nums">{bytes(queueStats.broker.bytes)}</dd>
								</dl>
							{:else}
								<p class="text-destructive text-sm">{queueStats.broker_error ?? 'No broker snapshot.'}</p>
							{/if}
						</div>
					</div>

					{#if queueStats.broker}
						<div class="mt-5 overflow-hidden rounded-lg border">
							<Table.Root>
								<Table.Header>
									<Table.Row>
										<Table.Head>Route</Table.Head>
										<Table.Head class="text-right">Ready</Table.Head>
										<Table.Head class="text-right">In flight</Table.Head>
										<Table.Head class="text-right">Redelivered</Table.Head>
										<Table.Head class="text-right">Delivered</Table.Head>
									</Table.Row>
								</Table.Header>
								<Table.Body>
									{#each queueStats.broker.routes as route (route.route)}
										<Table.Row>
											<Table.Cell><code>{route.route}</code></Table.Cell>
											<Table.Cell class="text-right tabular-nums">{count(route.ready)}</Table.Cell>
											<Table.Cell class="text-right tabular-nums">{count(route.in_flight)}</Table.Cell>
											<Table.Cell class="text-right tabular-nums">{count(route.redelivered)}</Table.Cell>
											<Table.Cell class="text-right tabular-nums">{count(route.delivered)}</Table.Cell>
										</Table.Row>
									{/each}
								</Table.Body>
							</Table.Root>
						</div>
					{/if}
					<p class="text-muted-foreground mt-3 text-xs">
						Updated {relative(queueStats.at)} · refreshes every 5s
						{#if queueRefreshError} · refresh failed: {queueRefreshError}{/if}
					</p>
				{:else}
					<p class="text-muted-foreground text-sm">Queue details are loading.</p>
				{/if}
			</CardContent>
		</Card>

		<Card>
			<CardContent>
				<div class="flex flex-wrap items-center gap-3">
					<h2 class="font-heading text-base font-semibold">Run now</h2>
					<Button variant="ghost" size="xs" onclick={() => (picking = !picking)}>
						{picking ? 'all sources' : 'pick sources'}
					</Button>
					{#if form?.error}
						<span class="text-destructive text-sm">{form.error}</span>
					{:else if form?.run_id}
						<a
							class="text-success text-sm underline-offset-4 hover:underline"
							href="/ops/runs/{form.run_id}"
						>
							started {form.jobs} jobs
						</a>
					{/if}
				</div>

				<form method="POST" action="?/run" use:enhance class="mt-2 grid gap-3">
					{#if picking}
						<div
							class="border-border grid max-h-56 grid-cols-2 gap-1 overflow-y-auto rounded border p-2 sm:grid-cols-3 lg:grid-cols-4"
						>
							{#each data.sources ?? [] as source (source.source_id)}
								<label class="flex items-center gap-2 text-sm">
									<!-- A native input, not `<Checkbox>`: see `checkboxClass`. -->
									<input
										type="checkbox"
										class={checkboxClass}
										name="sources"
										value={source.source_id}
										bind:group={chosen}
									/>
									<span class="truncate" title={source.label}>{source.source_id}</span>
								</label>
							{/each}
						</div>
					{/if}

					<div class="flex flex-wrap items-center gap-3">
						<label class="flex items-center gap-2 text-sm">
							<span class="text-muted-foreground">cache</span>
							<NativeSelect name="cache_mode" class="h-8 text-xs" fit>
								<!-- refresh first and by default: a run under `auto` with a
								     stale max age replays yesterday's pages and reports
								     success while changing no prices. -->
								<option value="refresh">refresh (fetch everything)</option>
								<option value="auto">auto (use what is fresh)</option>
								<option value="replay">replay (offline, no network)</option>
							</NativeSelect>
						</label>
						<Button size="sm" type="submit">
							Run {picking && chosen.length ? `${chosen.length} sources` : 'all sources'}
						</Button>
					</div>
				</form>
			</CardContent>
		</Card>

		<section>
			<h2 class="eyebrow mb-3">Workers</h2>
			{#if workers.length === 0}
				<p class="text-muted-foreground text-sm">
					No workers have registered. Start one with <code>catalogue-worker</code>.
				</p>
			{:else}
				<div class="grid gap-3 md:grid-cols-2 lg:grid-cols-3">
					{#each workers as worker (worker.worker_id)}
						<WorkerCard {worker} {stream} />
					{/each}
				</div>
			{/if}
		</section>

		<section>
			<div class="mb-3 flex items-center justify-between">
				<h2 class="eyebrow">Recent runs</h2>
				<a
					class="text-accent-foreground text-sm underline-offset-4 hover:underline"
					href="/ops/runs">all runs</a
				>
			</div>
			<div class="bg-card overflow-hidden rounded-lg border">
				<Table.Root>
					<Table.Header>
						<Table.Row>
							<Table.Head>Started</Table.Head>
							<Table.Head>Kind</Table.Head>
							<Table.Head>Status</Table.Head>
							<Table.Head>Sources</Table.Head>
							<Table.Head>Duration</Table.Head>
						</Table.Row>
					</Table.Header>
					<Table.Body>
						{#each data.runs ?? [] as run (run.id)}
							<Table.Row>
								<Table.Cell>
									<a
										class="text-accent-foreground underline-offset-4 hover:underline"
										href="/ops/runs/{run.id}">{relative(run.created_at)}</a
									>
								</Table.Cell>
								<Table.Cell>{run.kind}</Table.Cell>
								<Table.Cell><StatusBadge tone={stateTone(run.status)}>{run.status}</StatusBadge></Table.Cell>
								<Table.Cell>
									{run.succeeded}/{run.jobs}{run.failed ? ` · ${run.failed} failed` : ''}
								</Table.Cell>
								<Table.Cell>{duration(run.started_at, run.finished_at)}</Table.Cell>
							</Table.Row>
						{:else}
							<Table.Row>
								<Table.Cell colspan={5} class="text-muted-foreground">No runs yet.</Table.Cell>
							</Table.Row>
						{/each}
					</Table.Body>
				</Table.Root>
			</div>
		</section>
	</div>
{/if}
