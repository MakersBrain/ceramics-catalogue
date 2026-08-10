<script lang="ts">
	import { enhance } from '$app/forms';
	import type { OpsStream, Worker } from './stream.svelte';
	import { compact, count, relative } from './format';
	import WorkerTrace from './WorkerTrace.svelte';

	let { worker, stream }: { worker: Worker; stream: OpsStream } = $props();

	// Derived from `last_heartbeat_at` and a local clock, not from an event.
	// Nothing fires when a process stops existing, so a worker that died has to
	// go stale on its own or it never goes stale at all.
	const age = $derived(stream.heartbeatAge(worker));
	const health = $derived(stream.health(worker));
	const progress = $derived(
		worker.current_job_id ? stream.progress[worker.current_job_id] : undefined
	);
	const rate = $derived(stream.jobRate(worker.current_job_id));
	let traceOpen = $state(false);

	const tone = $derived(
		{ ok: 'border-base-300', suspect: 'border-warning', lost: 'border-error' }[health]
	);
	const dot = $derived({ ok: 'bg-success', suspect: 'bg-warning', lost: 'bg-error' }[health]);
</script>

<div class="card bg-base-100 border shadow-sm {tone}">
	<div class="card-body gap-2 p-4">
		<div class="flex items-start justify-between gap-2">
			<div class="min-w-0">
				<div class="flex items-center gap-2">
					<span class="inline-block h-2 w-2 shrink-0 rounded-full {dot}"></span>
					<span class="truncate font-medium" title={worker.hostname}>{worker.hostname}</span>
					<span class="text-base-content/50 text-xs">pid {worker.pid}</span>
				</div>
				<div class="text-base-content/60 mt-1 text-xs">
					{worker.status}{worker.desired_state !== 'running' ? ` → ${worker.desired_state}` : ''}
					· up {compact((Date.now() - new Date(worker.started_at).getTime()) / 1000)}
					{#if worker.capabilities?.length}
						· {worker.capabilities.join(', ')}
					{/if}
				</div>
			</div>
			<span
				class="text-xs whitespace-nowrap {health === 'ok' ? 'text-base-content/50' : 'text-error'}"
				title="last heartbeat {relative(worker.last_heartbeat_at)}"
			>
				{compact(age)} ago
			</span>
		</div>

		{#if health === 'lost'}
			<p class="text-error text-xs">
				No heartbeat. Its jobs are recovered when their leases expire.
			</p>
		{/if}

		<div class="text-sm">
			{#if worker.current_source}
				crawling <span class="font-medium">{worker.current_source}</span>
			{:else}
				<span class="text-base-content/50">idle</span>
			{/if}
		</div>

		{#if progress}
			<div class="border-base-300 mt-1 border-t pt-2">
				<div class="text-base-content/50 mb-2 flex items-center justify-between text-xs">
					<span>{progress.phase ?? 'collecting'}</span>
					<span class="tabular-nums">updated {relative(progress.at)}</span>
				</div>
				<dl class="grid grid-cols-3 gap-x-3 gap-y-2 text-xs">
					<div>
						<dt class="text-base-content/50">indexed</dt>
						<dd class="tabular-nums text-sm font-medium">{count(progress.records)}</dd>
					</div>
					<div>
						<dt class="text-base-content/50">discovered</dt>
						<dd class="tabular-nums text-sm font-medium">{count(progress.discovered)}</dd>
					</div>
					<div>
						<dt class="text-base-content/50">errors</dt>
						<dd class="tabular-nums text-sm font-medium {progress.errors ? 'text-error' : ''}">
							{count(progress.errors)}
						</dd>
					</div>
					<div>
						<dt class="text-base-content/50">requests</dt>
						<dd class="tabular-nums">{count(progress.requests)}</dd>
					</div>
					<div>
						<dt class="text-base-content/50">index rate</dt>
						<dd class="tabular-nums">{rate ? `${rate.records.toFixed(1)}/s` : '—'}</dd>
					</div>
					<div>
						<dt class="text-base-content/50">request rate</dt>
						<dd class="tabular-nums">{rate ? `${rate.requests.toFixed(1)}/s` : '—'}</dd>
					</div>
				</dl>
				{#if progress.in_flight?.length}
					<div class="text-base-content/50 mt-2 truncate font-mono text-[0.68rem]" title={progress.in_flight[0].url}>
						{progress.in_flight.length} in flight · {progress.in_flight[0].url}
					</div>
				{/if}
			</div>
		{/if}

		{#if worker.current_job_id && health !== 'lost'}
			<details class="border-base-300 border-t pt-2" bind:open={traceOpen}>
				<summary class="cursor-pointer text-xs font-medium">Live log trace</summary>
				{#if traceOpen}
					<WorkerTrace jobId={worker.current_job_id} />
				{/if}
			</details>
		{/if}

		<div class="mt-1 flex flex-wrap gap-1">
			{#each health === 'lost' ? ['hide'] : ['pause', 'resume', 'drain', 'stop'] as action (action)}
				<form method="POST" action="/ops?/worker" use:enhance>
					<input type="hidden" name="id" value={worker.worker_id} />
					<input type="hidden" name="action" value={action} />
					<button
						class="btn btn-xs {action === 'stop' ? 'btn-error btn-outline' : 'btn-ghost'}"
						type="submit"
						disabled={worker.status === 'stopped'}
					>
						{action}
					</button>
				</form>
			{/each}
		</div>
		<p class="text-base-content/40 text-xs">
			{health === 'lost'
				? 'Hide removes this stale registration from the roster; its audit row remains.'
				: 'Controls this process, not the replica count: a restart policy may start another.'}
		</p>
	</div>
</div>
