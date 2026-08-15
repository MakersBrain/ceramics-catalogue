<script lang="ts">
	import { enhance } from '$app/forms';
	import type { OpsStream, Worker } from './stream.svelte';
	import { compact, relative } from './format';
	import WorkerJobLive from './WorkerJobLive.svelte';
	import { Card, CardContent } from '$lib/components/ui/card';
	import { Button } from '$lib/components/ui/button';

	let { worker, stream }: { worker: Worker; stream: OpsStream } = $props();

	// Derived from `last_heartbeat_at` and a local clock, not from an event.
	// Nothing fires when a process stops existing, so a worker that died has to
	// go stale on its own or it never goes stale at all.
	const age = $derived(stream.heartbeatAge(worker));
	const health = $derived(stream.health(worker));
	const jobs = $derived(worker.current_jobs ?? []);

	const tone = $derived(
		{ ok: 'border-border', suspect: 'border-warning', lost: 'border-destructive' }[health]
	);
	const dot = $derived({ ok: 'bg-success', suspect: 'bg-warning', lost: 'bg-destructive' }[health]);
</script>

<!--
	The health tone overrides the card's own border, which is why it is passed as
	a class rather than wrapped around one: a worker that has stopped answering
	should be legible as a shape in a grid of twelve, before any of the words are
	read. `--card-spacing` is retuned rather than padding being set directly, so
	the header and the body stay in step with each other.
-->
<Card class="gap-0 [--card-spacing:--spacing(4)] {tone}">
	<CardContent class="flex flex-col gap-2">
		<div class="flex items-start justify-between gap-2">
			<div class="min-w-0">
				<div class="flex items-center gap-2">
					<span class="inline-block h-2 w-2 shrink-0 rounded-full {dot}"></span>
					<span class="truncate font-medium" title={worker.hostname}>{worker.hostname}</span>
					<span class="text-muted-foreground text-xs">pid {worker.pid}</span>
				</div>
				<div class="text-muted-foreground mt-1 text-xs">
					{worker.status}{worker.desired_state !== 'running' ? ` → ${worker.desired_state}` : ''}
					· up {compact((Date.now() - new Date(worker.started_at).getTime()) / 1000)}
					{#if worker.capabilities?.length}
						· {worker.capabilities.join(', ')}
					{/if}
				</div>
			</div>
			<span
				class="text-xs whitespace-nowrap {health === 'ok' ? 'text-muted-foreground' : 'text-destructive'}"
				title="last heartbeat {relative(worker.last_heartbeat_at)}"
			>
				{compact(age)} ago
			</span>
		</div>

		{#if health === 'lost'}
			<p class="text-destructive text-xs">
				No heartbeat. Its jobs are recovered when their leases expire.
			</p>
		{/if}

		<div class="text-sm">
			{#if jobs.length}
				<span class="font-medium">{jobs.length}</span> active {jobs.length === 1 ? 'source' : 'sources'}
			{:else if worker.current_source}
				crawling <span class="font-medium">{worker.current_source}</span>
			{:else}
				<span class="text-muted-foreground">idle</span>
			{/if}
		</div>

		{#if jobs.length && health !== 'lost'}
			<div class="border-border mt-1 divide-y border-t">
				{#each jobs as job (job.job_id)}
					<WorkerJobLive {job} {stream} />
				{/each}
			</div>
		{/if}

		<div class="mt-1 flex flex-wrap gap-1">
			{#each health === 'lost' ? ['hide'] : ['pause', 'resume', 'drain', 'stop'] as action (action)}
				<form method="POST" action="/ops?/worker" use:enhance>
					<input type="hidden" name="id" value={worker.worker_id} />
					<input type="hidden" name="action" value={action} />
					<Button
						size="xs"
						variant={action === 'stop' ? 'destructive' : 'ghost'}
						type="submit"
						disabled={worker.status === 'stopped'}
					>
						{action}
					</Button>
				</form>
			{/each}
		</div>
		<p class="text-muted-foreground/70 text-xs">
			{health === 'lost'
				? 'Hide removes this stale registration from the roster; its audit row remains.'
				: 'Controls this process, not the replica count: a restart policy may start another.'}
		</p>
	</CardContent>
</Card>
